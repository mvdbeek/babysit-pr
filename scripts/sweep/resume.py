"""Ask a PR's reviewer session a question, or hand it a re-review brief, without a terminal.

usage: resume.py <pr key> [--session ID] [--record RUN-DIR LABEL] (-p TEXT | -F FILE)

Runs `claude -p --resume <session id>` in the PR's review checkout and prints the answer. The
session id comes from --session, else the PR's newest sweep review, else the newest transcript
recorded for the checkout. The reviewer's transcript may belong to another Claude account than
this process uses, so CLAUDE_CONFIG_DIR is set to the account that holds it. While the PR's
herdr workspace is open the session may be live in its pane, so the conversation is continued
as a fork (`--fork-session`): same history, a new session id, and no second writer on the
open one.

--record is for a re-review: when the report <label>-…pr<N>.md exists afterwards, a row (pr
key, the head the report names, session id, checkout) is appended to <run-dir>/sessions.tsv,
which track.py records in the dashboard.

When the checkout is gone, prints the commands that bring it back and exits 2. A resumed
reviewer can take many minutes: run this in the background with its output in a file and wait
for it, rather than in one blocking call.
"""

import argparse
import os
import re
import subprocess
import sys

import prkeys


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("key")
    ap.add_argument("--session")
    ap.add_argument("--record", nargs=2, metavar=("RUN-DIR", "LABEL"))
    text = ap.add_mutually_exclusive_group(required=True)
    text.add_argument("-p", "--prompt")
    text.add_argument("-F", "--prompt-file")
    args = ap.parse_args()
    pr = prkeys.parse(args.key)
    last = prkeys.last_review(pr)
    wanted = args.session or (last or {}).get("session") or ""
    session, home = prkeys.session_file(pr, wanted)
    if not session and not args.session:
        session, home = prkeys.session_file(
            pr
        )  # the recorded session has no transcript left: take the newest
    if not os.path.isdir(pr.checkout):
        sha = (last or {}).get("head") or "<reviewed sha>"
        print(
            f"{pr.checkout} is gone. Recreate it, then run this again:\n"
            f"  wtpr -r {pr.repo} --name {pr.name} --no-focus --tag {prkeys.TAG} {pr.url}\n"
            f"  git -C {pr.checkout} reset --hard {sha}",
            file=sys.stderr,
        )
        sys.exit(2)
    if not session:
        sys.exit(
            f"no reviewer session found for {pr.key}"
            + (f" (looked for {wanted})" if wanted else "")
        )
    prompt = args.prompt if args.prompt is not None else open(args.prompt_file).read()
    env = dict(os.environ)
    if os.path.realpath(home) == os.path.realpath(os.path.expanduser("~/.claude")):
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = home
    workspaces = prkeys.herdr_workspaces()
    fork = workspaces is None or pr.name in workspaces
    command = ["claude", "-p", "--resume", session] + (["--fork-session"] if fork else [])
    print(
        f"[resuming {session} in {pr.checkout}{' as a fork' if fork else ''}]",
        file=sys.stderr,
        flush=True,
    )
    code = subprocess.run(command, input=prompt, text=True, cwd=pr.checkout, env=env).returncode
    if fork:
        session = prkeys.session_file(pr)[0] or session  # the fork is the newest transcript there
    if args.record:
        run_dir, label = args.record
        report = os.path.join(prkeys.out(), pr.report_file(label))
        if os.path.exists(report):
            named = re.search(r"Reviewed head:[\s`*]*([0-9a-f]{7,40})", open(report).read())
            head = (
                named.group(1)
                if named
                else prkeys.sh("git", "-C", pr.checkout, "rev-parse", "FETCH_HEAD").stdout.strip()
            )
            with open(os.path.join(run_dir, "sessions.tsv"), "a") as f:
                f.write(f"{pr.key}\t{head[:11]}\t{session}\t{pr.checkout}\n")
            print(f"[recorded {pr.key} {head[:11]} {session}]", file=sys.stderr)
        else:
            print(f"[no report {report}: nothing recorded]", file=sys.stderr)
    sys.exit(code)


if __name__ == "__main__":
    main()
