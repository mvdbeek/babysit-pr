"""Repairs of watches on worker machines: the guardian stays here, the agent runs there.

The controller's guardian (``pr_supervisor.run_repair``) keeps the watch, its session
lease and its result. It stages a spec on the worker and runs the worker half,
``pr_supervisor.py _repair_worker SPEC``, from the worker's deployed babysit-pr over
ssh. That half never opens a queue: it checks the checkout with this machine's hardened
Git, runs the agent in its own process group inside the worker's babysit-bwrap, keeps
``~/.local/state/babysit-pr/runs/<attempt>/`` there and streams the agent's output,
which the guardian copies into its own ``agent.log``.

The ssh connection's stdin is the worker half's lifeline: when it ends (the guardian
stopped, was killed or lost the network) the worker half stops the agent's group. So
a controller restart kills repairs in flight on workers; their watches are blocked and
retried later, never reattached. A guardian that loses the worker mid-repair cannot
tell whether the agent still runs: it leaves an orphan tombstone on the session's lease
(``session_locks``), which ``reconcile`` clears once the worker shows the run ended.
"""

import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import machines
import owned_process
import session_locks

SPECS = "~/.local/state/babysit-pr/repair-specs"
# Specs are read once, at start; older ones are removed when the next is staged.
SPEC_KEEP = 7 * 86400
# The worker enforces the repair's timeout; past this much more, the guardian gives up.
SSH_SLACK = 60
# After its lifeline ends, the worker half stops its agent (5 s grace) and exits.
LIFELINE_GRACE = 15
# How often a pane repair's guardian asks the worker for its result.
PANE_POLL = 5
# The end of a pane repair's agent.log copied back for the dashboard.
MAX_LOG = 1024 * 1024
STATUS_TIMEOUT = 30
CANCEL_TIMEOUT = 20
MAX_STATUS = 1024 * 1024
MAX_SUMMARY = 20000


class Unreachable(Exception):
    """The worker could not be asked; a run there may still be going."""


def worker_python(worker):
    python = worker.python
    return machines.remote_path(python) if python.startswith(("/", "~")) else python


def worker_command(worker, *args):
    script = machines.remote_path(f"{worker.babysit_root.rstrip('/')}/scripts/pr_supervisor.py")
    return [worker_python(worker), script, *args]


def unreachable(worker):
    return (
        f"{worker.label} unreachable mid-repair; remote agent may still be running "
        "(the session stays held until the worker shows the run ended)"
    )


def check_version(home, worker):
    import pr_supervisor
    import remote_probe

    deployed = remote_probe.call(
        home, worker, "version", STATUS_TIMEOUT, babysit_root=worker.babysit_root
    )["head"]
    own = pr_supervisor.git(pr_supervisor.SKILL, "rev-parse", "HEAD")
    if deployed != own:
        raise RuntimeError(
            f"{worker.label} runs babysit-pr {deployed[:12]}, this machine {own[:12]}: "
            f"update babysit-pr on {worker.label} (provision_worker.sh --user-step --sha {own})"
        )


def spec_for(worker, job, attempt, prompt, pr):
    """What the worker half needs, and nothing the worker should not decide."""
    keys = (
        "id",
        "agent",
        "cwd",
        "session_id",
        "model",
        "sandbox",
        "claude_permission_mode",
        "claude_config_dir",
        "claude_config_env",
        "claude_subscription",
        "repair_timeout",
    )
    return {
        "attempt": attempt,
        "job": {key: job.get(key) for key in keys},
        # The worker half builds its own launcher, never one the job names.
        "features": ["gpg"] if "gpg" in worker.labels else [],
        "head_branch": pr["head_branch"],
        "head_sha": pr["head_sha"],
        "prompt": prompt,
        # The worker's own herdr binding: the worker half checks it runs in that pane.
        "pane": job.get("pane"),
    }


def status(home, worker, attempt, timeout=STATUS_TIMEOUT):
    """The worker's record of a run: started, still running, its result and reply."""
    try:
        done = machines.run(
            home,
            worker,
            worker_command(worker, "_repair_status", attempt),
            timeout=timeout,
            max_output=MAX_STATUS,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise Unreachable(str(exc)) from exc
    if done.returncode == 255:
        raise Unreachable(done.stderr.decode(errors="replace").strip()[-300:])
    try:
        answer = json.loads(done.stdout)
    except ValueError as exc:
        raise Unreachable(f"{worker.label} answered no run status") from exc
    return validate_status(answer)


def validate_status(value):
    if not isinstance(value, dict) or set(value) != {"started", "running", "result", "reply"}:
        raise Unreachable("Malformed run status")
    if not isinstance(value["started"], bool) or not isinstance(value["running"], bool):
        raise Unreachable("Malformed run status")
    result = value["result"]
    if result is not None and not (
        isinstance(result, dict)
        and set(result) == {"exit", "error"}
        and (result["exit"] is None or type(result["exit"]) is int)
        and (result["error"] is None or isinstance(result["error"], str))
    ):
        raise Unreachable("Malformed run result")
    return value


def outcome(value):
    """A structured repair outcome, as a local repair's ``reply.json`` must be."""
    if (
        not isinstance(value, dict)
        or value.get("status") not in {"waiting", "blocked"}
        or not isinstance(value.get("summary"), str)
    ):
        raise RuntimeError("Repair returned no valid structured outcome")
    return {"status": value["status"], "summary": value["summary"][:MAX_SUMMARY]}


def orphaned(job, attempt):
    """Keep the session held: its agent may still run on the lost worker."""
    session_locks.mark_orphan(
        job.get("agent"), job["session_id"], job.get("claude_config_dir"), attempt, job["machine"]
    )


def cancel(home, worker, attempt):
    """Ask the worker half to stop its agent; whether it confirmed it stopped."""
    try:
        done = machines.run(
            home,
            worker,
            worker_command(worker, "_repair_cancel", attempt),
            timeout=CANCEL_TIMEOUT,
            max_output=MAX_STATUS,
        )
        return done.returncode == 0 and json.loads(done.stdout).get("stopped") is True
    except (subprocess.SubprocessError, OSError, ValueError, AttributeError):
        return False


def run(home, job, folder, pr, prompt):
    """Run one repair's agent on the job's worker; its structured outcome.

    Called inside the session's lease, which an orphan tombstone keeps held when the
    worker is lost. A stop request (KeyboardInterrupt) stops the ssh client, so the worker
    half sees its lifeline end, then asks it to cancel; it is raised again after.
    """
    import claude_runner

    worker = machines.resolve(home, job["machine"])
    assert worker is not None
    attempt = folder.name
    check_version(home, worker)
    spec = machines.put_file(
        home,
        worker,
        machines.remote_path(SPECS),
        f"{attempt}.json",
        json.dumps(spec_for(worker, job, attempt, prompt, pr)).encode(),
        keep=SPEC_KEEP,
    )
    if job.get("pane"):
        return run_in_pane(home, worker, job, folder, spec)
    argv = machines.remote_argv(home, worker, worker_command(worker, "_repair_worker", spec))
    proc = None
    reader = None
    lost = False
    with (folder / "agent.log").open("wb") as log, (folder / "worker.log").open("ab") as errors:
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,  # The lifeline: never written, closed when we stop.
                stdout=subprocess.PIPE,
                stderr=errors,
                start_new_session=True,
            )
            # A local repair's agent.log, from the worker's stream: read_result works as is.
            reader = threading.Thread(
                target=claude_runner.stream_output, args=(proc.stdout, log, False), daemon=True
            )
            reader.start()
            try:
                code = proc.wait(timeout=job["repair_timeout"] + SSH_SLACK)
            except subprocess.TimeoutExpired:
                lost = True
            else:
                lost = code == 255
        except KeyboardInterrupt:
            # A second stop request must not cut this cleanup short.
            for sig in (signal.SIGHUP, signal.SIGTERM):
                signal.signal(sig, signal.SIG_IGN)
            if proc is not None:
                assert proc.stdin is not None
                proc.stdin.close()  # The lifeline ends: the worker half stops its agent.
                try:
                    proc.wait(timeout=LIFELINE_GRACE)
                except subprocess.TimeoutExpired:
                    pass
                owned_process.stop_group(proc, grace=5)
            if not cancel(home, worker, attempt):
                orphaned(job, attempt)
            raise
        finally:
            if proc is not None:
                owned_process.stop_group(proc, grace=5)
                assert proc.stdin is not None
                proc.stdin.close()  # The lifeline ends with this guardian's interest.
            if reader is not None:
                reader.join(timeout=5)
    try:
        if lost:
            raise Unreachable(unreachable(worker))
        record = status(home, worker, attempt)
        if record["result"] is None:
            if record["running"] or not record["started"]:
                raise Unreachable(unreachable(worker))
            raise RuntimeError(f"The repair on {worker.label} ended without a result")
    except Unreachable:
        orphaned(job, attempt)
        return {"status": "blocked", "summary": unreachable(worker)}
    if job.get("agent") == "claude" and reader is not None and reader.is_alive():
        raise RuntimeError("Claude output stream did not finish")
    return conclude(worker, job, folder, record, streamed=True)


def typed_path(path):
    # "$HOME" rather than ~, so a quoted remainder still lands in the worker's home.
    return '"$HOME"/' + shlex.quote(path[2:]) if path.startswith("~/") else shlex.quote(path)


def run_in_pane(home, worker, job, folder, spec):
    """A pane-bound repair: the worker half typed into the watch's pane on the worker.

    There is no lifeline: the agent runs in view, bounded by its own timeout; this
    guardian polls for the result until that timeout plus SSH_SLACK, and a stop request
    asks the worker to cancel.
    """
    import herdr_handoff
    import pane_runner
    import sandbox

    attempt = folder.name
    pane = pane_runner.locate(job["pane"], job["cwd"], require_shell=True, machine=worker)
    python = worker.python
    script = f"{worker.babysit_root.rstrip('/')}/scripts/pr_supervisor.py"
    line = " ".join(
        [
            typed_path(python) if python.startswith(("/", "~")) else shlex.quote(python),
            typed_path(script),
            "_repair_worker",
            shlex.quote(spec),
        ]
    )
    deadline = time.monotonic() + job["repair_timeout"] + SSH_SLACK
    record = None
    try:
        # Exactly one submission, never retried: delivery may have happened. A stop
        # request from here on cancels, so a line that starts late finds the run taken.
        herdr_handoff.herdr("pane", "run", pane, sandbox.typed(line), machine=worker)
        while True:
            time.sleep(PANE_POLL)
            try:
                record = status(home, worker, attempt)
            except Unreachable:
                record = None  # A transient failure: asked again until the deadline.
            if record is not None and record["result"] is not None:
                break
            if time.monotonic() >= deadline:
                orphaned(job, attempt)
                return {"status": "blocked", "summary": unreachable(worker)}
    except KeyboardInterrupt:
        for sig in (signal.SIGHUP, signal.SIGTERM):
            signal.signal(sig, signal.SIG_IGN)
        if not cancel(home, worker, attempt):
            orphaned(job, attempt)
        raise
    log = machines.remote_path(f"~/.local/state/babysit-pr/runs/{attempt}/agent.log")
    try:
        copied = machines.run(
            home, worker, ["tail", "-c", str(MAX_LOG), log], timeout=STATUS_TIMEOUT
        )
        (folder / "agent.log").write_bytes(copied.stdout if copied.returncode == 0 else b"")
    except (subprocess.SubprocessError, OSError):
        pass  # The outcome stands without the log.
    return conclude(worker, job, folder, record, streamed=False)


def conclude(worker, job, folder, record, streamed):
    """The outcome of a run the worker recorded. A Claude result comes from the stream
    when this guardian copied it, else from the reply the worker half parsed."""
    import claude_runner
    import gh_pr_watch as watch

    result = record["result"]
    if result["error"]:
        raise RuntimeError(f"{worker.label}: {result['error'][:2000]}")
    if result["exit"] != 0:
        raise RuntimeError(
            f"{job.get('agent', 'codex')} exited {result['exit']} on {worker.label}; "
            f"inspect {folder / 'agent.log'}"
        )
    if job.get("agent") == "claude" and streamed:
        reply = claude_runner.read_result(folder / "agent.log", job["session_id"])
    else:
        # Fetched apart from the agent's output, which cannot forge it.
        reply = record["reply"]
    reply = outcome(reply)
    watch.save_state(folder / "reply.json", reply)
    return reply


def reconcile(home, machine_id, session_id, attempt):
    """Clear an orphan tombstone once its worker shows that run ended; whether it did."""
    worker = machines.load(home).get(machine_id)
    if worker is None or not isinstance(attempt, str):
        return False
    try:
        record = status(home, worker, attempt)
    except Unreachable:
        return False
    if record["running"]:
        return False
    # A spec staged or a line typed may still start the run: take its run folder first.
    if not record["started"] and not cancel(home, worker, attempt):
        return False
    current = session_locks.orphan_attempt(session_id, machine_id)
    if current == attempt:
        session_locks.clear_orphan(None, session_id, None, machine_id)
    return True


# The worker half -------------------------------------------------------------


def state_home():
    return Path.home() / ".local" / "state" / "babysit-pr"


def run_folder(attempt):
    return state_home() / "runs" / str(uuid.UUID(attempt))


def read_spec(path):
    path = Path(path).resolve()
    if path.parent != Path(os.path.expanduser(SPECS)).resolve():
        raise ValueError("A repair spec is read only from the staged specs folder")
    spec = json.loads(path.read_text())
    job = spec.get("job") if isinstance(spec, dict) else None
    if (
        not isinstance(job, dict)
        or job.get("agent") not in {"codex", "claude"}
        or not isinstance(job.get("cwd"), str)
        or not job["cwd"].startswith("/")
        or type(job.get("repair_timeout")) is not int
        or not set(spec.get("features", [])) <= {"gpg"}
        or not isinstance(spec.get("prompt"), str)
    ):
        raise ValueError("Malformed repair spec")
    uuid.UUID(spec["attempt"])
    uuid.UUID(job["session_id"])
    return spec


def worker_main(spec_path):
    """``_repair_worker``: run one repair's agent here, bounded by its timeout and lifeline."""
    import claude_accounts
    import github_cli
    import pr_supervisor
    import sandbox

    spec = read_spec(spec_path)
    job, attempt = spec["job"], spec["attempt"]
    folder = run_folder(attempt)
    codex_out = Path.home() / ".cache" / "babysit-repairs" / attempt
    folder.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.mkdir(mode=0o700)  # Single use: a replayed spec finds it and stops.
    (folder / "pid").write_text(json.dumps({"worker": os.getpid()}))
    result: dict[str, int | str | None] = {"exit": None, "error": "Repair did not finish"}
    proc = None
    lost = threading.Event()
    stopping = threading.Lock()

    def stop():
        with stopping:
            if proc is not None and proc.poll() is None:
                owned_process.stop_group(proc, grace=5)

    def lifeline():
        # EOF: the guardian stopped, died or lost the network.
        while sys.stdin.buffer.read(65536):
            pass
        lost.set()
        stop()

    pane = spec.get("pane")

    def interrupted(signum, frame):
        lost.set()
        raise KeyboardInterrupt()

    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, interrupted)
    if not pane:
        # In a pane stdin is the terminal: the agent runs in view, bounded by its timeout.
        threading.Thread(target=lifeline, daemon=True).start()
    output = None
    try:
        if pane:
            import pane_runner

            # The worker's own herdr: the binding was captured there.
            pane_runner.verify_runner({"pane": pane, "cwd": job["cwd"]})
            print(f"\nBabysitter: resuming {job['session_id']}\n", flush=True)
        pr_supervisor.verify_worktree(
            {"cwd": job["cwd"], "machine": None},
            {"head_branch": spec["head_branch"], "head_sha": spec["head_sha"]},
        )
        pr_supervisor.watch.save_state(folder / "schema.json", pr_supervisor.RESULT_SCHEMA)
        prompt_path = folder / "prompt.txt"
        prompt_path.write_text(spec["prompt"])
        features = tuple(spec.get("features", ()))
        local = {**job, "sandbox_backend": "bwrap", "sandbox_features": features}
        if job["agent"] == "claude":
            local["claude_command"] = sandbox.claude_launcher("bwrap", features)
        else:
            # Codex writes its reply beside its schema, in a folder of their own that the
            # sandbox is given: never the run folder, whose result and pid it could forge.
            codex_out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            codex_out.mkdir(mode=0o700)
            pr_supervisor.watch.save_state(codex_out / "schema.json", pr_supervisor.RESULT_SCHEMA)
            grants = sandbox.Grants(
                add_dirs=(str(codex_out),),
                env_pass=sandbox.REPAIR_ENV,
                features=("playwright", *features),
            )
            local["codex_command"] = sandbox.wrap(["codex"], grants, "bwrap")
        env = os.environ.copy()
        if job["agent"] == "claude" and job.get("claude_config_dir"):
            env = claude_accounts.environment(
                job["claude_config_dir"],
                subscription=job.get("claude_subscription", False),
                base=env,
                config_env=job.get("claude_config_env", False),
            )
        env.pop("CODEX_THREAD_ID", None)
        for key in ("CLAUDECODE", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            env.pop(key, None)
        env["BABYSIT_PR_REPAIR"] = job["id"]
        env.update(github_cli.NON_INTERACTIVE)
        with prompt_path.open() as prompt, (folder / "agent.log").open("wb") as log:
            with stopping:
                # Checked under the lock the lifeline stops under: an agent never starts
                # after its lifeline ended.
                if lost.is_set():
                    raise KeyboardInterrupt()
                proc = subprocess.Popen(
                    pr_supervisor.command_for(
                        local, codex_out if job["agent"] == "codex" else folder
                    ),
                    cwd=job["cwd"],
                    env=env,
                    stdin=prompt,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            (folder / "pid").write_text(json.dumps({"worker": os.getpid(), "agent": proc.pid}))
            if job["agent"] == "claude" and pane:
                import claude_runner

                # Shown readably in the pane; the raw stream stays in agent.log.
                output = threading.Thread(
                    target=claude_runner.stream_output,
                    args=(proc.stdout, log, True),
                    daemon=True,
                )
            else:  # Streamed as it is to the guardian, or into the pane.
                output = threading.Thread(
                    target=pr_supervisor.tee_output, args=(proc.stdout, log), daemon=True
                )
            output.start()
            try:
                code = proc.wait(timeout=job["repair_timeout"])
            except subprocess.TimeoutExpired:
                result = {"exit": None, "error": f"Timed out after {job['repair_timeout']} s"}
            else:
                output.join(timeout=5)
                result = {"exit": code, "error": None}
                if code == 0 and job["agent"] == "codex":
                    pr_supervisor.watch.save_state(folder / "reply.json", codex_reply(codex_out))
                if code == 0 and job["agent"] == "claude":
                    import claude_runner

                    # For a pane repair's guardian, which never sees the stream.
                    pr_supervisor.watch.save_state(
                        folder / "reply.json",
                        claude_runner.read_result(folder / "agent.log", job["session_id"]),
                    )
            if lost.is_set():
                raise KeyboardInterrupt()
    except KeyboardInterrupt:
        result = {"exit": None, "error": "Repair stopped: the controller ended its connection"}
    except Exception as exc:
        result = {"exit": None, "error": str(exc)[:2000]}
    finally:
        for sig in (signal.SIGHUP, signal.SIGTERM):
            signal.signal(sig, signal.SIG_IGN)
        stop()
        if proc is not None:
            owned_process.stop_group(proc)
        if output is not None:
            output.join(timeout=5)
        shutil.rmtree(codex_out, ignore_errors=True)
        pr_supervisor.watch.save_state(folder / "result.json", result)
        if pane:
            print(f"\nBabysitter: {result['error'] or 'agent exited'}\n", flush=True)
    return 0


def codex_reply(folder):
    """The reply Codex wrote in its sandboxed folder: a regular file, read bounded."""
    descriptor = os.open(folder / "reply.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Codex left no reply file")
        data = stream.read(MAX_STATUS // 2 + 1)
    if len(data) > MAX_STATUS // 2:
        raise ValueError("Codex's reply is too large")
    return json.loads(data)


def _alive(pid, attempt):
    """Whether ``pid`` still runs this attempt's worker half (not a reused pid)."""
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        command = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return attempt in command


def _read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def worker_status(attempt):
    """``_repair_status``: what this worker knows of a run, for the controller."""
    folder = run_folder(attempt)
    pids = _read(folder / "pid") or {}
    result = _read(folder / "result.json")
    reply = _read(folder / "reply.json")
    return {
        "started": folder.is_dir(),
        "running": result is None and _alive(pids.get("worker"), attempt),
        "result": result if isinstance(result, dict) else None,
        "reply": reply
        if isinstance(reply, dict) and len(json.dumps(reply)) < MAX_STATUS // 2
        else None,
    }


def worker_cancel(attempt, wait=10):
    """``_repair_cancel``: stop this attempt's worker half, which stops its agent first.

    A run not started yet is taken, so a late ``_repair_worker`` (a staged spec, a line
    typed into a pane) fails at its own exclusive mkdir instead of starting an agent.
    """
    folder = run_folder(attempt)
    folder.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        folder.mkdir(mode=0o700)
    except FileExistsError:
        pass
    else:
        result = {"exit": None, "error": "Cancelled before start"}
        (folder / "result.json").write_text(json.dumps(result))
        return {"stopped": True}
    deadline = time.monotonic() + wait
    # The worker half records its pid right after its mkdir; a run that just started
    # has it a moment later.
    pid = (_read(folder / "pid") or {}).get("worker")
    while pid is None and not (folder / "result.json").exists():
        if time.monotonic() >= deadline:
            return {"stopped": False}
        time.sleep(0.1)
        pid = (_read(folder / "pid") or {}).get("worker")
    if isinstance(pid, int) and _alive(pid, attempt):
        os.kill(pid, signal.SIGTERM)
    while _alive(pid, attempt) and time.monotonic() < deadline:
        time.sleep(0.2)
    return {"stopped": not _alive(pid, attempt)}
