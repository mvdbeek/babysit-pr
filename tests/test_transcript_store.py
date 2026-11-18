"""The transcript store on its own: queries, its search index, versions, the sessions it
records for a checkout, and what pruning leaves."""

import json
import sqlite3
from pathlib import Path

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


def test_another_parser_version_reads_again_and_keeps_its_entries_until_then(tmp_path):
    store = ts.Store(tmp_path / ts.FILE)
    row = store.open("", "/f.jsonl", "claude", 1)
    store.save(
        row["id"], 0, restarted=False, added=[(0, *entry("kept"))], changed=[], place=place(1)
    )
    again = store.open("", "/f.jsonl", "claude", 1)
    assert (again["saved"], again["inode"], again["offset"]) == (1, 1, 10)
    # Another parser: its place is lost, so the file is read again from its start.
    changed = store.open("", "/f.jsonl", "claude", 2)
    assert (changed["id"], changed["inode"], changed["offset"]) == (row["id"], None, 0)
    # A file that can no longer be read keeps what the older parser made of it.
    assert store.page(row["id"], 0, 9) == [{"role": "user", "text": "kept"}]
    assert store.row("", "/f.jsonl")["saved"] == 1
    store.save(
        row["id"],
        changed["generation"],
        restarted=True,
        added=[(0, *entry("new"))],
        changed=[],
        place=place(1),
    )
    assert store.page(row["id"], 0, 9) == [{"role": "user", "text": "new"}]
    assert len(store.search([row["id"]], '"new"', 5)[0]) == 1
    assert (tmp_path / ts.FILE).stat().st_mode & 0o777 == 0o600


def test_a_whole_transcript_is_set_aside_until_a_read_from_the_start_is_whole_too(tmp_path):
    store = ts.Store(tmp_path / ts.FILE)
    row = store.open("", "/f.jsonl", "claude", 1)
    old = [(i, *entry(f"old {i}")) for i in range(3)]
    generation = store.save(
        row["id"], 0, restarted=False, added=old, changed=[], place=place(3), ready=True
    )
    assert store.whole(store.row("", "/f.jsonl")) == (row["id"], 0, 3)

    def texts(source):
        return [found["text"] for found in store.page(source, 0, 9)]

    def found(text):
        return [idx for _, idx, _, _ in store.search([row["id"], -row["id"]], f'"{text}"', 9)[0]]

    # The file is read again from its start, and the read stops short of its end.
    generation = store.save(
        row["id"],
        generation,
        restarted=True,
        added=[(0, *entry("new 0"))],
        changed=[],
        place=place(1),
        ready=False,
    )
    partial = store.row("", "/f.jsonl")
    assert (partial["saved"], partial["ready"]) == (1, 0) and texts(row["id"]) == ["new 0"]
    assert store.whole(partial) == (-row["id"], 0, 3)
    assert texts(-row["id"]) == ["old 0", "old 1", "old 2"] and found("old") == [2, 1, 0]
    assert [kept["file"] for kept in store.recorded("", "/", 9)] == []
    store.describe(partial, "sid", "/wt", None, 1.0, 1)
    assert [kept["file"] for kept in store.recorded("", "/wt", 9)] == ["/f.jsonl"]
    # Started again once more, only the unfinished read goes.
    generation = store.save(
        row["id"], generation, restarted=True, added=[], changed=[], place=place(0), ready=False
    )
    assert texts(row["id"]) == [] and texts(-row["id"]) == ["old 0", "old 1", "old 2"]
    assert [kept["file"] for kept in store.recorded("", "/wt", 9)] == ["/f.jsonl"]
    # Read to its end, the new transcript is the record, and the one set aside goes.
    store.save(
        row["id"],
        generation,
        restarted=False,
        added=[(0, *entry("new 0")), (1, *entry("new 1"))],
        changed=[],
        place=place(2),
        ready=True,
    )
    assert store.whole(store.row("", "/f.jsonl")) == (row["id"], 0, 2)
    assert texts(-row["id"]) == [] and found("old") == [] and found("new") == [1, 0]
    with sqlite3.connect(tmp_path / ts.FILE) as db:
        db.execute("INSERT INTO search(search) VALUES ('integrity-check')")


def test_a_store_from_before_is_taken_over_and_gains_what_it_lacks(tmp_path):
    # As the store was while it was a cache, under its name then.
    with sqlite3.connect(tmp_path / ts.CACHE_FILE) as db:
        db.executescript(
            """
            CREATE TABLE transcripts (
                id INTEGER PRIMARY KEY, machine TEXT NOT NULL, file TEXT NOT NULL,
                agent TEXT NOT NULL, parser INTEGER NOT NULL, inode INTEGER,
                offset INTEGER NOT NULL DEFAULT 0, skipping INTEGER NOT NULL DEFAULT 0,
                saved INTEGER NOT NULL DEFAULT 0, first INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT '{}', ready INTEGER NOT NULL DEFAULT 0,
                generation INTEGER NOT NULL DEFAULT 0, seen REAL NOT NULL,
                UNIQUE (machine, file)
            );
            CREATE TABLE entries (
                transcript INTEGER NOT NULL, idx INTEGER NOT NULL, data TEXT NOT NULL,
                body TEXT NOT NULL, indexed INTEGER NOT NULL DEFAULT 0,
                UNIQUE (transcript, idx)
            );
            INSERT INTO transcripts (id, machine, file, agent, parser, saved, ready, seen)
                VALUES (7, '', '/old.jsonl', 'claude', 1, 1, 1, 0);
            INSERT INTO entries (transcript, idx, data, body)
                VALUES (7, 0, '{"role": "user", "text": "from before"}', 'from before');
            PRAGMA user_version=1;
            """
        )
    store = ts.Store(tmp_path / ts.FILE)
    assert not (tmp_path / ts.CACHE_FILE).exists()
    row = store.row("", "/old.jsonl")
    assert (row["id"], row["saved"], row["session"], row["cwd"]) == (7, 1, None, None)
    assert row["raw_size"] == 0 and store.kept_files("") == {"/old.jsonl": [7, None, 0]}
    assert store.page(7, 0, 9) == [{"role": "user", "text": "from before"}]
    assert len(store.search([7], '"before"', 5)[0]) == 1
    store.describe(row, "sid", "/wt/repo", "A title", 5.0, 9)
    assert [found["file"] for found in store.recorded("", "/wt/repo", 9)] == ["/old.jsonl"]
    # Code from before then starts a cache of its own, which is left alone.
    (tmp_path / ts.CACHE_FILE).write_bytes(b"")
    assert ts.Store(tmp_path / ts.FILE).row("", "/old.jsonl")["session"] == "sid"
    assert (tmp_path / ts.CACHE_FILE).exists()


def test_a_store_written_by_newer_code_is_left_alone_for_it(tmp_path):
    store = ts.Store(tmp_path / ts.FILE)
    row = store.open("", "/f.jsonl", "claude", 1)
    store.save(
        row["id"], 0, restarted=False, added=[(0, *entry("kept"))], changed=[], place=place(1)
    )
    with sqlite3.connect(tmp_path / ts.FILE) as db:
        db.execute("PRAGMA user_version=99")
    # This code keeps a store of its own beside it, which it finds again.
    older = ts.Store(tmp_path / ts.FILE)
    assert older.path == tmp_path / f"{ts.FILE}.v{ts.SCHEMA}" and older.row("", "/f.jsonl") is None
    mine = older.open("", "/mine.jsonl", "claude", 1)
    assert ts.Store(tmp_path / ts.FILE).row("", "/mine.jsonl")["id"] == mine["id"]
    assert older.path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(tmp_path / ts.FILE) as db:
        assert db.execute("SELECT body FROM entries").fetchall() == [("kept",)]
        assert db.execute("PRAGMA user_version").fetchone() == (99,)


def test_a_search_indexes_what_it_can_in_time_and_names_what_is_left(tmp_path):
    store = ts.Store(tmp_path / ts.FILE)
    ids = []
    for file in ("/a.jsonl", "/b.jsonl"):
        row = store.open("", file, "claude", 1)
        added = [(i, *entry(f"needle {i}")) for i in range(5)]
        store.save(row["id"], 0, restarted=False, added=added, changed=[], place=place(5))
        ids.append(row["id"])
    # No time at all: nothing is indexed, so nothing is found, and both are named.
    assert store.search(ids, '"needle"', 9, stop=0) == ([], {})
    assert store.unindexed(ids) == set(ids) and store.unindexed([]) == set()
    store.index(ids[0], batch=2)
    assert store.unindexed(ids) == {ids[1]}
    assert store.search(ids, '"needle"', 9, stop=0)[1] == {ids[0]: 5}
    assert store.search(ids, '"needle"', 9)[1] == {ids[0]: 5, ids[1]: 5}
    assert store.unindexed(ids) == set()


def test_a_session_file_is_kept_in_pieces_and_kept_on_from_where_it_ended(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "PIECE", 8)
    store = ts.Store(tmp_path / ts.FILE)
    id_ = store.open("", "/s/one.jsonl", "claude", 1)["id"]
    other = store.open("", "/s/two.jsonl", "claude", 1)["id"]

    def pieces(transcript=id_):
        with sqlite3.connect(store.path) as db:
            return db.execute(
                "SELECT count(*) FROM raw WHERE transcript=?", (transcript,)
            ).fetchone()[0]

    assert list(store.kept(id_)) == []
    store.keep(id_, 5, 0, [b"0123456789"])
    store.keep(other, 6, 0, [b"another file"])
    assert b"".join(store.kept(id_)) == b"0123456789" and pieces() == 2
    assert store.row("", "/s/one.jsonl")["raw_size"] == 10
    # Kept on: the last piece is filled up, and only whole pieces come before it.
    store.keep(id_, 5, 10, [b"abc", b"defghijkl", b"m"])
    assert [len(piece) for piece in store.kept(id_)] == [8, 8, 7]
    assert b"".join(store.kept(id_)) == b"0123456789abcdefghijklm"
    assert store.kept_files("") == {"/s/one.jsonl": [id_, 5, 23], "/s/two.jsonl": [other, 6, 12]}
    # Only from where the copy ends, and only of the file it was made of.
    for inode, offset in [(5, 10), (5, 30), (9, 23)]:
        with pytest.raises(ts.Conflict):
            store.keep(id_, inode, offset, [b"x"])
    with pytest.raises(ts.Conflict):
        store.keep(99, 5, 0, [b"x"])
    assert b"".join(store.kept(id_)) == b"0123456789abcdefghijklm"

    # All of it is written at once: a read that fails leaves the copy as it was.
    def failing():
        yield b"nopqrstuvwxyz"
        raise OSError("gone")

    for offset in (23, 0):
        with pytest.raises(OSError):
            store.keep(id_, 5, offset, failing())
    assert b"".join(store.kept(id_)) == b"0123456789abcdefghijklm"
    # Another file in its place is kept from its start; nothing of the first stays.
    store.keep(id_, 9, 0, [b"new"])
    assert b"".join(store.kept(id_)) == b"new" and pieces() == 1
    assert store.kept_files("")["/s/one.jsonl"] == [id_, 9, 3]
    assert b"".join(store.kept(other)) == b"another file"


def test_a_file_written_back_is_the_one_its_transcript_and_copy_are_of(tmp_path):
    store = ts.Store(tmp_path / ts.FILE)

    def kept(name, read_from, read_to, copy_of, copy):
        row = store.open("", name, "claude", 1)
        store.save(
            row["id"],
            0,
            restarted=False,
            added=[(0, *entry("kept"))],
            changed=[],
            place=(read_from, read_to, False, 1, 0, "{}"),
            ready=True,
        )
        store.keep(row["id"], copy_of, 0, [copy])
        return row["id"]

    def state(name):
        row = store.row("", name)
        return row["inode"], row["offset"], *store.kept_files("")[name][1:]

    # Written back whole: read on from where it was, and kept on from where the copy ends.
    whole = kept("/s/whole.jsonl", 5, 7, 5, b"a line\n")
    store.restored(whole, 8, 7)
    assert state("/s/whole.jsonl") == (8, 7, 8, 7)
    store.keep(whole, 8, 7, [b"more\n"])
    assert b"".join(store.kept(whole)) == b"a line\nmore\n"
    # Written back without the unfinished line the copy ends in: the copy is not of that
    # file, and is made again rather than kept on past the line that was left out.
    cut = kept("/s/cut.jsonl", 5, 7, 5, b'a line\n{"part')
    store.restored(cut, 8, 7)
    assert state("/s/cut.jsonl") == (8, 7, None, 13)
    with pytest.raises(ts.Conflict):
        store.keep(cut, 8, 13, [b'ial"}\n'])
    # A transcript read further than was written back, or from another file than the
    # copy is of, is read again from the start of the one written back.
    ahead = kept("/s/ahead.jsonl", 5, 40, 5, b"a line\n")
    store.restored(ahead, 8, 7)
    assert state("/s/ahead.jsonl") == (None, 40, 8, 7)
    other = kept("/s/other.jsonl", 3, 7, 4, b"a line\n")
    store.restored(other, 8, 7)
    assert state("/s/other.jsonl") == (None, 7, 8, 7)


def test_sessions_are_recorded_for_their_checkout_and_what_is_below_it(tmp_path, monkeypatch):
    store = ts.Store(tmp_path / ts.FILE)
    kept = [
        ("", "/s/one.jsonl", "/wt/repo", 3.0),
        ("", "/s/sub.jsonl", "/wt/repo/lib", 5.0),
        ("", "/s/sibling.jsonl", "/wt/repo-2", 6.0),
        ("", "/s/parent.jsonl", "/wt", 7.0),
        ("mini", "/s/remote.jsonl", "/wt/repo", 8.0),
    ]
    for machine, file, cwd, updated in kept:
        row = store.open(machine, file, "claude", 1)
        store.describe(row, Path(file).stem, cwd, f"About {Path(file).stem}", updated, 10)
        store.save(
            row["id"],
            0,
            restarted=False,
            added=[(0, *entry(file))],
            changed=[],
            place=place(1),
            ready=True,
        )
    found = store.recorded("", "/wt/repo", 9)
    assert [(f["file"], f["session"], f["title"]) for f in found] == [
        ("/s/sub.jsonl", "sub", "About sub"),
        ("/s/one.jsonl", "one", "About one"),
    ]
    assert (found[0]["agent"], found[0]["updated"], found[0]["size"]) == ("claude", 5.0, 10)
    assert [f["file"] for f in store.recorded("", "/wt/repo", 1)] == ["/s/sub.jsonl"]
    assert [f["file"] for f in store.recorded("mini", "/wt/repo", 9)] == ["/s/remote.jsonl"]
    assert len(store.recorded("", "/wt", 9)) == 4 and store.recorded("", "/wt/rep", 9) == []
    assert len(store.recorded("", "/wt/repo/", 9)) == 2 and len(store.recorded("", "/", 9)) == 4
    # A session with nothing saved is not listed; one read to its end says how far.
    empty = store.open("", "/s/empty.jsonl", "claude", 1)
    store.describe(empty, "empty", "/wt/repo", None, 9.0, 0)
    assert len(store.recorded("", "/wt/repo", 9)) == 2
    assert store.read_to("") == {file: 10 for machine, file, _, _ in kept if not machine}
    # Described again the same, nothing is written.
    row = store.row("", "/s/one.jsonl")
    monkeypatch.setattr(store, "write", None)
    store.describe(row, "one", "/wt/repo", "About one", 3.0, 10)


def test_pruning_keeps_transcripts_of_gone_files_and_drops_only_their_details(tmp_path):
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
    assert store.details("/gone.jsonl", (1, 2, 3)) is None and store.details(kept, (1, 2, 3))
    # At most once a day; however long ago a transcript was read, it stays.
    store.prune(exists=lambda file: False)
    assert store.details(kept, (1, 2, 3))
    store.prune(now=store.pruned + 400 * 86400, exists=lambda file: False)
    assert store.details(kept, (1, 2, 3)) is None
    for machine, file in (("", str(kept)), ("", "/gone.jsonl"), ("mini", "/remote.jsonl")):
        assert store.page(store.row(machine, file)["id"], 0, 9)[0]["text"] == file
