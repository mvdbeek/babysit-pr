"""pr-review summaries from temporary review folders on disk and on a stand-in share."""

import json
import os
import types

import dufs_share
import pr_reviews
import pytest
import workspace_artefacts as wa

VERIFIED = """### P1 1: Data loss when the queue restarts

Body.

### P3 1: A typo (verified)

Body.
"""
TRIMMED = """### P3 1: A typo (verified)

Body.
"""
REPLY = """<!-- Your own comments go here. -->

<details><summary>Risks</summary>

Risks are **minimal**: this change is a two-way door.

Second paragraph.

</details>
"""
META = "- head (reviewed commit): b54743e250148303de57ab2a73b3a89fb6efd530\n"


def write(folder, files, modified=None):
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (folder / name).write_text(text)
        if modified:
            os.utime(folder / name, (modified, modified))


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
        config.write_text(json.dumps({"enabled": True, "url": f"{base}/outbox"}))
        yield types.SimpleNamespace(
            outbox=outbox,
            url=f"{base}/outbox",
            artefacts=wa.Artefacts(tmp_path / "dashboard"),
        )


def test_the_newest_round_with_findings_and_its_latest_trim_are_summarised(tmp_path):
    home = tmp_path / "home"
    write(
        home / ".cache" / "babysit-sandbox" / "k1" / "artifacts" / "pr-review-o-r-5",
        {
            "2026-11-20-r1-verified.md": "### P2 1: Old\n",
            "2026-11-22-r2-meta.md": META,
            "2026-11-22-r2-verified.md": VERIFIED,
            "2026-11-22-r2-reply.md": REPLY,
            "2026-11-23-r2-trim1-verified.md": TRIMMED,
            # An aborted round: no findings yet, so round 2 is still the latest.
            "2026-11-23-r3-meta.md": META,
        },
    )
    review = pr_reviews.Reviews(None, home=home).summary("o", "r", 5)
    assert review["round"] == 2 and review["trim"] == 1 and review["date"] == "2026-11-23"
    assert review["counts"] == {"P1": 0, "P2": 0, "P3": 1}
    assert review["findings"] == [{"priority": "P3", "title": "A typo"}]
    assert review["risk"] == "Risks are minimal: this change is a two-way door."
    assert review["commit"].startswith("b54743e2")
    # On this machine's disk only: nothing to link to.
    assert review["links"] == {}


def test_folders_for_other_pull_requests_and_odd_names_are_ignored(tmp_path):
    home = tmp_path / "home"
    write(
        home / "src" / "agent-artefacts" / "outbox" / "pr-review-o-r-50",
        {"2026-11-22-r1-verified.md": VERIFIED},
    )
    reviews = pr_reviews.Reviews(None, home=home)
    assert reviews.summary("o", "r", 5) is None
    assert reviews.summary("o", "r", 50)["counts"]["P1"] == 1
    assert reviews.summary("../o", "r", 50) is None


def test_a_workers_review_on_the_share_is_found_with_links(share, tmp_path):
    home = tmp_path / "home"
    # This machine's older review of the same pull request loses to the worker's.
    write(
        home / "src" / "agent-artefacts" / "outbox" / "pr-review-o-r-5",
        {"2026-11-20-r1-verified.md": "### P2 1: Old\n"},
        modified=1_000_000,
    )
    folder = share.outbox / "mini" / "k2" / "artifacts" / "pr-review-o-r-5"
    write(folder, {"2026-11-22-r1-verified.md": VERIFIED, "2026-11-22-r1-reply.md": REPLY})
    write(share.outbox / "mini" / "k2" / "artifacts" / "pr-review-o-r-55", {"x.md": ""})
    review = pr_reviews.Reviews(share.artefacts, home=home).summary("o", "r", 5)
    assert review["counts"] == {"P1": 1, "P2": 0, "P3": 1}
    base = f"{share.url}/mini/k2/artifacts/pr-review-o-r-5"
    assert review["links"] == {
        "verified": f"{base}/2026-11-22-r1-verified.md?view",
        "reply": f"{base}/2026-11-22-r1-reply.md?view",
    }


def test_a_local_review_pulled_to_the_share_links_to_its_copy(share, tmp_path):
    home = tmp_path / "home"
    files = {"2026-11-22-r1-verified.md": VERIFIED}
    write(home / ".cache" / "babysit-sandbox" / "k1" / "artifacts" / "pr-review-o-r-5", files)
    write(share.outbox / "chonk" / "k1" / "artifacts" / "pr-review-o-r-5", files, 1_000_000)
    review = pr_reviews.Reviews(share.artefacts, home=home).summary("o", "r", 5)
    assert review["links"]["verified"].startswith(f"{share.url}/chonk/k1/")


def test_one_search_of_the_share_serves_every_pull_request(share, tmp_path, monkeypatch):
    asked = []

    def counting(url, **kwargs):
        asked.append(url)
        return wa.read_share(url, **kwargs)

    reviews = pr_reviews.Reviews(share.artefacts, home=tmp_path / "home", read_share=counting)
    for number in (1, 2, 3):
        assert reviews.summary("o", "r", number) is None
    assert [url for url in asked if "?q=" in url] == [f"{share.url}/?q=pr-review-&json"]


def test_a_share_without_search_finds_nothing_there(share, tmp_path):
    write(share.outbox / "pr-review-o-r-5", {"2026-11-22-r1-verified.md": VERIFIED})

    def listing_only(url, **kwargs):
        value = wa.read_share(url.replace("?q=pr-review-&json", "?json"), **kwargs)
        return {**value, "allow_search": False}

    reviews = pr_reviews.Reviews(share.artefacts, home=tmp_path / "home", read_share=listing_only)
    assert reviews.summary("o", "r", 5) is None
