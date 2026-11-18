"""Parsed agent transcripts kept in SQLite, with a full-text index over their entries.

This is what the viewer parsed from the agents' JSONL session files, so a transcript
opened again (after a restart, or once others pushed it out of memory) is read on from
where it stopped rather than parsed from the start. Each row names its file and its
machine ('' for this one) and keeps the parser's place in it; entries keep their index
in the whole transcript and their clipped JSON, and their text feeds an FTS5 trigram
index, so any three or more characters can be searched. Indexing costs more than
parsing, so entries are saved unindexed and indexed soon after by a thread of their
own, or first by a search that needs them.

It is also the record of a session once its file is gone (Claude deletes its own after
a month, and a worker's go with the worker): a row keeps its session's ID, working
directory, title and last change, so the session is still listed for its checkout, and
nothing here drops a transcript. Only the session details are a cache.

A session of this machine also has its file kept as it was, compressed, so that a
session whose file is gone can be given it back and resumed.
"""

import contextlib
import json
import lzma
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

FILE = "transcript-archive.sqlite"
# The name while the store was a cache that dropped a transcript once its file was gone.
# It is taken over under the new name, so that code from before, run on the same home,
# starts a cache of its own rather than emptying this one.
CACHE_FILE = "transcripts.sqlite"
# Bumped when the tables change. An older store gains what it lacks. One written by
# newer code is left as it is for that code, and this one keeps a store of its own
# beside it, named after its version.
SCHEMA = 2
# What a row keeps of its session, to list it once its file is gone.
SESSION_COLUMNS = {
    "session": "TEXT",
    "cwd": "TEXT",
    "title": "TEXT",
    "updated": "REAL",
    "size": "INTEGER",
}
# How much of its session file a row keeps, and of which file: one read on, by its inode.
FILE_COLUMNS = {
    "raw_inode": "INTEGER",
    "raw_size": "INTEGER NOT NULL DEFAULT 0",
}
# A session file is kept in pieces of this many of its bytes, each compressed alone, so
# a file that grew is kept on by writing its last piece again and no other.
PIECE = 4 * 1024 * 1024
# The fastest xz preset: session files shrink to about a seventh, at some 70 MB a second.
PRESET = 1
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
        if self.path.name == FILE and not self.path.exists():
            move_store(self.path.with_name(CACHE_FILE), self.path)
        os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
        self.lock = threading.Lock()
        self.pruned = 0.0
        with self.db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA:
            # Written by code newer than this, which finds it untouched when it runs again.
            self.path = self.path.with_name(f"{self.path.name}.v{SCHEMA}")
            os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
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
                    session TEXT,
                    cwd TEXT,
                    title TEXT,
                    updated REAL,
                    size INTEGER,
                    raw_inode INTEGER,
                    raw_size INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (machine, file)
                );
                CREATE TABLE IF NOT EXISTS raw (
                    transcript INTEGER NOT NULL,
                    piece INTEGER NOT NULL,
                    data BLOB NOT NULL,
                    PRIMARY KEY (transcript, piece)
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
            # In one transaction: another dashboard opening the same store adds them once.
            db.execute("BEGIN IMMEDIATE")
            had = {column[1] for column in db.execute("PRAGMA table_info(transcripts)")}
            for name, kind in {**SESSION_COLUMNS, **FILE_COLUMNS}.items():
                if name not in had:
                    db.execute(f"ALTER TABLE transcripts ADD COLUMN {name} {kind}")
            db.execute("COMMIT")
            db.execute("CREATE INDEX IF NOT EXISTS transcripts_cwd ON transcripts (machine, cwd)")
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
            # In WAL mode a commit lost to a power cut leaves the store whole, and its
            # bytes are read again from the session file, which is still there so soon.
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
        """The row for ``file`` on ``machine`` as a dict, made if new, to read the file on.

        A row kept by another parser version, or for the other agent, is read again from
        the file's start: it loses its place, and its entries go when that read saves
        its first, so they are still there if the file can no longer be read.
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
                db.execute(
                    "UPDATE transcripts SET agent=?, parser=?, inode=NULL, offset=0, skipping=0,"
                    " generation=generation+1 WHERE id=?",
                    (agent, parser, found[0]),
                )
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
            "SELECT id, inode, offset, skipping, saved, first, state, ready, generation,"
            f" {', '.join(SESSION_COLUMNS)}, raw_size{extra} FROM transcripts"
            " WHERE machine=? AND file=?",
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

    def describe(self, row, session, cwd, title, updated, size):
        """Keep what lists ``row``'s session, written only when it changed."""
        found = dict(zip(SESSION_COLUMNS, (session, cwd, title, updated, size), strict=True))
        if all(row.get(name) == value for name, value in found.items()):
            return
        with self.write() as db:
            db.execute(
                f"UPDATE transcripts SET {', '.join(f'{name}=?' for name in found)} WHERE id=?",
                (*found.values(), row["id"]),
            )
        row.update(found)

    def recorded(self, machine, root, limit):
        """The newest ``limit`` sessions kept for ``machine`` whose working directory is
        ``root`` or below it, as dicts naming their file and agent; none without entries."""
        root = root.rstrip("/") or "/"
        below = root.rstrip("/") + "/"
        with self.db() as db:
            cursor = db.execute(
                f"SELECT file, agent, {', '.join(SESSION_COLUMNS)}, raw_size FROM transcripts"
                " WHERE machine=? AND (cwd=? OR (cwd>=? AND cwd<?)) AND (saved>0 OR EXISTS"
                " (SELECT 1 FROM entries WHERE entries.transcript=-transcripts.id))"
                " ORDER BY updated DESC LIMIT ?",
                # Every path below the root sorts from "root/" up to "root0", the next
                # character after the slash.
                (machine, root, below, below[:-1] + "0", limit),
            )
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, values, strict=True)) for values in cursor]

    def read_to(self, machine):
        """Where each of ``machine``'s files was read to, as {file: offset}: those read
        to their end whose session is recorded."""
        with self.db() as db:
            return dict(
                db.execute(
                    "SELECT file, offset FROM transcripts"
                    " WHERE machine=? AND ready=1 AND session IS NOT NULL",
                    (machine,),
                )
            )

    def save(self, transcript, generation, *, restarted, added, changed, place, ready=None):
        """Store one read's progress in a single transaction; returns the next generation.

        ``restarted`` starts again from the file's start (it was replaced or truncated, or
        the parser changed); ``added`` and ``changed`` are (index, entry JSON, search
        text) triples; ``place`` is the parser's position (inode, offset, skipping, saved,
        first, state); entries before ``first`` go. ``ready`` marks a read that reached
        the end of the file. Refused, with nothing written, unless the row is still at
        ``generation``: another reader saved it since this one began.

        What a read that reached the file's end had kept is not dropped by a restart but
        set aside, under the row's negated ID, until the new read reaches the end too:
        should the file go before that, the transcript set aside is the record
        (``whole``).
        """
        inode, offset, skipping, saved, first, state = place
        with self.write() as db:
            was = db.execute(
                "SELECT ready FROM transcripts WHERE id=? AND generation=?",
                (transcript, generation),
            ).fetchone()
            if was is None:
                raise Conflict("The transcript was read elsewhere meanwhile; refresh")
            if restarted:
                aside = db.execute(
                    "SELECT 1 FROM entries WHERE transcript=? LIMIT 1", (-transcript,)
                ).fetchone()
                if was[0] and not aside:
                    db.execute(
                        "UPDATE entries SET transcript=? WHERE transcript=?",
                        (-transcript, transcript),
                    )
                else:
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
            if ready:
                db.execute("DELETE FROM entries WHERE transcript=?", (-transcript,))
        return generation + 1

    def whole(self, row):
        """The last whole read of ``row``'s transcript, as (entries ID, first, total):
        its own entries, or those set aside while a read from the file's start has not
        reached its end."""
        if not row["ready"]:
            with self.db() as db:
                first, last = db.execute(
                    "SELECT min(idx), max(idx) FROM entries WHERE transcript=?", (-row["id"],)
                ).fetchone()
            if first is not None:
                return -row["id"], first, last + 1
        return row["id"], row["first"], row["saved"]

    def index(self, transcript, batch=INDEX_BATCH, stop=None):
        """Index ``transcript``'s unindexed entries, ``batch`` per transaction, until
        monotonic ``stop`` if one is given."""
        if not self.searchable:
            return
        while stop is None or time.monotonic() < stop:
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

    def search(self, ranked, expression, limit, stop=None):
        """Entries of the ``ranked`` transcripts (ids, best first) matching ``expression``.

        Returns (matches, counts): at most ``limit`` (transcript, index, entry, snippet)
        tuples, newest first within each transcript, and every match's count per id.
        Entries not indexed yet are indexed first, until monotonic ``stop`` if one is
        given: ``unindexed`` then names the transcripts searched only in part.
        """
        if not self.searchable:
            raise ValueError("Search needs SQLite with FTS5 and its trigram tokenizer")
        if not ranked:
            return [], {}
        for id_ in ranked:
            self.index(id_, stop=stop)
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

    def unindexed(self, transcripts):
        """Those of ``transcripts`` (ids) with entries a search does not find yet."""
        if not transcripts:
            return set()
        with self.db() as db:
            return {
                id_
                for (id_,) in db.execute(
                    "SELECT DISTINCT transcript FROM entries WHERE indexed = 0 AND transcript IN"
                    f" ({', '.join('?' for _ in transcripts)})",
                    list(transcripts),
                )
            }

    # Session files ---------------------------------------------------------------

    def kept_files(self, machine):
        """How much of each of ``machine``'s session files is kept, and of which file, as
        {file: (transcript, inode, bytes)}."""
        with self.db() as db:
            return {
                file: found
                for file, *found in db.execute(
                    "SELECT file, id, raw_inode, raw_size FROM transcripts WHERE machine=?",
                    (machine,),
                )
            }

    def keep(self, transcript, inode, offset, chunks):
        """Keep ``chunks``, the bytes of ``transcript``'s session file from ``offset`` on.

        ``offset`` is where the copy kept so far ends, or 0 to make it again, as when
        another file (by its ``inode``) took the place of the one kept. It is all
        written at once, so the copy is whole at any time. Refused, with nothing
        written, when the copy does not end at ``offset``.
        """
        with self.db() as db:
            kept = db.execute(
                "SELECT raw_inode, raw_size FROM transcripts WHERE id=?", (transcript,)
            ).fetchone()
            if kept is None or (offset and (kept[0] != inode or kept[1] != offset)):
                raise Conflict("The session file was kept elsewhere meanwhile")
            # The last piece is filled up first, so every piece but the last is whole.
            piece, held = divmod(offset, PIECE)
            unfilled = bytearray()
            if held:
                last = db.execute(
                    "SELECT data FROM raw WHERE transcript=? AND piece=?", (transcript, piece)
                ).fetchone()
                if last is None:
                    raise Conflict("The copy kept of the session file is not whole")
                unfilled += lzma.decompress(last[0])
        # Compressed before the write, which others wait behind.
        size = offset - held
        pieces = []
        for chunk in chunks:
            unfilled += chunk
            while len(unfilled) >= PIECE:
                pieces.append((piece, lzma.compress(bytes(unfilled[:PIECE]), preset=PRESET)))
                piece += 1
                size += PIECE
                del unfilled[:PIECE]
        if unfilled:
            pieces.append((piece, lzma.compress(bytes(unfilled), preset=PRESET)))
            size += len(unfilled)
        with self.write() as db:
            if not offset:
                db.execute("DELETE FROM raw WHERE transcript=?", (transcript,))
            db.executemany(
                "INSERT OR REPLACE INTO raw (transcript, piece, data) VALUES (?, ?, ?)",
                [(transcript, *found) for found in pieces],
            )
            moved = db.execute(
                "UPDATE transcripts SET raw_inode=?, raw_size=?"
                " WHERE id=? AND (?=0 OR (raw_inode IS ? AND raw_size=?))",
                (inode, size, transcript, offset, inode, offset),
            ).rowcount
            if not moved:
                raise Conflict("The session file was kept elsewhere meanwhile")

    def kept(self, transcript):
        """``transcript``'s session file as it was kept, a piece of its bytes at a time."""
        piece = 0
        while True:
            with self.db() as db:
                found = db.execute(
                    "SELECT data FROM raw WHERE transcript=? AND piece=?", (transcript, piece)
                ).fetchone()
            if found is None:
                return
            yield lzma.decompress(found[0])
            piece += 1

    def restored(self, transcript, inode, size):
        """``transcript``'s file was written back from its copy as the file ``inode``,
        the first ``size`` bytes of the copy.

        The copy is of that file now, if all of it was written. The transcript is read
        on in that file from its place, if it was read from the file the copy was made
        of and no further than was written. Any other copy or transcript starts again
        from the file's start: it names no file, as an inode can be given out again.
        """
        with self.write() as db:
            db.execute(
                "UPDATE transcripts SET"
                " inode=CASE WHEN inode IS raw_inode AND offset<=? THEN ? ELSE NULL END,"
                " raw_inode=CASE WHEN raw_size=? THEN ? ELSE NULL END,"
                " generation=generation+1 WHERE id=?",
                (size, inode, size, inode, transcript),
            )

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
        """Drop the details of files that are gone, and the oldest past the cap; at most
        once per PRUNE_EVERY. Transcripts stay: one whose file is gone is the record."""
        now = time.time() if now is None else now
        with self.lock:
            if not self.prune_due(now):
                return
            self.pruned = now
        with self.db() as db:
            files = [file for (file,) in db.execute("SELECT file FROM details")]
        stale = [file for file in files if not exists(file)]
        with self.write() as db:
            db.executemany("DELETE FROM details WHERE file=?", [(f,) for f in stale])
            db.execute(
                "DELETE FROM details WHERE rowid IN"
                " (SELECT rowid FROM details ORDER BY rowid DESC LIMIT -1 OFFSET ?)",
                (MAX_DETAILS,),
            )


def move_store(source, target):
    """Rename a store that exists, with its write-ahead log beside it: the store itself
    last, as it is what another process looks for."""
    if not source.exists():
        return
    for suffix in ("-wal", "-shm", ""):
        with contextlib.suppress(FileNotFoundError):
            os.rename(f"{source}{suffix}", f"{target}{suffix}")


def snippet_parts(text):
    """A snippet as [(text, matched)], split at its match markers."""
    parts = []
    for piece in re.split(f"({MARK[0]}[^{MARK[1]}]*{MARK[1]})", text):
        if piece.startswith(MARK[0]) and piece.endswith(MARK[1]):
            parts.append((piece[1:-1], True))
        elif piece:
            parts.append((piece.replace(MARK[0], "").replace(MARK[1], ""), False))
    return parts
