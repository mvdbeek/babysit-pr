"""The transcript store on its own: queries, its search index, versions and pruning."""

import json
import sqlite3

import pytest
import transcript_store as ts


def entry(text):
    return json.dumps({"role": "user", "text": text}), text


def place(saved, first=0):
    return (1, 10, False, saved, first, "{}")


def test_queries_match_each_word_literally_or_short_words_as_one_phrase():
    assert ts.match_expression("remote probe") == '"remote" "probe"'
    assert ts.match_expression(' say "hi" ') == '"say" """hi"""'
    assert ts.match_expression("is it ok") == '"is it ok"'
    assert (
        ts.match_expression(" ".join("word" for _ in range(9)))
        == '"' + " ".join("word" for _ in range(9)) + '"'
    )
    for text in ("", "  ", "ab", None, "x" * (ts.MAX_QUERY + 1)):
        with pytest.raises(ValueError):
            ts.match_expression(text)
    assert ts.snippet_parts("a \x02hit\x03 b \x02two\x03") == [
        ("a ", False),
        ("hit", True),
        (" b ", False),
        ("two", True),
    ]


def test_saved_entries_are_searched_as_they_change_and_go(tmp_path):
    store = ts.Store(tmp_path / ts.FILE)
    row = store.open("", "/f.jsonl", "claude", 1)
    added = [(0, *entry("alpha beta")), (1, *entry("gamma"))]
    generation = store.save(
        row["id"], row["generation"], restarted=False, added=added, changed=[], place=place(2)
    )

    def found(text):
        matches, counts = store.search([row["id"]], ts.match_expression(text), 10)
        return [idx for _, idx, _, _ in matches], counts

    assert found("beta") == ([0], {row["id"]: 1})
    generation = store.save(
        row["id"],
        generation,
        restarted=False,
        added=[(2, *entry("beta again"))],
        changed=[(0, *entry("alpha delta"))],
        place=place(3),
    )
    assert found("beta")[0] == [2] and found("delta")[0] == [0]
    # The oldest entries past ``first`` go, from the index too; a restart drops the rest.
    generation = store.save(
        row["id"], generation, restarted=False, added=[], changed=[], place=place(3, first=1)
    )
    assert found("delta")[0] == [] and store.page(row["id"], 0, 9)[0]["text"] == "gamma"
    store.save(
        row["id"],
        generation,
        restarted=True,
        added=[(0, *entry("fresh"))],
        changed=[],
        place=place(1),
        ready=True,
    )
    assert found("gamma")[0] == [] and found("fresh")[0] == [0]
    assert store.row("", "/f.jsonl")["ready"] == 1
    with sqlite3.connect(tmp_path / ts.FILE) as db:
        db.execute("INSERT INTO search(search) VALUES ('integrity-check')")


def test_another_schema_or_parser_version_starts_again(tmp_path):
    store = ts.Store(tmp_path / ts.FILE)
    row = store.open("", "/f.jsonl", "claude", 1)
    store.save(
        row["id"], 0, restarted=False, added=[(0, *entry("kept"))], changed=[], place=place(1)
    )
    assert store.open("", "/f.jsonl", "claude", 1)["saved"] == 1
    assert store.open("", "/f.jsonl", "claude", 2)["saved"] == 0
    store.save(
        store.row("", "/f.jsonl")["id"],
        0,
        restarted=False,
        added=[(0, *entry("kept"))],
        changed=[],
        place=place(1),
    )
    with sqlite3.connect(tmp_path / ts.FILE) as db:
        db.execute("PRAGMA user_version=99")
    reopened = ts.Store(tmp_path / ts.FILE)
    assert reopened.row("", "/f.jsonl") is None
    row = reopened.open("", "/f.jsonl", "claude", 2)
    reopened.save(
        row["id"], 0, restarted=False, added=[(0, *entry("new"))], changed=[], place=place(1)
    )
    assert len(reopened.search([row["id"]], '"new"', 5)[0]) == 1
    assert (tmp_path / ts.FILE).stat().st_mode & 0o777 == 0o600


def test_pruning_drops_gone_files_unread_rows_and_the_least_recently_read(tmp_path, monkeypatch):
    store = ts.Store(tmp_path / ts.FILE)
    kept = tmp_path / "kept.jsonl"
    kept.write_text("")
    for machine, file in (("", str(kept)), ("", "/gone.jsonl"), ("mini", "/remote.jsonl")):
        row = store.open(machine, file, "claude", 1)
        store.save(
            row["id"], 0, restarted=False, added=[(0, *entry(file))], changed=[], place=place(1)
        )
    store.put_details(kept, (1, 2, 3), {"id": "x"})
    store.put_details("/gone.jsonl", (1, 2, 3), None)
    assert (
        store.details(kept, (1, 2, 3)) == ({"id": "x"},) and store.details(kept, (1, 2, 4)) is None
    )
    store.prune()
    assert store.row("", "/gone.jsonl") is None and store.details("/gone.jsonl", (1, 2, 3)) is None
    assert store.row("", str(kept)) and store.row("mini", "/remote.jsonl")
    # At most once a day; then rows unread for KEEP_DAYS go, and the oldest past the cap.
    store.prune(exists=lambda file: False)
    assert store.row("", str(kept))
    later = store.pruned + ts.PRUNE_EVERY
    monkeypatch.setattr(ts, "MAX_TRANSCRIPTS", 1)
    store.prune(now=later)
    assert [store.row("", str(kept)) is None, store.row("mini", "/remote.jsonl") is None].count(
        True
    ) == 1
    store.prune(now=later + ts.KEEP_DAYS * 86400 + ts.PRUNE_EVERY)
    assert store.row("", str(kept)) is None and store.row("mini", "/remote.jsonl") is None
    with sqlite3.connect(tmp_path / ts.FILE) as db:
        assert db.execute("SELECT count(*) FROM entries").fetchone() == (0,)
