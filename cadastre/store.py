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
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version < 2:
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS partition_incarnations (
                        incarnation_id TEXT PRIMARY KEY,
                        disk_stable_id TEXT NOT NULL REFERENCES disks(stable_id),
                        device TEXT, name TEXT NOT NULL, partition_number INTEGER, slot_key TEXT NOT NULL,
                        partuuid TEXT, filesystem TEXT NOT NULL, filesystem_uuid TEXT, size_bytes INTEGER NOT NULL,
                        identity_confidence TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                        status TEXT NOT NULL, replaces TEXT, replaced_by TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_partitions_disk ON partition_incarnations(disk_stable_id);
                    CREATE INDEX IF NOT EXISTS idx_partitions_slot ON partition_incarnations(disk_stable_id, slot_key);
                    PRAGMA user_version = 2;
                    """
                )
            if version < 3:
                columns = {row["name"] for row in db.execute("PRAGMA table_info(partition_incarnations)")}
                if "lineage_id" not in columns:
                    db.execute("ALTER TABLE partition_incarnations ADD COLUMN lineage_id TEXT")
                if "start_bytes" not in columns:
                    db.execute("ALTER TABLE partition_incarnations ADD COLUMN start_bytes INTEGER NOT NULL DEFAULT 0")
                db.executescript("""
                    UPDATE partition_incarnations SET lineage_id = incarnation_id WHERE lineage_id IS NULL;
                    CREATE TABLE IF NOT EXISTS partition_geometry_observations (
                        id INTEGER PRIMARY KEY,
                        lineage_id TEXT NOT NULL,
                        disk_stable_id TEXT NOT NULL REFERENCES disks(stable_id),
                        incarnation_id TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        device TEXT,
                        partition_number INTEGER,
                        slot_key TEXT NOT NULL,
                        partuuid TEXT,
                        start_bytes INTEGER NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        partition_table TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_geometry_lineage
                        ON partition_geometry_observations(disk_stable_id, lineage_id, observed_at);
                    PRAGMA user_version = 3;
                    """)

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
            partitions = db.execute(
                "SELECT * FROM partition_incarnations WHERE disk_stable_id = ? ORDER BY status, last_seen DESC",
                (stable_id,),
            ).fetchall()
        result = self._serialize(row, attempts)
        result["partitions"] = [
            self._with_geometry(self._serialize_partition(partition, row)) for partition in partitions
        ]
        return result

    def list_disks(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM disks ORDER BY last_seen DESC").fetchall()
            attempts = db.execute(
                "SELECT stable_id, attempted_at, status, detail FROM index_attempts ORDER BY attempted_at DESC"
            ).fetchall()
            partitions = db.execute("SELECT * FROM partition_incarnations ORDER BY last_seen DESC").fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for attempt in attempts:
            grouped.setdefault(attempt["stable_id"], []).append(attempt)
        disk_rows = {row["stable_id"]: row for row in rows}
        children: dict[str, list[dict[str, Any]]] = {}
        for partition in partitions:
            children.setdefault(partition["disk_stable_id"], []).append(
                self._with_geometry(self._serialize_partition(partition, disk_rows[partition["disk_stable_id"]]))
            )
        result = [self._serialize(row, grouped.get(row["stable_id"], [])) for row in rows]
        for disk in result:
            disk["partitions"] = children.get(disk["stableId"], [])
        return result

    def add_partition(self, disk: dict[str, Any], partition: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Reconcile a disk-child occurrence using filesystem lineage when trustworthy."""
        self.add_disk(disk)
        now = datetime.now(UTC).isoformat()
        incarnation_id = partition["incarnationId"]
        lineage_id = incarnation_id
        slot_key = str(partition.get("number") or partition.get("device") or partition["name"])
        filesystem_uuid = partition.get("filesystemUuid")
        with self.connect() as db:
            if filesystem_uuid:
                matches = db.execute(
                    "SELECT incarnation_id FROM partition_incarnations WHERE disk_stable_id=? AND lower(filesystem_uuid)=lower(?) AND status='present'",  # noqa: E501
                    (disk["stableId"], filesystem_uuid),
                ).fetchall()
                if len(matches) > 1 or (len(matches) == 1 and matches[0]["incarnation_id"] != incarnation_id):
                    raise ValueError("Ambiguous duplicate filesystem UUID on one disk")
            existed = (
                db.execute("SELECT 1 FROM partition_incarnations WHERE incarnation_id = ?", (incarnation_id,)).fetchone()
                is not None
            )
            previous = (
                None
                if existed
                else db.execute(
                    "SELECT incarnation_id FROM partition_incarnations WHERE disk_stable_id=? AND slot_key=? AND incarnation_id!=? AND status='present' ORDER BY last_seen DESC LIMIT 1",  # noqa: E501
                    (disk["stableId"], slot_key, incarnation_id),
                ).fetchone()
            )
            replaces = previous["incarnation_id"] if previous else None
            if previous:
                db.execute(
                    "UPDATE partition_incarnations SET status='replaced', replaced_by=? WHERE incarnation_id=?",
                    (incarnation_id, replaces),
                )
            db.execute(
                """INSERT INTO partition_incarnations (incarnation_id,disk_stable_id,device,name,partition_number,slot_key,partuuid,filesystem,filesystem_uuid,size_bytes,identity_confidence,first_seen,last_seen,status,replaces,replaced_by,lineage_id,start_bytes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'present',?,NULL,?,?) ON CONFLICT(incarnation_id) DO UPDATE SET device=excluded.device,name=excluded.name,partition_number=excluded.partition_number,slot_key=excluded.slot_key,partuuid=excluded.partuuid,filesystem=excluded.filesystem,filesystem_uuid=excluded.filesystem_uuid,size_bytes=excluded.size_bytes,start_bytes=excluded.start_bytes,identity_confidence=excluded.identity_confidence,last_seen=excluded.last_seen,status='present',replaced_by=NULL""",  # noqa: E501
                (
                    incarnation_id,
                    disk["stableId"],
                    partition.get("device"),
                    partition["name"],
                    partition.get("number"),
                    slot_key,
                    partition.get("partuuid"),
                    partition["filesystem"],
                    filesystem_uuid,
                    partition["sizeBytes"],
                    partition["identityConfidence"],
                    now,
                    now,
                    replaces,
                    lineage_id,
                    partition.get("startBytes", 0),
                ),
            )
            geometry = (
                partition.get("device"),
                partition.get("number"),
                slot_key,
                partition.get("partuuid"),
                partition.get("startBytes", 0),
                partition["sizeBytes"],
                disk["partitionTable"],
            )
            latest = db.execute(
                "SELECT device,partition_number,slot_key,partuuid,start_bytes,size_bytes,partition_table FROM partition_geometry_observations WHERE disk_stable_id=? AND lineage_id=? ORDER BY id DESC LIMIT 1",  # noqa: E501
                (disk["stableId"], lineage_id),
            ).fetchone()
            if latest is None or tuple(latest) != geometry:
                db.execute(
                    "INSERT INTO partition_geometry_observations (lineage_id,disk_stable_id,incarnation_id,observed_at,device,partition_number,slot_key,partuuid,start_bytes,size_bytes,partition_table) VALUES (?,?,?,?,?,?,?,?,?,?,?)",  # noqa: E501
                    (lineage_id, disk["stableId"], incarnation_id, now, *geometry),
                )
        return self.get_partition(incarnation_id), not existed

    def remove_disk(self, stable_id: str) -> dict[str, Any]:
        """Remove only persisted Cadastre records for one canonical disk ID."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM disks WHERE stable_id = ?", (stable_id,)).fetchone()
            if row is None:
                raise KeyError(stable_id)
            partition_count = db.execute(
                "SELECT COUNT(*) AS count FROM partition_incarnations WHERE disk_stable_id = ?",
                (stable_id,),
            ).fetchone()["count"]
            attempt_count = db.execute(
                "SELECT COUNT(*) AS count FROM index_attempts WHERE stable_id = ?", (stable_id,)
            ).fetchone()["count"]
            db.execute("DELETE FROM partition_incarnations WHERE disk_stable_id = ?", (stable_id,))
            db.execute("DELETE FROM index_attempts WHERE stable_id = ?", (stable_id,))
            db.execute("DELETE FROM disks WHERE stable_id = ?", (stable_id,))
        return {
            "stableId": stable_id,
            "partitionRecordsRemoved": partition_count,
            "indexAttemptsRemoved": attempt_count,
        }

    def get_partition(self, incarnation_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM partition_incarnations WHERE incarnation_id = ?", (incarnation_id,)
            ).fetchone()
        if row is None:
            raise KeyError(incarnation_id)
        return self._with_geometry(self._serialize_partition(row))

    def list_partitions(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM partition_incarnations ORDER BY last_seen DESC").fetchall()
        return [self._with_geometry(self._serialize_partition(row)) for row in rows]

    def _with_geometry(self, partition: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM partition_geometry_observations WHERE disk_stable_id=? AND lineage_id=? ORDER BY observed_at",  # noqa: E501
                (partition["diskStableId"], partition["lineageId"]),
            ).fetchall()
        partition["geometryObservations"] = [
            {
                "diskStableId": r["disk_stable_id"],
                "lineageId": r["lineage_id"],
                "incarnationId": r["incarnation_id"],
                "observedAt": r["observed_at"],
                "device": r["device"],
                "number": r["partition_number"],
                "slotKey": r["slot_key"],
                "partuuid": r["partuuid"],
                "startBytes": r["start_bytes"],
                "sizeBytes": r["size_bytes"],
                "partitionTable": r["partition_table"],
            }
            for r in rows
        ]
        return partition

    @staticmethod
    def _serialize_partition(row: sqlite3.Row, disk: sqlite3.Row | None = None) -> dict[str, Any]:
        result = {
            "incarnationId": row["incarnation_id"],
            "diskStableId": row["disk_stable_id"],
            "device": row["device"],
            "name": row["name"],
            "number": row["partition_number"],
            "partuuid": row["partuuid"],
            "lineageId": row["lineage_id"],
            "startBytes": row["start_bytes"],
            "filesystem": row["filesystem"],
            "filesystemUuid": row["filesystem_uuid"],
            "sizeBytes": row["size_bytes"],
            "identityConfidence": row["identity_confidence"],
            "firstSeen": row["first_seen"],
            "lastSeen": row["last_seen"],
            "status": row["status"],
            "displayStatus": "Historical" if row["status"] != "present" else "Current",
            "disk": (
                {"stableId": disk["stable_id"], "brand": disk["brand"], "model": disk["model"], "serial": disk["serial"]}
                if disk
                else None
            ),
            "replaces": row["replaces"],
            "replacedBy": row["replaced_by"],
        }
        return result

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
