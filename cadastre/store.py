"""SQLite persistence for observed disks."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS disks (
                    stable_id TEXT PRIMARY KEY, device TEXT NOT NULL, brand TEXT NOT NULL,
                    model TEXT NOT NULL, size_bytes INTEGER NOT NULL, serial TEXT NOT NULL,
                    partition_table TEXT NOT NULL, filesystems_json TEXT NOT NULL,
                    smart_status TEXT NOT NULL, smart_detail TEXT, transport TEXT NOT NULL,
                    removable INTEGER NOT NULL, read_only INTEGER NOT NULL,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS index_attempts (
                    id INTEGER PRIMARY KEY, stable_id TEXT NOT NULL REFERENCES disks(stable_id),
                    attempted_at TEXT NOT NULL, status TEXT NOT NULL, detail TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_attempts_disk ON index_attempts(stable_id);
                """
            )

    def add_disk(self, disk: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            existed = db.execute("SELECT 1 FROM disks WHERE stable_id = ?", (disk["stableId"],)).fetchone() is not None
            db.execute(
                """INSERT INTO disks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(stable_id) DO UPDATE SET device=excluded.device, brand=excluded.brand,
                model=excluded.model, size_bytes=excluded.size_bytes, serial=excluded.serial,
                partition_table=excluded.partition_table, filesystems_json=excluded.filesystems_json,
                smart_status=excluded.smart_status, smart_detail=excluded.smart_detail,
                transport=excluded.transport, removable=excluded.removable,
                read_only=excluded.read_only, last_seen=excluded.last_seen""",
                (
                    disk["stableId"],
                    disk["device"],
                    disk["brand"],
                    disk["model"],
                    disk["sizeBytes"],
                    disk["serial"],
                    disk["partitionTable"],
                    json.dumps(disk["filesystems"]),
                    disk["smartStatus"],
                    disk.get("smartDetail"),
                    disk["transport"],
                    disk["removable"],
                    disk["readOnly"],
                    now,
                    now,
                ),
            )
        return self.get_disk(disk["stableId"]), not existed

    def get_disk(self, stable_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM disks WHERE stable_id = ?", (stable_id,)).fetchone()
            if row is None:
                raise KeyError(stable_id)
            attempts = db.execute(
                "SELECT attempted_at, status, detail FROM index_attempts WHERE stable_id = ? ORDER BY attempted_at DESC",
                (stable_id,),
            ).fetchall()
        return self._serialize(row, attempts)

    def list_disks(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM disks ORDER BY last_seen DESC").fetchall()
            attempts = db.execute(
                "SELECT stable_id, attempted_at, status, detail FROM index_attempts ORDER BY attempted_at DESC"
            ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for attempt in attempts:
            grouped.setdefault(attempt["stable_id"], []).append(attempt)
        return [self._serialize(row, grouped.get(row["stable_id"], [])) for row in rows]

    @staticmethod
    def _serialize(row: sqlite3.Row, attempts: list[sqlite3.Row]) -> dict[str, Any]:
        index_attempts = [{"date": a["attempted_at"], "status": a["status"], "detail": a["detail"]} for a in attempts]
        return {
            "stableId": row["stable_id"],
            "device": row["device"],
            "brand": row["brand"],
            "model": row["model"],
            "sizeBytes": row["size_bytes"],
            "serial": row["serial"],
            "partitionTable": row["partition_table"],
            "filesystems": json.loads(row["filesystems_json"]),
            "smartStatus": row["smart_status"],
            "smartDetail": row["smart_detail"],
            "transport": row["transport"],
            "removable": bool(row["removable"]),
            "readOnly": bool(row["read_only"]),
            "firstSeen": row["first_seen"],
            "lastSeen": row["last_seen"],
            "indexAttempts": index_attempts,
            "indexDates": [a["date"] for a in index_attempts if a["status"] == "succeeded"],
        }
