"""Parsed agent transcripts kept in SQLite, with a full-text index over their entries.

The agents' JSONL session files stay the record; this is what the viewer parsed from
them, so a transcript opened again (after a restart, or once others pushed it out of
memory) is read on from where it stopped rather than parsed from the start. Each row
names its file and its machine ('' for this one) and keeps the parser's place in it;
entries keep their index in the whole transcript and their clipped JSON, and their
text feeds an FTS5 trigram index, so any three or more characters can be searched.
Indexing costs more than parsing, so entries are saved unindexed and indexed soon after
by a thread of their own, or first by a search that needs them.

Everything here can be rebuilt from the session files: an unknown schema or parser
version discards what was kept, and rows for files that are gone are pruned.
"""

import contextlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

FILE = "transcripts.sqlite"
# Bumped when the tables change; a store of another version is emptied, not migrated.
SCHEMA = 1
# Rows not read for this long are dropped, and the least recently read past the cap.
KEEP_DAYS = 30
MAX_TRANSCRIPTS = 2000
MAX_DETAILS = 20000
PRUNE_EVERY = 24 * 3600
# Search terms: FTS5's trigram index matches nothing shorter than three characters.
MAX_QUERY = 200
MAX_TERMS = 8
MIN_TERM = 3
# Snippet markers around each match, which the browser turns into highlights.
MARK = ("\x02", "\x03")
# Entries indexed per transaction, so a save never waits long behind indexing.
INDEX_BATCH = 2000


class Conflict(ValueError):
    """Another reader saved a transcript while this one was reading it."""


def match_expression(text):
    """An FTS5 query matching ``text`` literally: each word as a substring, all of them.

    A query with a word too short to match alone is searched as one phrase instead.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Type something to search for")
    text = " ".join(text.split())
    if len(text) > MAX_QUERY:
        raise ValueError(f"Search for at most {MAX_QUERY} characters")
    words = text.split(" ")
    if len(words) > MAX_TERMS or any(len(word) < MIN_TERM for word in words):
        if len(text) < MIN_TERM:
            raise ValueError(f"Search for at least {MIN_TERM} characters")
        words = [text]
    return " ".join('"' + word.replace('"', '""') + '"' for word in words)


class Store:
    """One SQLite file of parsed transcripts; each operation opens its own connection."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
        self.lock = threading.Lock()
        self.pruned = 0.0
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA:
                for kind, name in db.execute(
                    "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'trigger')"
                    " AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'search_%'"
                ).fetchall():
                    db.execute(f'DROP {kind.upper()} IF EXISTS "{name}"')
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS transcripts (
                    id INTEGER PRIMARY KEY,
                    machine TEXT NOT NULL,
                    file TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    parser INTEGER NOT NULL,
                    inode INTEGER,
                    offset INTEGER NOT NULL DEFAULT 0,
                    skipping INTEGER NOT NULL DEFAULT 0,
                    saved INTEGER NOT NULL DEFAULT 0,
                    first INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT '{}',
                    ready INTEGER NOT NULL DEFAULT 0,
                    generation INTEGER NOT NULL DEFAULT 0,
                    seen REAL NOT NULL,
                    UNIQUE (machine, file)
                );
                CREATE TABLE IF NOT EXISTS entries (
                    transcript INTEGER NOT NULL,
                    idx INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    body TEXT NOT NULL,
                    indexed INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (transcript, idx)
                );
                CREATE INDEX IF NOT EXISTS entries_unindexed ON entries (transcript)
                    WHERE indexed = 0;
                CREATE TABLE IF NOT EXISTS details (
                    file TEXT PRIMARY KEY,
                    version TEXT NOT NULL,
                    found TEXT
                );
                """
            )
            indexed = db.execute("SELECT 1 FROM sqlite_master WHERE name='search'").fetchone()
            try:
                db.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
                        body, content='entries', content_rowid='rowid', tokenize='trigram'
                    );
                    CREATE TRIGGER IF NOT EXISTS entries_indexed
                    AFTER UPDATE OF indexed ON entries WHEN old.indexed = 0 AND new.indexed = 1
                    BEGIN
                        INSERT INTO search(rowid, body) VALUES (new.rowid, new.body);
                    END;
                    CREATE TRIGGER IF NOT EXISTS entries_removed
                    AFTER DELETE ON entries WHEN old.indexed = 1 BEGIN
                        INSERT INTO search(search, rowid, body)
                        VALUES ('delete', old.rowid, old.body);
                    END;
                    CREATE TRIGGER IF NOT EXISTS entries_changed
                    AFTER UPDATE OF body ON entries WHEN old.indexed = 1 AND new.indexed = 1
                    BEGIN
                        INSERT INTO search(search, rowid, body)
                        VALUES ('delete', old.rowid, old.body);
                        INSERT INTO search(rowid, body) VALUES (new.rowid, new.body);
                    END;
                    """
                )
                self.searchable = True
            except sqlite3.OperationalError:
                # This SQLite has no FTS5 or no trigram tokenizer: views work, search does not.
                self.searchable = False
            if self.searchable and not indexed:
                # Entries kept while this SQLite could not index them (an older one).
                db.execute("UPDATE entries SET indexed = 0")
            db.execute(f"PRAGMA user_version={SCHEMA}")
        # Transcripts with entries to index, and the thread indexing them.
        self.waiting: dict[int, None] = {}
        self.wake = threading.Condition()
        self.indexer: threading.Thread | None = None

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            # The session files are the record: losing the last commits to a crash only
            # means reading those bytes again.
            db.execute("PRAGMA synchronous=NORMAL")
            # A replaced entry leaves the search index through its delete trigger.
            db.execute("PRAGMA recursive_triggers=ON")
            yield db
        finally:
            db.close()

    @contextlib.contextmanager
    def write(self):
        """A write transaction; one writer at a time within this process."""
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    # Transcripts ---------------------------------------------------------------

    def open(self, machine, file, agent, parser):
        """The row for ``file`` on ``machine`` as a dict, made if new.

        A row kept by another parser version, or for the other agent, starts again.
        """
        with self.db() as db:
            row = self.row_of(db, machine, file, extra=", agent, parser, seen")
        if row and (row.pop("agent"), row.pop("parser")) == (agent, parser):
            # Read every few seconds while open: when it was read is only kept to the hour.
            if row.pop("seen") > time.time() - 3600:
                return row
        with self.write() as db:
            found = db.execute(
                "SELECT id, agent, parser FROM transcripts WHERE machine=? AND file=?",
                (machine, file),
            ).fetchone()
            if found and (found[1] != agent or found[2] != parser):
                db.execute("DELETE FROM entries WHERE transcript=?", (found[0],))
                db.execute("DELETE FROM transcripts WHERE id=?", (found[0],))
                found = None
            if found is None:
                db.execute(
                    "INSERT INTO transcripts (machine, file, agent, parser, seen)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (machine, file, agent, parser, time.time()),
                )
            else:
                db.execute("UPDATE transcripts SET seen=? WHERE id=?", (time.time(), found[0]))
            row = self.row_of(db, machine, file)
        return row

    @staticmethod
    def row_of(db, machine, file, extra=""):
        cursor = db.execute(
            "SELECT id, inode, offset, skipping, saved, first, state, ready, generation"
            f"{extra} FROM transcripts WHERE machine=? AND file=?",
            (machine, file),
        )
        values = cursor.fetchone()
        return (
            dict(zip([c[0] for c in cursor.description], values, strict=True)) if values else None
        )

    def row(self, machine, file):
        """The saved row for ``file`` on ``machine``, or None."""
        with self.db() as db:
            return self.row_of(db, machine, file)

    def save(self, transcript, generation, *, restarted, added, changed, place, ready=None):
        """Store one read's progress in a single transaction; returns the next generation.

        ``restarted`` drops what was kept first (the file was replaced or truncated);
        ``added`` and ``changed`` are (index, entry JSON, search text) triples; ``place``
        is the parser's position (inode, offset, skipping, saved, first, state); entries
        before ``first`` go. ``ready`` marks a read that reached the end of the file.
        Refused, with nothing written, unless the row is still at ``generation``: another
        reader saved it since this one began.
        """
        inode, offset, skipping, saved, first, state = place
        with self.write() as db:
            if (
                db.execute(
                    "SELECT 1 FROM transcripts WHERE id=? AND generation=?",
                    (transcript, generation),
                ).fetchone()
                is None
            ):
                raise Conflict("The transcript was read elsewhere meanwhile; refresh")
            if restarted:
                db.execute("DELETE FROM entries WHERE transcript=?", (transcript,))
            db.execute("DELETE FROM entries WHERE transcript=? AND idx<?", (transcript, first))
            db.executemany(
                "INSERT OR REPLACE INTO entries (transcript, idx, data, body) VALUES (?, ?, ?, ?)",
                [(transcript, *item) for item in added],
            )
            db.executemany(
                "UPDATE entries SET data=?, body=? WHERE transcript=? AND idx=?",
                [(data, body, transcript, idx) for idx, data, body in changed],
            )
            db.execute(
                "UPDATE transcripts SET inode=?, offset=?, skipping=?, saved=?, first=?,"
                " state=?, ready=CASE WHEN ? IS NULL THEN ready ELSE ? END,"
                " generation=generation+1 WHERE id=?",
                (inode, offset, int(skipping), saved, first, state, ready, ready, transcript),
            )
        return generation + 1

    def index(self, transcript, batch=INDEX_BATCH):
        """Index ``transcript``'s unindexed entries, ``batch`` per transaction."""
        if not self.searchable:
            return
        while True:
            with self.write() as db:
                done = db.execute(
                    "UPDATE entries SET indexed = 1 WHERE rowid IN (SELECT rowid FROM entries"
                    " WHERE transcript = ? AND indexed = 0 LIMIT ?)",
                    (transcript, batch),
                ).rowcount
            if done < batch:
                return

    def index_soon(self, transcript):
        """Index ``transcript``'s new entries on the indexing thread."""
        if not self.searchable:
            return
        with self.wake:
            self.waiting[transcript] = None
            if self.indexer is None or not self.indexer.is_alive():
                self.indexer = threading.Thread(
                    target=self.index_waiting, name="transcript-index", daemon=True
                )
                self.indexer.start()
            self.wake.notify()

    def index_waiting(self):
        while True:
            with self.wake:
                if not self.waiting and not self.wake.wait(timeout=60):
                    self.indexer = None
                    return
                if not self.waiting:
                    continue
                transcript = next(iter(self.waiting))
                del self.waiting[transcript]
            try:
                self.index(transcript)
            except sqlite3.Error:
                pass  # A search indexes what is left before it looks.

    def entry(self, transcript, index):
        """One saved entry, or None if it is not kept."""
        with self.db() as db:
            found = db.execute(
                "SELECT data FROM entries WHERE transcript=? AND idx=?", (transcript, index)
            ).fetchone()
        return json.loads(found[0]) if found else None

    def page(self, transcript, start, end):
        """The saved entries from index ``start`` up to ``end``, in order."""
        with self.db() as db:
            return [
                json.loads(data)
                for (data,) in db.execute(
                    "SELECT data FROM entries WHERE transcript=? AND idx>=? AND idx<? ORDER BY idx",
                    (transcript, start, end),
                )
            ]

    def search(self, ranked, expression, limit):
        """Entries of the ``ranked`` transcripts (ids, best first) matching ``expression``.

        Returns (matches, counts): at most ``limit`` (transcript, index, entry, snippet)
        tuples, newest first within each transcript, and every match's count per id.
        """
        if not self.searchable:
            raise ValueError("Search needs SQLite with FTS5 and its trigram tokenizer")
        if not ranked:
            return [], {}
        for id_ in ranked:
            self.index(id_)
        listed = ",".join("(?, ?)" for _ in ranked)
        order = [value for rank, id_ in enumerate(ranked) for value in (id_, rank)]
        with self.db() as db:
            try:
                rows = db.execute(
                    f"WITH listed(id, rank) AS (VALUES {listed})"
                    " SELECT e.transcript, e.idx, e.data,"
                    " snippet(search, 0, ?, ?, '…', 24)"
                    " FROM search JOIN entries e ON e.rowid = search.rowid"
                    " JOIN listed ON listed.id = e.transcript"
                    " WHERE search MATCH ? ORDER BY listed.rank, e.idx DESC LIMIT ?",
                    (*order, *MARK, expression, limit),
                ).fetchall()
                counts = dict(
                    db.execute(
                        f"WITH listed(id, rank) AS (VALUES {listed})"
                        " SELECT e.transcript, count(*)"
                        " FROM search JOIN entries e ON e.rowid = search.rowid"
                        " JOIN listed ON listed.id = e.transcript"
                        " WHERE search MATCH ? GROUP BY e.transcript",
                        (*order, expression),
                    ).fetchall()
                )
            except sqlite3.OperationalError as exc:
                raise ValueError(f"Could not search for that: {exc}") from exc
        return [(t, idx, json.loads(data), snip) for t, idx, data, snip in rows], counts

    # Session details -------------------------------------------------------------

    def details(self, file, version):
        """The details saved for ``file`` at ``version`` as (found,), else None."""
        with self.db() as db:
            row = db.execute(
                "SELECT version, found FROM details WHERE file=?", (str(file),)
            ).fetchone()
        if row is None or row[0] != json.dumps(version):
            return None
        return (json.loads(row[1]) if row[1] is not None else None,)

    def put_details(self, file, version, found):
        with self.write() as db:
            db.execute(
                "INSERT OR REPLACE INTO details (file, version, found) VALUES (?, ?, ?)",
                (str(file), json.dumps(version), None if found is None else json.dumps(found)),
            )

    # Pruning ---------------------------------------------------------------------

    def prune_due(self, now=None):
        return (time.time() if now is None else now) - self.pruned >= PRUNE_EVERY

    def prune(self, now=None, exists=os.path.exists):
        """Drop rows of this machine's files that are gone, rows not read for KEEP_DAYS,
        and the least recently read past the caps. At most once per PRUNE_EVERY."""
        now = time.time() if now is None else now
        with self.lock:
            if not self.prune_due(now):
                return
            self.pruned = now
        with self.db() as db:
            kept = db.execute("SELECT id, machine, file, seen FROM transcripts").fetchall()
            files = [file for (file,) in db.execute("SELECT file FROM details")]
        gone = [
            id_
            for id_, machine, file, seen in kept
            if seen < now - KEEP_DAYS * 86400 or (machine == "" and not exists(file))
        ]
        stale = [file for file in files if not exists(file)]
        with self.write() as db:
            for id_ in gone:
                db.execute("DELETE FROM entries WHERE transcript=?", (id_,))
                db.execute("DELETE FROM transcripts WHERE id=?", (id_,))
            db.executemany("DELETE FROM details WHERE file=?", [(f,) for f in stale])
            for (id_,) in db.execute(
                "SELECT id FROM transcripts ORDER BY seen DESC LIMIT -1 OFFSET ?",
                (MAX_TRANSCRIPTS,),
            ).fetchall():
                db.execute("DELETE FROM entries WHERE transcript=?", (id_,))
                db.execute("DELETE FROM transcripts WHERE id=?", (id_,))
            db.execute(
                "DELETE FROM details WHERE rowid IN"
                " (SELECT rowid FROM details ORDER BY rowid DESC LIMIT -1 OFFSET ?)",
                (MAX_DETAILS,),
            )


def snippet_parts(text):
    """A snippet as [(text, matched)], split at its match markers."""
    parts = []
    for piece in re.split(f"({MARK[0]}[^{MARK[1]}]*{MARK[1]})", text):
        if piece.startswith(MARK[0]) and piece.endswith(MARK[1]):
            parts.append((piece[1:-1], True))
        elif piece:
            parts.append((piece.replace(MARK[0], "").replace(MARK[1], ""), False))
    return parts
