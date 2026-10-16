"""Private, bounded local state and a single-writer lock."""

import fcntl
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .models import SyncError, encoded


def private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(data, output, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, OSError) as exc:
        raise SyncError(f"Cannot read {path.name}; restore its backup rather than resetting state") from exc


@contextmanager
def writer_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "sync.lock").open("a") as handle:
        os.chmod(directory / "sync.lock", 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncError("Another Procareberry process holds this data directory") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Ledger:
    def __init__(self, path: Path, binding: str):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise SyncError("Unsupported ledger version; restore a compatible backup")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                source_key TEXT PRIMARY KEY, event_day TEXT NOT NULL, state TEXT NOT NULL,
                signature TEXT NOT NULL, body TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS event_day_index ON events(event_day);
            PRAGMA user_version=1;
        """)
        stored = self.db.execute("SELECT value FROM metadata WHERE key='binding'").fetchone()
        if stored and stored[0] != binding:
            self.db.close()
            raise SyncError(
                "This ledger belongs to different accounts; use their original configuration or a separate data directory"
            )
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('binding', ?)", (binding,))
        self.db.commit()

    def get(self, source_key: str) -> dict | None:
        row = self.db.execute("SELECT * FROM events WHERE source_key=?", (source_key,)).fetchone()
        return {**dict(row), "body": json.loads(row["body"])} if row else None

    def put(self, key: str, day: str, state: str, signature: str, body: dict):
        self.db.execute(
            "INSERT INTO events VALUES(?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET event_day=excluded.event_day,state=excluded.state,signature=excluded.signature,body=excluded.body",
            (key, day, state, signature, encoded(body)),
        )
        self.db.commit()

    def prune(self, today):
        cutoff = (today - timedelta(days=89)).isoformat()
        self.db.execute("DELETE FROM events WHERE event_day < ?", (cutoff,))
        self.db.commit()
        last = self.db.execute("SELECT value FROM metadata WHERE key='compacted'").fetchone()
        if not last or (today - datetime.fromisoformat(last[0]).date()).days >= 7:
            self.db.execute("VACUUM")
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('compacted', ?)", (today.isoformat(),))
            self.db.commit()

    def counts(self):
        return dict(self.db.execute("SELECT state,count(*) FROM events GROUP BY state"))

    def close(self):
        self.db.close()
