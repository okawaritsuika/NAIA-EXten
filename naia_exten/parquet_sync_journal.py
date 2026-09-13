"""Durable, small consumption records; Parquet compaction runs off the pop path."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def _json_default(value):
    item = getattr(value, "item", None)
    return item() if callable(item) else str(value)


class ConsumptionJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS consumed (
                seq INTEGER PRIMARY KEY, target TEXT NOT NULL,
                row_key TEXT NOT NULL, payload TEXT NOT NULL,
                pending INTEGER NOT NULL DEFAULT 1,
                UNIQUE(target, row_key))""")
            db.execute("CREATE INDEX IF NOT EXISTS consumed_pending ON consumed(pending)")
            db.execute("""CREATE TABLE IF NOT EXISTS replacement_receipts (
                target TEXT PRIMARY KEY, digest TEXT NOT NULL, sequences TEXT NOT NULL)""")

    @contextmanager
    def _connect(self):
        # FULL commits preserve an acknowledged pop across abrupt app exit.
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def target_key(path):
        return str(Path(path).resolve())

    def record(self, paths, row_key, row_id, fallback):
        payload = json.dumps([row_id, fallback], ensure_ascii=False, default=_json_default)
        with self._connect() as db:
            db.executemany(
                "INSERT OR IGNORE INTO consumed(target,row_key,payload) VALUES(?,?,?)",
                [(self.target_key(path), row_key, payload) for path in paths],
            )

    def records_for(self, paths):
        found = {}
        with self._connect() as db:
            for path in paths:
                for key, payload in db.execute(
                    "SELECT row_key,payload FROM consumed WHERE target=?", (self.target_key(path),)
                ):
                    found[key] = json.loads(payload)
        return found

    def pending(self):
        grouped = {}
        with self._connect() as db:
            for seq, target, payload in db.execute(
                "SELECT seq,target,payload FROM consumed WHERE pending=1 ORDER BY seq"
            ):
                grouped.setdefault(target, []).append((seq, *json.loads(payload)))
        return grouped

    def acknowledge(self, sequences):
        # Keep tombstones after compaction: old in-memory/last-search snapshots
        # must not resurrect these rows when a filtered pool is restored.
        with self._connect() as db:
            db.executemany("UPDATE consumed SET pending=0 WHERE seq=?", [(seq,) for seq in sequences])

    def stage_replacement(self, target, digest, sequences):
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO replacement_receipts VALUES(?,?,?)",
                       (self.target_key(target), digest, json.dumps(sequences)))

    def receipt(self, target):
        with self._connect() as db:
            row = db.execute("SELECT digest,sequences FROM replacement_receipts WHERE target=?",
                             (self.target_key(target),)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def clear_receipt(self, target):
        with self._connect() as db:
            db.execute("DELETE FROM replacement_receipts WHERE target=?", (self.target_key(target),))
