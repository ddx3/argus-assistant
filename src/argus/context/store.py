"""Storing the timeline on disk.

The in-memory timeline lives under a window, and old entries drop out of it. But what
drops out is not always garbage: a conversation from an hour-long interview may be
needed in full, to reread it, to compare two runs with different settings, or to build
a summary from it later. That is why a timeline kept by the pipeline itself (a run
without the assistant) is duplicated into SQLite. When the assistant is present, it
keeps the timeline, and it is not written to the database: such a session gets no
`timeline.db` (`Argus._open_session`); the assistant's answers are stored by the chat
(`argus/chats/`), not by this database.

SQLite rather than JSONL, for one thing: full-text search. "When he was asking about
databases" is a query, not a scroll through a file.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Self

from argus.context.timeline import Entry, Kind
from argus.core.events import Source

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id          INTEGER PRIMARY KEY,
    t           INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    confidence  REAL
);
CREATE INDEX IF NOT EXISTS entries_t ON entries(t);

CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts
USING fts5(text, content='entries', content_rowid='id', tokenize='unicode61');

CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


class TimelineStore:
    """The timeline on disk."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # The database is opened by the thread that turned recording on (the button runs on the
        # console's worker thread), written to by the event loop, and closed again by the worker
        # thread: one connection shared by several threads. So there is no thread check, but
        # there is a lock on every operation, since sqlite3 itself does not promise safe writes
        # from two threads at once.
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def add(self, entry: Entry) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO entries(t, kind, source, text, confidence) VALUES (?, ?, ?, ?, ?)",
                (entry.t, entry.kind.value, entry.source.value, entry.text, entry.confidence),
            )

    def add_many(self, entries: list[Entry]) -> None:
        with self._lock:
            for entry in entries:
                self.add(entry)

    def commit(self) -> None:
        with self._lock:
            self._db.commit()

    def all(self) -> Iterator[Entry]:
        with self._lock:
            rows = self._db.execute(
                "SELECT t, kind, source, text, confidence FROM entries ORDER BY t, id"
            ).fetchall()
        for t, kind, source, text, confidence in rows:
            yield Entry(t=t, kind=Kind(kind), source=Source(source), text=text,
                        confidence=confidence)

    def search(self, query: str, limit: int = 20) -> list[Entry]:
        """Find by words. Returns entries in time order."""
        with self._lock:
            rows = self._db.execute(
                """
                SELECT e.t, e.kind, e.source, e.text, e.confidence
                FROM entries_fts f JOIN entries e ON e.id = f.rowid
                WHERE entries_fts MATCH ?
                ORDER BY e.t LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        return [
            Entry(t=t, kind=Kind(k), source=Source(s), text=txt, confidence=c)
            for t, k, s, txt, c in rows
        ]

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.commit()
            self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
