"""Work out what this run should review, and in which order.

usage: collect.py <run-dir> [--label LABEL] [--team-requests] [--draft-requests]

Two sources:
  - every open non-draft PR of the settings' `repos` (of `owner`);
  - with `review_requests`, every open PR, in any repository on GitHub, where the user's
    review is requested.
    Requests to a team the user is in count only with --team-requests, and drafts only with
    --draft-requests; what is left out is listed in plan.json.

A PR is skipped when the dashboard has a sweep review of its current head (matched by prefix).

Writes into <run-dir>:
  start_utc.txt  when the run started (kept if it is already there)
  prs.json       every open PR considered: the `gh pr list` fields plus "owner", "repo" and
                 "review_requested"
  queue.txt      PR keys that can get a fresh review session, most important first
  plan.json      the run label, the queue, and everything left out with the reason:
                 "held" (an existing checkout that must not be replaced: resume its session
                 instead), "drafts", "unreviewable" (no writable clone), "team_requests",
                 and "errors" (a repository or search GitHub did not answer for; the run
                 goes on without it)
and prints a summary. Queue order: review requests, then the swept repositories, then the
settings' `later_repos`, with PRs opened by the settings' `bots` after the others of their
group; most recently updated first within each group.
"""

import argparse
import concurrent.futures
import glob
import json
import os
import re
import time

import prkeys

FIELDS = (
    "number,headRefOid,baseRefName,author,title,additions,deletions,changedFiles,isDraft,updatedAt"
)
LIST_LIMIT = 1000


def next_label(date):
    """`<date>` for the day's first run, then `<date>-run2`, `-run3`, ... by the files already in the report folder."""
    runs = []
    for path in glob.glob(f"{prkeys.out()}/{date}*"):
        m = re.match(rf"{date}(?:-run(\d+))?-", os.path.basename(path))
        if m:
            runs.append(int(m.group(1) or 1))
    return date if not runs else f"{date}-run{max(runs) + 1}"


def sweep(repo):
    """The open non-draft PRs of one swept repository, or the error that kept them from being listed."""
    slug = f"{prkeys.owner()}/{repo}"
    try:
        prs = json.loads(
            prkeys.gh(
                "pr",
                "list",
                "-R",
                slug,
                "--state",
                "open",
                "--draft=false",
                "--limit",
                str(LIST_LIMIT),
                "--json",
                FIELDS,
            )
        )
    except (RuntimeError, ValueError) as exc:
        return exc
    for p in prs:
        p.update(owner=prkeys.owner(), repo=repo, review_requested=False)
    return prs


def requested(qualifier):
    """Open PRs anywhere on GitHub with a review request for the user (`user-review-requested`)
    or for the user or one of their teams (`review-requested`)."""
    out = prkeys.gh(
        "api", "-X", "GET", "search/issues", "--paginate",
        "-f", f"q=is:pr is:open archived:false {qualifier}:@me",
        "-f", "per_page=100", "--jq", ".items[].html_url",
    )  # fmt: skip
    return [prkeys.from_url(url) for url in out.split()]


def attempt(errors, what, fn, *args, default):
    """fn's result; a failure is recorded so that one bad answer from GitHub does not end the run."""
    try:
        return fn(*args)
    except (RuntimeError, ValueError) as exc:
        errors.append(f"{what}: {str(exc).splitlines()[0][:300]}")
        return default


def details(pr):
    """The list fields of one PR, or the error that kept them from being read."""
    try:
        p = json.loads(prkeys.gh("pr", "view", pr.url, "--json", FIELDS))
    except RuntimeError as exc:
        return {"key": pr.key, "error": str(exc)}
    p.update(owner=pr.owner, repo=pr.repo, review_requested=True)
    return p


def group(p):
    if p["review_requested"]:
        return 0
    settings = prkeys.settings()
    later = p["repo"] in settings.get("later_repos", [])
    bot = p["author"].get("login") in settings.get("bots", [])
    return 1 + 2 * later + bot


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run_dir")
    ap.add_argument(
        "--label",
        help="run label for report and index names (default: the next free one for today)",
    )
    ap.add_argument(
        "--team-requests",
        action="store_true",
        help="also take review requests made to a team the user is in",
    )
    ap.add_argument(
        "--draft-requests",
        action="store_true",
        help="also review drafts the user's review is requested on",
    )
    args = ap.parse_args()

    os.makedirs(args.run_dir, exist_ok=True)
    start_file = os.path.join(args.run_dir, "start_utc.txt")
    if not os.path.exists(start_file):
        with open(start_file, "w") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ\n", time.gmtime()))
    start = open(start_file).read().strip()
    date = time.strftime("%Y-%m-%d")
    label = args.label or next_label(date)

    queue, held, drafts, unreviewable, errors = [], [], [], [], []
    swept = prkeys.settings().get("repos", [])
    asks = prkeys.settings().get("review_requests", True)
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        lists = list(pool.map(sweep, swept))
        for repo, listed in zip(swept, lists, strict=True):
            if isinstance(listed, Exception):
                errors.append(
                    f"{prkeys.owner()}/{repo} was not listed: {str(listed).splitlines()[0][:300]}"
                )
        lists = [[] if isinstance(listed, Exception) else listed for listed in lists]
        direct = (
            attempt(
                errors,
                "review requests were not searched",
                requested,
                "user-review-requested",
                default=[],
            )
            if asks
            else []
        )
        with_teams = (
            attempt(
                errors,
                "team review requests were not searched",
                requested,
                "review-requested",
                default=direct,
            )
            if asks
            else []
        )
        prs = {prkeys.entry(p).key: p for prs in lists for p in prs}
        wanted = with_teams if args.team_requests else direct
        for p in pool.map(details, [pr for pr in wanted if pr.key not in prs]):
            if "error" in p:
                unreviewable.append({"key": p["key"], "reason": p["error"]})
            else:
                prs[prkeys.entry(p).key] = p
    for pr in wanted:
        if pr.key in prs:
            prs[pr.key]["review_requested"] = True
    # A team request on a swept PR is reviewed anyway; only the ones nothing else brings in are left out.
    left_out_teams = sorted({pr.key for pr in with_teams} - {pr.key for pr in wanted} - set(prs))

    workspaces = prkeys.herdr_workspaces()
    counts: dict[str, dict[str, int]] = {}
    for key, p in prs.items():
        pr = prkeys.entry(p)
        count = counts.setdefault(pr.slug, {"non_draft": 0, "already_reviewed": 0})
        count["non_draft"] += not p["isDraft"]
        if prkeys.reviewed(pr, p["headRefOid"]):
            count["already_reviewed"] += 1
        elif p["isDraft"] and not args.draft_requests:
            drafts.append(key)
        elif problem := prkeys.clone_problem(pr):
            unreviewable.append({"key": key, "reason": problem})
        elif reason := prkeys.held(pr, workspaces, heads=[p["headRefOid"]]):
            last = prkeys.last_review(pr)
            session = (last or {}).get("session") or ""
            held.append(
                {
                    "key": key,
                    "reason": reason,
                    "checkout": pr.checkout,
                    "session": session or prkeys.session_file(pr)[0],
                    "previous_head": (last or {}).get("head") or "",
                    "head": p["headRefOid"],
                }
            )
        else:
            queue.append(key)
    queue.sort(key=lambda k: prs[k]["updatedAt"], reverse=True)
    queue.sort(key=lambda k: group(prs[k]))

    plan = {
        "label": label,
        "date": date,
        "start_utc": start,
        "me": attempt(
            errors,
            "the GitHub login was not read",
            prkeys.gh,
            "api",
            "user",
            "--jq",
            ".login",
            default="",
        ).strip(),
        "errors": errors,
        "counts": counts,
        "list_capped": [
            f"{prkeys.owner()}/{r}"
            for r, prs_ in zip(swept, lists, strict=True)
            if len(prs_) >= LIST_LIMIT
        ],
        "review_requested": sorted(pr.key for pr in wanted),
        "queue": queue,
        "held": held,
        "drafts": sorted(drafts),
        "unreviewable": unreviewable,
        "team_requests": left_out_teams,
    }
    with open(os.path.join(args.run_dir, "prs.json"), "w") as f:
        json.dump(list(prs.values()), f, indent=1)
    with open(os.path.join(args.run_dir, "plan.json"), "w") as f:
        json.dump(plan, f, indent=1)
    with open(os.path.join(args.run_dir, "queue.txt"), "w") as f:
        f.write("".join(k + "\n" for k in queue))

    print(f"label {label}   start {start}   user {plan['me']}")
    for error in errors:
        print(f"ERROR {error}")
    for slug, c in sorted(counts.items()):
        print(
            f"  {slug}: {c['non_draft']} open non-draft, {c['already_reviewed']} already reviewed at their head"
        )
    for slug in plan["list_capped"]:
        print(f"  WARNING {slug}: the list reached --limit {LIST_LIMIT}; some PRs were not seen")
    print(
        f"review requested on {len(wanted)} open PRs ({len(left_out_teams)} more through teams, left out)"
    )
    print(f"queue ({len(queue)}): {' '.join(queue)}")
    for h in held:
        print(f"held {h['key']}: {h['reason']}; session {h['session'] or 'none found'}")
    if drafts:
        print(
            f"drafts with a review request, not reviewed ({len(drafts)}): {' '.join(sorted(drafts))}"
        )
    reasons: dict[str, list[str]] = {}
    for u in unreviewable:
        reasons.setdefault(u["reason"], []).append(u["key"])
    for reason, keys in reasons.items():
        print(f"unreviewable ({len(keys)}), {reason}: {' '.join(keys)}")


if __name__ == "__main__":
    main()
