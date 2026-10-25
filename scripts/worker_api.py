"""Watch registration and handoff from worker machines, forwarded to the controller's dashboard.

Each registered machine has one token, issued on the controller (``pr_supervisor.py
worker-token ID``) and kept on the worker in ``~/.config/babysit-pr/worker.json`` with
the controller's dashboard URL. The controller stores only the token's SHA-256, in
``<home>/worker-tokens.json`` (mode 600), apart from ``machines.json``, which may be
shared. The token decides the machine: a request cannot name another one.

What the token allows is registering watches on its own machine, for sessions and
checkouts the controller then reads there itself (``remote_probe``), and handing off a
watch registered there: the controller then exits that watch's agent through the
worker's herdr, as ``herdr_handoff`` does here. Any process on the worker that can read
``worker.json``, which babysit-bwrap shows to every sandboxed agent there, can do that.
"""

import hashlib
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from argparse import Namespace
from pathlib import Path
from urllib.parse import urlsplit

import machines

TOKENS = "worker-tokens.json"
TOKEN = re.compile(r"[A-Za-z0-9_-]{20,128}")
MAX_BODY = 65536
MAX_ANSWER = 65536
# Reading the worker's checkout and session, and GitHub, can take a while over ssh.
FORWARD_TIMEOUT = 300
ACTIONS = {"register", "handoff"}
# Set by babysit-bwrap inside a worker's sandbox: its registration broker's socket.
SOCKET_ENV = "BABYSIT_REGISTER_SOCKET"
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
PR = re.compile(
    r"[1-9][0-9]{0,9}|https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*"
)
PANE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,63}")
WATCH = re.compile(r"[0-9a-f]{16}")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Text that reaches a repair prompt may span lines; nothing else a terminal acts on.
PROMPT_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# One request per machine and worktree at a time; a flood waits for nothing.
_busy: dict[str, threading.Lock] = {}
_busy_lock = threading.Lock()
# Per machine and worktree, a bucket of RATE requests refilled over a minute; a
# failed one costs FAILURE_COST more, so repeated failures back off. At most MAX_KEYS
# are kept before idle ones are dropped.
RATE = 6
FAILURE_COST = 2
_buckets: dict[str, tuple[float, float]] = {}
MAX_KEYS = 256
# Worker watches are polled from this machine's GitHub token; never faster than this.
MIN_POLL = 30
# How long a handoff from a worker may wait for its agent's final response.
MAX_HANDOFF_WAIT = 3600


class RateLimited(ValueError):
    """Too many requests from one machine; answered 429."""


def _take(key, cost=1.0, force=False):
    """Whether ``cost`` tokens were available; ``force`` takes them regardless."""
    now = time.monotonic()
    with _busy_lock:
        if len(_buckets) > MAX_KEYS:
            # Buckets refilled by now are as good as new ones.
            for name, (tokens, then) in list(_buckets.items()):
                if tokens + (now - then) * RATE / 60 >= RATE:
                    del _buckets[name]
        tokens, then = _buckets.get(key, (float(RATE), now))
        tokens = min(float(RATE), tokens + (now - then) * RATE / 60)
        if tokens < cost and not force:
            _buckets[key] = (tokens, now)
            return False
        _buckets[key] = (max(-float(RATE), tokens - cost), now)
        return True


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _read_tokens(home):
    path = Path(home) / TOKENS
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict) or not isinstance(value.get("tokens"), dict):
        raise ValueError(f"{path} is malformed")
    return value["tokens"]


def _write_tokens(home, tokens):
    path = Path(home) / TOKENS
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = path.with_name(f".{TOKENS}.{os.getpid()}")
    descriptor = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump({"version": 1, "tokens": tokens}, stream, indent=2, sort_keys=True)
    os.replace(partial, path)


def issue(home, machine_id):
    """A new token for a registered machine; any earlier one stops working."""
    machine = machines.resolve(home, machine_id)
    assert machine is not None
    tokens = _read_tokens(home)
    token = secrets.token_urlsafe(32)
    _write_tokens(home, {**tokens, machine.id: _digest(token)})
    return token


def revoke(home, machine_id):
    tokens = _read_tokens(home)
    if machine_id not in tokens:
        raise ValueError(f"No worker token for {machine_id}")
    del tokens[machine_id]
    _write_tokens(home, tokens)


def authenticate(home, header):
    """The enabled, registered machine whose token ``header`` carries; None otherwise."""
    if not isinstance(header, str) or not header.startswith("Bearer "):
        return None
    token = header.removeprefix("Bearer ")
    if not TOKEN.fullmatch(token):
        return None
    offered = _digest(token)
    found = None
    # Every entry is compared, so the time taken says nothing about which one matched.
    for machine_id, digest in _read_tokens(home).items():
        if isinstance(digest, str) and secrets.compare_digest(digest, offered):
            found = machine_id
    if found is None:
        return None
    machine = machines.load(home).get(found)
    return machine if machine is not None and machine.enabled else None


def _text(value, what, limit=4096, pattern=None, prompt=False):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"Invalid {what}")
    if (PROMPT_CONTROL if prompt else CONTROL).search(value):
        raise ValueError(f"Invalid {what}: control characters")
    if pattern is not None and not pattern.fullmatch(value):
        raise ValueError(f"Invalid {what}")
    return value


def _optional(value, *args, **kwargs):
    return None if value is None else _text(value, *args, **kwargs)


def _absolute(value, what):
    value = _text(value, what)
    if not value.startswith("/"):
        raise ValueError(f"Invalid {what}: not an absolute path")
    return value


def _count(value, what, limit):
    if type(value) is not int or not 0 < value <= limit:
        raise ValueError(f"Invalid {what}")
    return value


REGISTER = {
    "pr",
    "repo",
    "ci_repo",
    "branch",
    "cwd",
    "session",
    "agent",
    "claude_account",
    "pane",
    "rollout",
    "instructions",
    "on_green",
    "poll_seconds",
    "max_repairs",
    "repair_timeout",
    "config_dir",
}


def register_args(home, machine, request):
    """``pr_supervisor.register``'s arguments from a worker's request, checked strictly."""
    if not isinstance(request, dict) or set(request) != REGISTER:
        raise ValueError("Expected exactly the registration fields")
    agent = request["agent"]
    if agent not in {"codex", "claude"}:
        raise ValueError("Invalid agent")
    branch = _optional(request["branch"], "branch", 200, BRANCH)
    # A branch watch has no PR; anything else names its own, never "auto".
    pr = "auto" if branch and request["pr"] == "auto" else _text(request["pr"], "PR", 300, PR)
    repo = _optional(request["repo"], "repo", 200, machines.REPO)
    ci_repo = request["ci_repo"]
    if ci_repo != "head":
        ci_repo = _optional(ci_repo, "CI repo", 200, machines.REPO)
    return Namespace(
        home=home,
        machine=machine.id,
        pr=pr,
        repo=repo,
        ci_repo=ci_repo,
        branch=branch,
        cwd=_absolute(request["cwd"], "cwd"),
        session=_text(request["session"], "session", 36, UUID),
        agent=agent,
        claude_account=_optional(request["claude_account"], "account", 64, machines.WORKSPACE),
        pane=_optional(request["pane"], "pane", 64, PANE),
        headless=request["pane"] is None,
        rollout=None if request["rollout"] is None else _absolute(request["rollout"], "rollout"),
        instructions=_text(request["instructions"], "instructions", 20000, prompt=True),
        on_green=_optional(request["on_green"], "continuation", 20000, prompt=True),
        codex_command=None,
        claude_command=None,
        poll_seconds=max(MIN_POLL, _count(request["poll_seconds"], "poll interval", 86400)),
        max_repairs=_count(request["max_repairs"], "repair budget", 100),
        repair_timeout=_count(request["repair_timeout"], "repair timeout", 86400),
        config_dir=(
            None
            if request["config_dir"] is None
            else _absolute(request["config_dir"], "config dir")
        ),
    )


HANDOFF = {"id", "pane", "timeout", "agent", "session", "cwd", "config_dir"}


def handoff_args(machine, request):
    """``herdr_handoff.schedule``'s arguments from a worker's request, checked strictly.

    The conversation and worktree that asked, which must be the watch's own.
    """
    if not isinstance(request, dict) or set(request) != HANDOFF:
        raise ValueError("Expected exactly the handoff fields")
    if request["agent"] not in {"codex", "claude"}:
        raise ValueError("Invalid agent")
    return Namespace(
        id=_text(request["id"], "watch", 16, WATCH),
        machine=machine,
        pane=_text(request["pane"], "pane", 64, PANE),
        timeout=_count(request["timeout"], "timeout", MAX_HANDOFF_WAIT),
        max_workers=2,
        agent=request["agent"],
        session=_text(request["session"], "session", 36, UUID).lower(),
        cwd=_absolute(request["cwd"], "cwd"),
        config_dir=(
            None
            if request["config_dir"] is None
            else _absolute(request["config_dir"], "config dir")
        ),
    )


def handle(home, machine, action, request):
    """Answer one worker request for ``machine``, the one its token names.

    Limits apply per machine and worktree, so one sandbox cannot hold up the others.
    """
    import pr_supervisor

    cwd = request.get("cwd") if isinstance(request, dict) else None
    key = f"{machine.id}\0{cwd if isinstance(cwd, str) and len(cwd) <= 4096 else ''}"
    with _busy_lock:
        if len(_busy) > MAX_KEYS:
            for name in [k for k, lock in _busy.items() if not lock.locked()]:
                del _busy[name]
        busy = _busy.setdefault(key, threading.Lock())
    if not busy.acquire(blocking=False):
        raise ValueError(f"Another request from this worktree on {machine.label} is running")
    try:
        if action not in ACTIONS:
            raise ValueError("Unknown worker action")
        if not _take(key):
            raise RateLimited(f"Too many requests from {machine.label}; wait a minute")
        try:
            if action == "handoff":
                args = handoff_args(machine, request)
            else:
                args = register_args(home, machine, request)
            db = pr_supervisor.open_db(home)
            try:
                if action == "handoff":
                    import herdr_handoff

                    return herdr_handoff.schedule(db, home, args)
                job = pr_supervisor.register(db, args)
            finally:
                db.close()
        except Exception:
            _take(key, FAILURE_COST, force=True)
            raise
        return {
            "id": job["id"],
            "status": job["status"],
            "session": job["session_id"],
            "machine": job["machine"],
            # Run on the controller, once the original agent has exited.
            "release_argv": pr_supervisor.release_argv(home, job["id"]),
        }
    finally:
        busy.release()


# The worker side -------------------------------------------------------------


def worker_file():
    return Path.home() / ".config" / "babysit-pr" / "worker.json"


def on_worker():
    """Whether this is a worker: provisioning's mark, or a launch's variable."""
    return bool(os.environ.get("BABYSIT_WORKER")) or worker_file().exists()


def controller_url(value):
    """A controller dashboard URL: http(s), a host, no credentials, query or fragment."""
    if not isinstance(value, str) or len(value) > 512 or CONTROL.search(value):
        return None
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError:
        return None
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.query
        or parts.fragment
    ):
        return None
    return value.rstrip("/")


def controller():
    """The controller's dashboard URL and this worker's token, from ``worker.json``."""
    path = worker_file()
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        value = {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path} is unreadable: {exc}") from exc
    url = controller_url(value.get("controller")) if isinstance(value, dict) else None
    token = value.get("token") if isinstance(value, dict) else None
    if url is None or not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ValueError(
            "Registering or handing off a watch here needs the controller's URL and token in "
            f"{path} (provision_worker.sh --controller; see references/worker-setup.md)"
        )
    return url, token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect would carry the token to wherever it points.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def forward(action, payload, timeout=FORWARD_TIMEOUT):
    """The controller's answer to ``payload``: through this sandbox's registration broker
    (``worker_broker``) inside babysit-bwrap, else posted with ``worker.json``'s token."""
    path = os.environ.get(SOCKET_ENV)
    if not path:
        return post(action, payload, timeout)
    try:
        with socket.socket(socket.AF_UNIX) as broker:
            broker.settimeout(timeout)
            broker.connect(path)
            broker.sendall(json.dumps({**payload, "action": action}).encode())
            broker.shutdown(socket.SHUT_WR)
            data = b""
            while chunk := broker.recv(65536):
                data += chunk
                if len(data) > MAX_ANSWER:
                    raise ValueError("The registration broker's answer is too large")
        answer = json.loads(data)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot reach this sandbox's registration broker: {exc}") from exc
    if not isinstance(answer, dict):
        raise RuntimeError("The registration broker answered no JSON object")
    if "error" in answer:
        raise ValueError(str(answer["error"])[:2000])
    return answer


def post(action, payload, timeout=FORWARD_TIMEOUT):
    """Send ``payload`` to the controller's ``/api/worker/<action>``; its JSON answer."""
    url, token = controller()
    request = urllib.request.Request(
        f"{url}/api/worker/{action}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    # No proxy from the environment sees the token either.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            answer = json.loads(response.read(MAX_ANSWER))
    except urllib.error.HTTPError as exc:
        try:
            detail = str(json.loads(exc.read(MAX_ANSWER))["error"])
        except (OSError, ValueError, KeyError, TypeError):
            detail = f"HTTP {exc.code}"
        raise ValueError(f"The controller refused: {detail[:1000]}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"Cannot reach the controller at {url}: {reason}") from exc
    if not isinstance(answer, dict):
        raise RuntimeError("The controller answered no JSON object")
    return answer


def registration(args):
    """The request a worker's ``register`` sends: its own paths, a named PR."""
    import gh_pr_watch as watch

    if getattr(args, "machine", None):
        raise ValueError("The controller names this worker from its token; drop --machine")
    if args.codex_command or args.claude_command:
        raise ValueError("Repairs on a worker always run in its own sandbox launcher")
    cwd = os.path.realpath(args.cwd)
    session = args.session or (os.environ.get("CODEX_THREAD_ID") if args.agent == "codex" else None)
    if not session:
        raise ValueError("Supply the exact --session UUID")
    pr = args.pr
    if pr == "auto" and not args.branch:
        # The controller cannot look in this checkout; this worker's gh names its PR.
        pr = watch.resolve_pr("auto", repo_override=args.repo, cwd=cwd)["url"]
    return {
        "pr": pr,
        "repo": args.repo,
        "ci_repo": args.ci_repo,
        "branch": args.branch,
        "cwd": cwd,
        "session": session,
        "agent": args.agent,
        "claude_account": args.claude_account,
        "pane": args.pane,
        "rollout": os.path.realpath(args.rollout) if args.rollout else None,
        "instructions": args.instructions,
        "on_green": args.on_green,
        "poll_seconds": args.poll_seconds,
        "max_repairs": args.max_repairs,
        "repair_timeout": args.repair_timeout,
        # A sandbox's broker names its own; registered from outside one, there is none.
        "config_dir": None,
    }


def handoff_request(args):
    """The request a worker's ``handoff`` sends: the conversation and worktree asking.

    The controller compares them with the watch's own; the token decides the machine.
    """
    asking = [
        (agent, os.environ[name])
        for agent, name in (("claude", "CLAUDE_CODE_SESSION_ID"), ("codex", "CODEX_THREAD_ID"))
        if os.environ.get(name)
    ]
    if not asking:
        raise ValueError("Schedule the handoff from the registered Codex or Claude conversation")
    agent, session = asking[0]
    return {
        "id": args.id,
        "pane": args.pane,
        "timeout": args.timeout,
        "agent": agent,
        "session": session,
        "cwd": os.path.realpath(os.getcwd()),
        # A sandbox's broker names its own; asked from outside one, there is none.
        "config_dir": None,
    }


def print_token(token, stream=None):
    stream = stream or sys.stdout
    if stream.isatty():
        raise ValueError(
            "Pipe the token into the worker's provision_worker.sh --controller; "
            "it is not printed to a terminal"
        )
    stream.write(token + "\n")
    stream.flush()
