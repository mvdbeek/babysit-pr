"""Run per-PR review sessions with bounded concurrency, and clean up finished ones.

usage:
  scheduler.py cleanup [run-dir] [--dry-run]   remove review checkouts, in every repository, whose PR
                                               is merged or closed, and tag the ones that stay
  scheduler.py run <run-dir> <label> [--dry-run]   review every PR key in <run-dir>/todo.txt

PR keys, checkout names and paths are in prkeys.py: `planemo#1612` reviews in
~/src/worktrees/planemo/review-planemo-pr-1612, a bare `24191` (galaxy) in
~/src/worktrees/galaxy/review-pr-24191.

`run` expects <run-dir>/prs.json, <run-dir>/todo.txt (one PR key per line) and
<run-dir>/briefs/<checkout name>.md (mkbrief.py). Each checkout is created by
`wtpr -r <repo> --name <checkout name> --tag review <PR URL>`, so the dashboard links it to the
PR and shows it under the `review` tag. Checkouts are kept after the review so the session can
be resumed for follow-up questions; only the agent and its herdr workspace go away. A re-review
replaces the checkout at the same path, so the dashboard still finds the earlier sessions'
transcripts there.

Neither command touches a checkout the user is working in (an open workspace, uncommitted
changes or local commits; see prkeys.held): `run` logs `held` and starts nothing for that PR,
`cleanup` logs `cleanup-held` and leaves it.

Progress goes to <run-dir>/scheduler.log and <run-dir>/sessions.tsv (pr key, reviewed head,
session id, checkout). Log lines to look for: `launch <key> <base> <exit code>`, `held`, `skip`,
`dirty`, `slow`, `noreport` (the reviewer's workspace is gone and it wrote no report),
`timeout` (no report after 105 minutes; the reviewer is left as it is), `tag … <nonzero>` and,
at the end, `ALL DONE`. Use a new run directory for every run: the log is appended to.
"""

import concurrent.futures
import json
import os
import shlex
import sys
import time

import prkeys
from prkeys import TAG

GRACE = 90  # seconds between the report appearing and treating the agent as finished
EXIT_WAIT = 900  # how long to leave exiting to the dashboard before closing the workspace
SLOW = 5400
TIMEOUT = 6300  # stop waiting for a reviewer that has written no report; it is left as it is


def sh(cmd):
    return prkeys.sh("/bin/sh", "-c", cmd)


def close_workspace(label, ws=None):
    w = (ws if ws is not None else prkeys.herdr_workspaces() or {}).get(label)
    if w:
        return sh(f"herdr workspace close {w['workspace_id']}").returncode
    return None


def tag(action, paths):
    """Add or remove the review tag on checkouts; returns wt's exit code and complaint."""
    if not paths:
        return 0, ""
    r = sh(f"wt tag {action} {TAG} {' '.join(shlex.quote(p) for p in paths)}")
    return r.returncode, r.stderr.strip()[:200]


def remove_checkout(pr):
    """Drop the checkout, its branch and its tag. Transcripts stay under ~/.claude*/projects."""
    close_workspace(pr.name)
    time.sleep(2)
    r = sh(f"git -C {pr.clone} worktree remove --force {pr.checkout}")
    sh(f"git -C {pr.clone} branch -D {pr.name}")
    return r.returncode, r.stderr.strip()[:200]


def cleanup(log, dry_run=False):
    ws = prkeys.herdr_workspaces()
    kept = []

    def state(pr):
        try:
            return json.loads(
                prkeys.gh("pr", "view", str(pr.n), "-R", pr.slug, "--json", "state,headRefOid")
            )
        except (RuntimeError, ValueError) as exc:
            return exc

    checkouts = prkeys.review_checkouts()
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        states = list(pool.map(state, [pr for pr, _ in checkouts]))
    for (pr, path), info in zip(checkouts, states, strict=True):
        if isinstance(info, Exception):
            log("cleanup-error", pr.key, str(info)[:200])
            kept.append(path)
            continue
        if info["state"] not in ("MERGED", "CLOSED"):
            kept.append(path)
            continue
        reason = prkeys.held(pr, ws, heads=[info["headRefOid"]])
        if reason:
            log("cleanup-held", pr.key, info["state"], reason)
            kept.append(path)
            continue
        if dry_run:
            print(f"would remove {pr.key} ({info['state']}): {path}")
            continue
        log("cleanup", pr.key, info["state"], *remove_checkout(pr))
        if not os.path.lexists(path):
            tag("remove", [path])
    if dry_run:
        print(f"would tag {len(kept)} checkouts that stay with `{TAG}`")
        return
    # Checkouts made before the scheduler passed --tag, or re-created by hand, have no tag yet.
    log("tag", f"{len(kept)} checkouts", *tag("add", kept))


def run(run_dir, label, log, dry_run=False):
    prs = {prkeys.entry(p).key: p for p in json.load(open(f"{run_dir}/prs.json"))}
    todo = list(dict.fromkeys(prkeys.parse(x) for x in open(f"{run_dir}/todo.txt").read().split()))
    missing = [pr.key for pr in todo if pr.key not in prs]
    if missing:
        sys.exit(f"todo.txt keys missing from prs.json: {' '.join(missing)}")
    nobrief = [pr.key for pr in todo if not os.path.exists(f"{run_dir}/briefs/{pr.name}.md")]
    if nobrief:
        sys.exit(f"no brief for: {' '.join(nobrief)} (run mkbrief.py)")

    wtpr_help = sh("wtpr -h")
    wtpr_help = wtpr_help.stdout + wtpr_help.stderr
    exit_flag = "--exit-when-done" in wtpr_help
    tag_flag = "--tag" in wtpr_help
    log("exit-when-done", "supported" if exit_flag else "not supported; closing workspaces here")
    log(
        "tag-flag",
        "supported" if tag_flag else "not supported; tagging with `wt tag add` after each launch",
    )

    def report(pr):
        return os.path.exists(f"{prkeys.out()}/{pr.report_file(label)}")

    def command(pr):
        flags = ("--exit-when-done " if exit_flag else "") + (f"--tag {TAG} " if tag_flag else "")
        return (
            f"cd {shlex.quote(run_dir)} && wtpr -r {pr.repo} --name {pr.name} --no-focus {flags}"
            f"-F briefs/{pr.name}.md {pr.url}"
        )

    if dry_run:
        ws = prkeys.herdr_workspaces()
        for pr in todo:
            problem = prkeys.clone_problem(pr) or prkeys.held(
                pr, ws, heads=[prs[pr.key]["headRefOid"]]
            )
            print(
                f"# {pr.key}: {'would not launch: ' + problem if problem else 'replaces ' + pr.checkout if os.path.lexists(pr.checkout) else 'new checkout'}"
            )
            print(command(pr))
        return

    def finish(pr):
        # The head the reviewer had: the PR may have moved between the listing and the launch.
        head = (
            sh(f"git -C {pr.checkout} rev-parse HEAD").stdout.strip() or prs[pr.key]["headRefOid"]
        )
        with open(f"{run_dir}/sessions.tsv", "a") as f:
            f.write(f"{pr.key}\t{head[:11]}\t{prkeys.session_file(pr)[0]}\t{pr.checkout}\n")
        finished.add(pr)

    started: set[prkeys.PR] = set()
    finished: set[prkeys.PR] = set()
    warned: set[prkeys.PR] = set()
    started_at: dict[prkeys.PR, float] = {}
    reported_at: dict[prkeys.PR, float] = {}
    while True:
        answered = prkeys.herdr_workspaces()
        ws = answered or {}
        for pr in sorted(started - finished):
            if report(pr) and pr not in reported_at:
                reported_at[pr] = time.time()
                log("report", pr.key)
            if pr in reported_at:
                waited = time.time() - reported_at[pr]
                if waited < GRACE:
                    continue
                if exit_flag and pr.name in ws and waited < EXIT_WAIT:
                    continue  # the dashboard exits the agent once it sees the marker
                if pr.name in ws:
                    log("close", pr.key, close_workspace(pr.name, ws))
                dirty = sh(f"git -C {pr.checkout} status --short").stdout.strip()
                if dirty:
                    log("dirty", pr.key, dirty.replace("\n", " | ")[:300])
                finish(pr)
                continue
            running = time.time() - started_at[pr]
            if answered is not None and pr.name not in ws and running > GRACE:
                # Nobody is left to write the report. The row lets track.py name it as MISSING.
                log("noreport", pr.key)
                finish(pr)
            elif running > TIMEOUT:
                log("timeout", pr.key, (ws.get(pr.name) or {}).get("agent_status"))
                finish(pr)
            elif running > SLOW and pr not in warned:
                warned.add(pr)
                log("slow", pr.key, (ws.get(pr.name) or {}).get("agent_status"))
        pending = [pr for pr in todo if pr not in started]
        while len(started - finished) < prkeys.settings()["parallel"] and pending:
            pr = pending.pop(0)
            started.add(pr)
            started_at[pr] = time.time()
            problem = prkeys.clone_problem(pr)
            if problem:
                log("skip", pr.key, problem)
                finished.add(pr)
                continue
            if os.path.lexists(pr.checkout):
                reason = prkeys.held(pr, answered, heads=[prs[pr.key]["headRefOid"]])
                if reason:
                    log("held", pr.key, reason)
                    finished.add(pr)
                    continue
                log("replace", pr.key, *remove_checkout(pr))
            r = sh(command(pr))
            warn = [line for line in (r.stdout + r.stderr).splitlines() if line.startswith("wt")]
            log("launch", pr.key, prs[pr.key]["baseRefName"], r.returncode, " ".join(warn)[:200])
            head = sh(f"git -C {pr.checkout} rev-parse HEAD").stdout.strip()
            if head:  # also after a failed launch that left a checkout: it is ours, not the user's
                prkeys.record_launch(pr, head)
            if r.returncode:
                finished.add(pr)  # nothing to wait for; the log line says why
            elif not tag_flag:
                code, err = tag("add", [pr.checkout])
                if code:
                    log("tag", pr.key, code, err)
        if not pending and started == finished:
            log("ALL DONE")
            return
        time.sleep(60)


def main():
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry_run = "--dry-run" in sys.argv[1:]
    if not args or args[0] not in ("cleanup", "run") or (args[0] == "run" and len(args) != 3):
        sys.exit(__doc__)
    command, *args = args
    run_dir = args[0] if args else "."
    os.makedirs(run_dir, exist_ok=True)
    logfile = open(os.path.join(run_dir, "scheduler.log"), "a", buffering=1)

    def log(*parts):
        logfile.write(time.strftime("%H:%M:%S ") + " ".join(map(str, parts)) + "\n")

    # Read once, now: a run lasts hours, and the dashboard may restart meanwhile.
    prkeys.settings()
    prkeys.reviewed_heads()
    if command == "cleanup":
        cleanup(log, dry_run)
    else:
        run(args[0], args[1], log, dry_run)


if __name__ == "__main__":
    main()
