"""Agents' reviews: the store, recording, and imports from temporary folders and a stand-in share."""

import json
import os
import types

import agent_reviews
import dufs_share
import pytest
import workspace_artefacts as wa

SETTINGS = {"owner": "galaxyproject", "default_repo": "galaxy"}
REPORT = """# Review: galaxyproject/galaxy#25013

- PR: https://github.com/galaxyproject/galaxy/pull/25013
- Title: Do not percent-encode the slashes of a tool ID (@someone)
- Base: `release_26.2`
- Reviewed head: `c44befd502b4863c199f7c2b02379412b0bfa303` (1 commit)
- Verdict: **approve, nits**. The fix is correct and small, and its test fails without it.

## Summary of the change
- A bullet.

## Findings

### Major
**M1. Data is lost when the queue restarts.**
`lib/queue.py:12` ...

### Nit
**N1. The `renderPanelBody` parameter is unnecessary, and its comment is wrong.**
**n2.** The comment says the stub renders nothing. It renders the slot.
- **N3 (forward-merge conflict)** on `dev`.

## What I checked
**1. Not a finding.**
"""
OLD_REPORT = """# Review: galaxyproject/galaxy PR #24191

PR: https://github.com/galaxyproject/galaxy/pull/24191

- **Base:** `release_25.1`
- **Reviewed head:** `101b6081a1170f745c3e1bb3df43e796f18d9079`

## Verdict

Correct and minimal. No **blockers** or majors.

## Findings

### minor — the only `ModelCache` round-trip test is never collected (pre-existing)
Body.

### nit — `TOOL_SHED_TEST_DBPATH` still shares the cache dir
Body.
"""
VERIFIED = """### P1 1: Data loss when the queue restarts

Body.

### P3 1: A typo (verified)
"""
TRIMMED = "### P3 1: A typo (verified)\n"
REPLY = """<details><summary>Risks</summary>

Risks are **minimal**: this change is a two-way door.

Second paragraph.

</details>
"""
META = (
    "- PR: https://github.com/o/r/pull/5\n"
    "- head (reviewed commit): b54743e250148303de57ab2a73b3a89fb6efd530\n"
)


def write(folder, files, modified=None):
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (folder / name).write_text(text)
        if modified:
            os.utime(folder / name, (modified, modified))


def reviews(tmp_path, artefacts=None, out=None, **options):
    settings = {**SETTINGS, "out": str(out or tmp_path / "out")}
    return agent_reviews.Reviews(
        tmp_path / "dashboard",
        artefacts,
        settings=lambda: settings,
        user_home=tmp_path / "home",
        **options,
    )


def test_a_sweep_report_gives_its_verdict_and_findings():
    parsed = agent_reviews.parse_report(REPORT)
    assert parsed["repo"] == "galaxyproject/galaxy" and parsed["number"] == 25013
    assert parsed["head"].startswith("c44befd5") and parsed["base"] == "release_26.2"
    assert parsed["verdict"] == "approve, nits"
    assert parsed["note"] == "The fix is correct and small, and its test fails without it."
    assert parsed["findings"] == [
        {"severity": "Major", "title": "Data is lost when the queue restarts"},
        {
            "severity": "Nit",
            "title": "The `renderPanelBody` parameter is unnecessary, and its comment is wrong",
        },
        {"severity": "Nit", "title": "The comment says the stub renders nothing"},
        {"severity": "Nit", "title": "(forward-merge conflict)"},
    ]


def test_an_older_sweep_report_with_findings_as_headings():
    parsed = agent_reviews.parse_report(OLD_REPORT)
    assert parsed["head"].startswith("101b6081") and parsed["base"] == "release_25.1"
    assert parsed["note"] == "Correct and minimal. No blockers or majors."
    assert [f["severity"] for f in parsed["findings"]] == ["Minor", "Nit"]
    assert parsed["findings"][0]["title"].startswith("the only `ModelCache` round-trip test")


def test_the_sweeps_old_tracker_is_imported_once_and_again_when_it_changes(tmp_path):
    out = tmp_path / "artefacts" / "outbox" / "galaxy-pr-reviews"
    write(out, {"2026-11-23-pr25013.md": REPORT, "2026-10-17-pr24191.md": OLD_REPORT})
    tracker = out / "reviewed.tsv"
    tracker.write_text(
        "# pr\thead_sha\tbase\treviewed_on\treport\n"
        "24191\t101b6081a11\trelease_25.1\t2026-10-17\toutbox/galaxy-pr-reviews/2026-10-17-pr24191.md\n"
        "planemo#9\tabcdef12345\tmaster\t2026-10-17\toutbox/galaxy-pr-reviews/gone.md\n"
    )
    store = reviews(tmp_path, out=out)
    found = store.for_pr("galaxyproject/galaxy", 24191)
    assert [r["head"] for r in found] == ["101b6081a11"]
    assert found[0]["label"] == "2026-10-17" and found[0]["source"] == "sweep"
    # A row whose report is gone still counts as reviewed at its head, without a summary.
    gone = store.latest()["galaxyproject/planemo#9"]
    assert gone["head"] == "abcdef12345" and gone["verdict"] is None and gone["links"] == {}
    with tracker.open("a") as handle:
        handle.write(
            "25013\tc44befd502b\trelease_26.2\t2026-11-23\t"
            "outbox/galaxy-pr-reviews/2026-11-23-pr25013.md\tsession-1\n"
        )
    store.refresh(force=True)
    review = store.for_pr("galaxyproject/galaxy", 25013)[0]
    assert review["verdict"] == "approve, nits" and review["session"] == "session-1"
    assert review["counts"] == [
        {"severity": "Major", "count": 1, "tone": "danger"},
        {"severity": "Nit", "count": 3, "tone": "muted"},
    ]
    assert store.heads() == {
        "galaxyproject/galaxy#24191": ["101b6081a11"],
        "galaxyproject/planemo#9": ["abcdef12345"],
        "galaxyproject/galaxy#25013": ["c44befd502b"],
    }


def sweep_review(out, name="2026-11-23-run2-pr25013.md", **changes):
    return {
        "source": "sweep",
        "repo": "galaxyproject/galaxy",
        "number": 25013,
        "head": "c44befd502b",
        "label": "2026-11-23-run2",
        "date": "2026-11-23",
        "report": str(out / name),
        "session": "s2",
        **changes,
    }


def test_the_sweep_records_a_review_read_from_its_report(tmp_path):
    out = tmp_path / "out"
    write(out, {"2026-11-23-run2-pr25013.md": REPORT})
    store = reviews(tmp_path, out=out)
    recorded = store.record(sweep_review(out))
    assert recorded["verdict"] == "approve, nits" and recorded["base"] == "release_26.2"
    assert len(recorded["findings"]) == 4
    # Recording the same report again replaces the row, not adds one.
    store.record(sweep_review(out, session="s3"))
    assert [r["session"] for r in store.for_pr("galaxyproject/galaxy", 25013)] == ["s3"]


def test_a_caller_cannot_choose_the_verdict(tmp_path):
    out = tmp_path / "out"
    write(out, {"2026-11-23-run2-pr25013.md": REPORT})
    with pytest.raises(ValueError, match="Unknown review fields: findings, verdict"):
        reviews(tmp_path, out=out).record(sweep_review(out, verdict="approve", findings=[]))


@pytest.mark.parametrize(
    "change,error",
    [
        ({"source": "pr-review"}, "Only the review sweep"),
        ({"repo": "../x"}, "repo"),
        ({"number": True}, "number"),
        ({"number": 2**63}, "number"),
        ({"head": "not a sha"}, "head"),
        ({"report": "relative.md"}, "report must be a file"),
    ],
)
def test_a_recorded_review_is_checked(tmp_path, change, error):
    out = tmp_path / "out"
    write(out, {"2026-11-23-run2-pr25013.md": REPORT})
    with pytest.raises(ValueError, match=error):
        reviews(tmp_path, out=out).record(sweep_review(out, **change))


def test_only_reports_in_the_sweeps_folder_are_read(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    secret = tmp_path / "secret.md"
    secret.write_text(REPORT)
    store = reviews(tmp_path, out=out)
    with pytest.raises(ValueError, match="not a report in the review sweep's report folder"):
        store.record(sweep_review(out, name="../secret.md"))
    # Nor through a link in the folder, to a file or to another folder.
    (out / "2026-11-23-pr1.md").symlink_to(secret)
    with pytest.raises(ValueError, match="not a report"):
        store.record(sweep_review(out, name="2026-11-23-pr1.md"))
    (out / "elsewhere").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="not a report"):
        store.record(sweep_review(out, name="elsewhere/secret.md"))
    assert store.latest() == {}


def test_answers_do_not_wait_for_an_import_in_progress(tmp_path):
    store = reviews(tmp_path)
    store.store.record({"source": "sweep", "repo": "o/r", "number": 1, "report": "/r.md"})
    with store.lock:  # Another request is importing.
        assert [r["number"] for r in store.for_pr("o/r", 1)] == [1]


def test_the_newest_pr_review_round_with_findings_and_its_latest_trim(tmp_path):
    folder = (
        tmp_path / "home" / ".cache" / "babysit-sandbox" / "k1" / "artifacts" / "pr-review-o-r-5"
    )
    write(
        folder,
        {
            "2026-11-20-r1-meta.md": META,
            "2026-11-20-r1-verified.md": "### P2 1: Old\n",
            "2026-11-22-r2-meta.md": META,
            "2026-11-22-r2-verified.md": VERIFIED,
            "2026-11-22-r2-reply.md": REPLY,
            "2026-11-23-r2-trim1-verified.md": TRIMMED,
            # An aborted round: no findings yet, so round 2 is still the latest.
            "2026-11-23-r3-meta.md": META,
        },
    )
    review = reviews(tmp_path).for_pr("o/r", 5)[0]
    assert review["source"] == "pr-review" and review["label"] == "r2 trim 1"
    assert review["date"] == "2026-11-23" and review["head"].startswith("b54743e2")
    assert review["findings"] == [{"severity": "P3", "title": "A typo", "tone": "muted"}]
    assert review["note"] == "Risks are minimal: this change is a two-way door."
    # On this machine's disk only, without a share: nothing to link to.
    assert review["links"] == {}


def test_a_self_review_of_a_branch_without_a_pr_is_left_out(tmp_path):
    folder = tmp_path / "home" / "src" / "agent-artefacts" / "outbox" / "pr-review-o-r-branch"
    write(
        folder,
        {"2026-11-22-r1-meta.md": "- PR: not opened yet\n", "2026-11-22-r1-verified.md": VERIFIED},
    )
    assert reviews(tmp_path).latest() == {}


@pytest.fixture(params=["stand-in", "dufs"])
def share(request, tmp_path):
    """A share of ``outbox`` configured as the artefacts experiment would read it."""
    if request.param == "dufs" and not dufs_share.installed():
        pytest.skip("the dufs binary is not installed")
    outbox = tmp_path / "share" / "outbox"
    outbox.mkdir(parents=True)
    with dufs_share.serve(outbox.parent, real=request.param == "dufs") as base:
        config = tmp_path / "dashboard" / "experiments" / "artefacts" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps({"enabled": True, "url": f"{base}/outbox", "root": str(outbox)})
        )
        yield types.SimpleNamespace(
            outbox=outbox, url=f"{base}/outbox", artefacts=wa.Artefacts(tmp_path / "dashboard")
        )


def test_a_workers_pr_review_on_the_share_is_found_with_links(share, tmp_path):
    folder = share.outbox / "mini" / "k2" / "artifacts" / "pr-review-o-r-5"
    write(
        folder,
        {
            "2026-11-22-r1-meta.md": META,
            "2026-11-22-r1-verified.md": VERIFIED,
            "2026-11-22-r1-reply.md": REPLY,
        },
    )
    review = reviews(tmp_path, share.artefacts).for_pr("o/r", 5)[0]
    assert [c["count"] for c in review["counts"]] == [1, 1]
    base = f"{share.url}/mini/k2/artifacts/pr-review-o-r-5"
    assert review["links"] == {
        "report": f"{base}/2026-11-22-r1-verified.md?view",
        "reply": f"{base}/2026-11-22-r1-reply.md?view",
    }


def test_a_sweep_report_in_the_shared_folder_links_to_the_share(share, tmp_path):
    out = share.outbox / "galaxy-pr-reviews"
    write(out, {"2026-11-23-pr25013.md": REPORT})
    review = reviews(tmp_path, share.artefacts, out=out).record(
        {
            "source": "sweep",
            "repo": "galaxyproject/galaxy",
            "number": 25013,
            "report": str(out / "2026-11-23-pr25013.md"),
        }
    )
    assert review["report_url"] == f"{share.url}/galaxy-pr-reviews/2026-11-23-pr25013.md?view"


def test_one_search_of_the_share_per_minute(share, tmp_path):
    asked = []

    def counting(url, **kwargs):
        asked.append(url)
        return wa.read_share(url, **kwargs)

    store = reviews(tmp_path, share.artefacts, read_share=counting)
    for number in (1, 2, 3):
        assert store.for_pr("o/r", number) == []
    assert [url for url in asked if "?q=" in url] == [f"{share.url}/?q=pr-review-&json"]


def test_a_share_without_search_adds_nothing(share, tmp_path):
    write(
        share.outbox / "pr-review-o-r-5",
        {"2026-11-22-r1-meta.md": META, "2026-11-22-r1-verified.md": VERIFIED},
    )

    def listing_only(url, **kwargs):
        value = wa.read_share(url.replace("?q=pr-review-&json", "?json"), **kwargs)
        return {**value, "allow_search": False}

    assert reviews(tmp_path, share.artefacts, read_share=listing_only).latest() == {}


def test_the_newest_review_of_each_pull_request(tmp_path):
    store = reviews(tmp_path)
    for date, verdict in (("2026-11-20", "changes needed"), ("2026-11-23", "approve")):
        store.store.record(
            {
                "source": "sweep",
                "repo": "O/R",
                "number": 1,
                "date": date,
                "verdict": verdict,
                "report": f"/r/{date}.md",
            }
        )
    assert store.latest()["o/r#1"]["verdict"] == "approve"
    assert [r["verdict"] for r in store.for_pr("o/r", 1)] == ["approve", "changes needed"]
