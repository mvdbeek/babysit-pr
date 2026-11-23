"""Check that the run posted nothing to GitHub as the user.

usage: posted.py <run-dir> [pr key ...]

For every PR this run started or resumed a reviewer for (todo.txt, sessions.tsv and the held
PRs of plan.json; or just the keys given), lists issue comments, reviews and review comments by
the authenticated user, in the PR's own repository, created or submitted since
<run-dir>/start_utc.txt (UTC). Prints one line per post, or `NONE since <start>`.
A post is not proof the run made it: the user may have posted by hand. Read that reviewer's
transcript before reporting it.
"""

import json
import os
import sys

import prkeys


def posts(pr, me, start):
    found = []
    for kind, path, when in (
        (
            "issue comment",
            f"repos/{pr.slug}/issues/{pr.n}/comments?since={start}&per_page=100",
            "created_at",
        ),
        (
            "review comment",
            f"repos/{pr.slug}/pulls/{pr.n}/comments?since={start}&per_page=100",
            "created_at",
        ),
        ("review", f"repos/{pr.slug}/pulls/{pr.n}/reviews?per_page=100", "submitted_at"),
    ):
        pages = json.loads(prkeys.gh("api", path, "--paginate", "--slurp"))
        for item in (i for page in pages for i in page):
            # `since` filters on the update time, so compare the creation time here.
            if (item.get("user") or {}).get("login") == me and (item.get(when) or "") >= start:
                found.append(f"{pr.key}\t{kind}\t{item[when]}\t{item.get('html_url', '')}")
    return found


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    run_dir, keys = sys.argv[1], sys.argv[2:]
    start = open(os.path.join(run_dir, "start_utc.txt")).read().strip()
    if not keys:
        todo, sessions = os.path.join(run_dir, "todo.txt"), os.path.join(run_dir, "sessions.tsv")
        if os.path.exists(todo):
            keys += open(todo).read().split()
        if os.path.exists(sessions):
            keys += [
                line.split("\t")[0] for line in open(sessions).read().splitlines() if line.strip()
            ]
        plan = os.path.join(run_dir, "plan.json")
        if os.path.exists(plan):
            keys += [h["key"] for h in json.load(open(plan))["held"]]
    me = prkeys.gh("api", "user", "--jq", ".login").strip()
    found = []
    for pr in sorted({prkeys.parse(k) for k in keys}):
        found += posts(pr, me, start)
    checked = sorted({prkeys.parse(k) for k in keys})
    print(
        "\n".join(found)
        if found
        else f"NONE since {start} by {me} on {len(checked)} PRs: {' '.join(pr.key for pr in checked)}"
    )


if __name__ == "__main__":
    main()
