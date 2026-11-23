"""Record this run's finished reviews in the dashboard.

usage: track.py <run-dir> <label>

Records every row of <run-dir>/sessions.tsv (pr key, reviewed head, session id, checkout)
whose report exists, with POST /api/agent-reviews; the dashboard reads the verdict and
findings from the report. Recording a review again changes nothing, so running this again
is safe. A session without a report is printed as MISSING and is not recorded: that PR is
reviewed again next run.

For a review done by resuming a held session, add its line to sessions.tsv first
(pr key, reviewed head, session id, checkout; tab-separated), or use resume.py --record.
"""

import json
import os
import sys

import prkeys


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    run_dir, label = sys.argv[1:]
    prs = {prkeys.entry(p).key: p for p in json.load(open(os.path.join(run_dir, "prs.json")))}
    sessions = os.path.join(run_dir, "sessions.tsv")
    lines = open(sessions).read().splitlines() if os.path.exists(sessions) else []
    for line in lines:
        if not line.strip():
            continue
        key, sha, session, checkout = (line.split("\t") + ["", "", ""])[:4]
        pr = prkeys.parse(key)
        report = os.path.join(prkeys.out(), pr.report_file(label))
        if not os.path.exists(report):
            print(f"MISSING {pr.key}: no report {os.path.basename(report)}; not recorded")
            continue
        base = (prs.get(pr.key) or {}).get("baseRefName")
        review = prkeys.record(pr, sha, base, label, report, session, checkout)
        print(
            f"recorded {pr.key} {sha} {review.get('verdict') or '(no verdict found)'} {session or '(no session id)'}"
        )


if __name__ == "__main__":
    main()
