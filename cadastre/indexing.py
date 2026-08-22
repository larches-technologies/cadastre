"""Durable metadata-only indexing over a caller-supplied root."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import duckdb

SCHEMA_VERSION = 5
MAX_QUERY_LIMIT = 200
MAX_ERROR_DETAIL = 1000
RESUMABLE_STATES = ("paused", "interrupted", "waiting_for_media")


class IndexingError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _lease_time() -> str:
    return (datetime.now(UTC) + timedelta(seconds=30)).isoformat()


def _relative_text(value: str | PurePosixPath) -> str:
    text = str(value)
    if text in ("", "."):
        return ""
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        raise IndexingError("INVALID_RELATIVE_PATH", "Path must be contained and relative")
    return path.as_posix()


def _contained(root: Path, relative: str) -> Path:
    relative = _relative_text(relative)
    candidate = root.joinpath(*PurePosixPath(relative).parts) if relative else root
    if os.path.commonpath((os.path.abspath(root), os.path.abspath(candidate))) != os.path.abspath(root):
        raise IndexingError("PATH_ESCAPES_ROOT", "Path escapes supplied root")
    return candidate


@dataclass(frozen=True)
class FilesystemIdentity:
    disk_stable_id: str
    lineage_id: str
    filesystem_uuid: str

    def __post_init__(self) -> None:
        if not all(self.values()):
            raise ValueError("Exact disk stable ID, lineage ID, and filesystem UUID are required")

    def values(self) -> tuple[str, str, str]:
        return self.disk_stable_id, self.lineage_id, self.filesystem_uuid


class IndexStore:
    """SQLite coordinator and immutable fragment manifest."""

    def __init__(self, db_path: str | Path, fragment_root: str | Path | None = None):
        self.db_path = Path(db_path)
        self.fragment_root = Path(fragment_root) if fragment_root else Path(f"{self.db_path}.fragments")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def initialize(self) -> None:
        """Add/reconcile indexing schema without changing v1-v3 inventory rows."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.fragment_root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"database schema {version} is newer than supported {SCHEMA_VERSION}")
            if version == 4:
                self._migrate_v4(db)
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS index_generations (
                    generation_id TEXT PRIMARY KEY,
                    disk_stable_id TEXT NOT NULL,
                    lineage_id TEXT NOT NULL,
                    filesystem_uuid TEXT NOT NULL,
                    source_root TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN (
                        'queued','starting','running','pause_requested','paused','waiting_for_remount',
                        'stopping','stopped','interrupted','completed','completed_with_errors','failed'
                    )),
                    is_partial INTEGER NOT NULL DEFAULT 1 CHECK (is_partial IN (0,1)),
                    control TEXT CHECK (control IN ('pause','stop') OR control IS NULL),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    heartbeat_at TEXT,
                    entries_seen INTEGER NOT NULL DEFAULT 0 CHECK (entries_seen >= 0),
                    files_seen INTEGER NOT NULL DEFAULT 0 CHECK (files_seen >= 0),
                    directories_seen INTEGER NOT NULL DEFAULT 0 CHECK (directories_seen >= 0),
                    logical_bytes INTEGER NOT NULL DEFAULT 0 CHECK (logical_bytes >= 0),
                    detail TEXT,
                    current_relative_path TEXT,
                    quiesced INTEGER NOT NULL DEFAULT 0 CHECK (quiesced IN (0,1))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_generation
                    ON index_generations((1))
                    WHERE state IN ('queued','starting','running','pause_requested','stopping');
                CREATE INDEX IF NOT EXISTS idx_generation_identity
                    ON index_generations(disk_stable_id,lineage_id,filesystem_uuid);
                CREATE TABLE IF NOT EXISTS index_pending_directories (
                    generation_id TEXT NOT NULL REFERENCES index_generations(generation_id) ON DELETE CASCADE,
                    relative_path TEXT NOT NULL,
                    queued_at TEXT NOT NULL,
                    PRIMARY KEY (generation_id,relative_path)
                );
                CREATE TABLE IF NOT EXISTS index_fragments (
                    fragment_id INTEGER PRIMARY KEY,
                    generation_id TEXT NOT NULL REFERENCES index_generations(generation_id) ON DELETE CASCADE,
                    relative_path TEXT NOT NULL,
                    row_count INTEGER NOT NULL CHECK (row_count >= 0),
                    logical_bytes INTEGER NOT NULL CHECK (logical_bytes >= 0),
                    registered_at TEXT NOT NULL,
                    UNIQUE (generation_id,relative_path)
                );
                CREATE TABLE IF NOT EXISTS index_path_errors (
                    error_id INTEGER PRIMARY KEY,
                    generation_id TEXT NOT NULL REFERENCES index_generations(generation_id) ON DELETE CASCADE,
                    relative_path TEXT NOT NULL,
                    error_code TEXT NOT NULL,
                    detail TEXT NOT NULL CHECK (length(detail) <= 1000),
                    observed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_index_errors_generation
                    ON index_path_errors(generation_id,error_id);
                """
            )
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _migrate_v4(db: sqlite3.Connection) -> None:
        """Rebuild only the v4 coordinator table to lock the Phase 2 state vocabulary."""
        db.executescript("""
            DROP INDEX IF EXISTS idx_one_active_generation;
            ALTER TABLE index_generations RENAME TO index_generations_v4;
            CREATE TABLE index_generations (
                generation_id TEXT PRIMARY KEY, disk_stable_id TEXT NOT NULL, lineage_id TEXT NOT NULL,
                filesystem_uuid TEXT NOT NULL, source_root TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('queued','starting','running','pause_requested','paused',
                    'waiting_for_remount','stopping','stopped','interrupted','completed',
                    'completed_with_errors','failed')),
                is_partial INTEGER NOT NULL DEFAULT 1 CHECK (is_partial IN (0,1)),
                control TEXT CHECK (control IN ('pause','stop') OR control IS NULL),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT,
                lease_owner TEXT, lease_expires_at TEXT, heartbeat_at TEXT,
                entries_seen INTEGER NOT NULL DEFAULT 0 CHECK(entries_seen>=0),
                files_seen INTEGER NOT NULL DEFAULT 0 CHECK(files_seen>=0),
                directories_seen INTEGER NOT NULL DEFAULT 0 CHECK(directories_seen>=0),
                logical_bytes INTEGER NOT NULL DEFAULT 0 CHECK(logical_bytes>=0), detail TEXT,
                current_relative_path TEXT, quiesced INTEGER NOT NULL DEFAULT 0 CHECK(quiesced IN (0,1))
            );
            INSERT INTO index_generations
            SELECT generation_id,disk_stable_id,lineage_id,filesystem_uuid,source_root,
                CASE state WHEN 'waiting_for_media' THEN 'waiting_for_remount'
                           WHEN 'stop_requested' THEN 'stopping' ELSE state END,
                is_partial,control,created_at,updated_at,finished_at,lease_owner,lease_expires_at,
                heartbeat_at,entries_seen,files_seen,directories_seen,logical_bytes,detail,NULL,
                CASE WHEN state IN ('paused','stopped','interrupted','waiting_for_media','completed','failed')
                     THEN 1 ELSE 0 END
            FROM index_generations_v4;
            DROP TABLE index_generations_v4;
            CREATE UNIQUE INDEX idx_one_active_generation ON index_generations((1))
                WHERE state IN ('queued','starting','running','pause_requested','stopping');
            CREATE INDEX IF NOT EXISTS idx_generation_identity
                ON index_generations(disk_stable_id,lineage_id,filesystem_uuid);
        """)

    def create_generation(self, identity: FilesystemIdentity, root: Path) -> str:
        generation_id = uuid.uuid4().hex
        timestamp = _now()
        with self.connect() as db:
            try:
                db.execute(
                    """INSERT INTO index_generations(
                        generation_id,disk_stable_id,lineage_id,filesystem_uuid,source_root,
                        state,created_at,updated_at
                    ) VALUES(?,?,?,?,?,'queued',?,?)""",
                    (generation_id, *identity.values(), str(root), timestamp, timestamp),
                )
            except sqlite3.IntegrityError as error:
                raise IndexingError("INDEX_ACTIVE", "Another generation is active") from error
            db.execute("INSERT INTO index_pending_directories VALUES(?,?,?)", (generation_id, "", timestamp))
        return generation_id

    def generation(self, generation_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM index_generations WHERE generation_id=?", (generation_id,)).fetchone()
        if row is None:
            raise IndexingError("INDEX_NOT_FOUND", "Generation not found")
        return dict(row)

    def generations(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), MAX_QUERY_LIMIT))
        with self.connect() as db:
            rows = db.execute("SELECT * FROM index_generations ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def active_for(self, disk_stable_id: str, lineage_id: str | None = None) -> list[dict[str, Any]]:
        sql = """SELECT * FROM index_generations WHERE disk_stable_id=?
            AND (state IN ('queued','starting','running','pause_requested','stopping') OR quiesced=0)"""
        parameters: list[Any] = [disk_stable_id]
        if lineage_id is not None:
            sql += " AND lineage_id=?"
            parameters.append(lineage_id)
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, parameters).fetchall()]

    def set_control(self, generation_id: str, control: Literal["pause", "stop"]) -> None:
        state = "pause_requested" if control == "pause" else "stopping"
        with self.connect() as db:
            changed = db.execute(
                """UPDATE index_generations SET state=?,control=?,updated_at=?
                WHERE generation_id=? AND state IN ('queued','starting','running')""",
                (state, control, _now(), generation_id),
            ).rowcount
        if not changed:
            raise IndexingError("INVALID_CONTROL", "Generation is not running")

    def recover_startup(self, available: Callable[[FilesystemIdentity], bool]) -> list[str]:
        recovered: list[str] = []
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM index_generations
                WHERE state IN ('queued','starting','running','pause_requested','stopping')"""
            ).fetchall()
            for row in rows:
                identity = FilesystemIdentity(row["disk_stable_id"], row["lineage_id"], row["filesystem_uuid"])
                state = "interrupted" if available(identity) else "waiting_for_remount"
                db.execute(
                    """UPDATE index_generations SET state=?,control=NULL,lease_owner=NULL,
                    lease_expires_at=NULL,heartbeat_at=NULL,quiesced=1,updated_at=?,detail=? WHERE generation_id=?""",
                    (state, _now(), "Recovered after an unclean worker exit", row["generation_id"]),
                )
                recovered.append(row["generation_id"])
        return recovered

    def registered_fragments(self, generation_id: str) -> list[Path]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT relative_path FROM index_fragments WHERE generation_id=? ORDER BY fragment_id",
                (generation_id,),
            ).fetchall()
        paths: list[Path] = []
        root = self.fragment_root.resolve()
        for row in rows:
            path = (self.fragment_root / _relative_text(row["relative_path"])).resolve()
            if root not in path.parents:
                raise IndexingError("INVALID_FRAGMENT_MANIFEST", "Registered fragment escapes its root")
            if path.suffix != ".parquet" or not path.is_file():
                raise IndexingError("MISSING_FRAGMENT", "A registered fragment is unavailable")
            paths.append(path)
        return paths


class MetadataEngine:
    """Synchronous cooperative walker for an explicitly supplied root."""

    def __init__(self, store: IndexStore, root: str | Path, identity: FilesystemIdentity, batch_size: int = 500):
        self.store = store
        self.root = Path(root)
        self.identity = identity
        self.batch_size = max(1, batch_size)
        if not self.root.is_dir() or self.root.is_symlink():
            raise ValueError("root must be an existing, non-symlink directory")

    def start(self) -> str:
        return self.store.create_generation(self.identity, self.root)

    def resume(self, generation_id: str) -> None:
        row = self.store.generation(generation_id)
        expected = (row["disk_stable_id"], row["lineage_id"], row["filesystem_uuid"])
        if expected != self.identity.values() or Path(row["source_root"]) != self.root:
            raise IndexingError("IDENTITY_MISMATCH", "Generation identity or root does not match")
        if row["state"] not in RESUMABLE_STATES:
            raise IndexingError("NOT_RESUMABLE", "Generation is not resumable")
        with self.store.connect() as db:
            db.execute(
                """UPDATE index_generations SET state='starting',control=NULL,quiesced=0,
                detail=NULL,updated_at=? WHERE generation_id=?""",
                (_now(), generation_id),
            )

    def run(self, generation_id: str, max_directories: int | None = None) -> dict[str, Any]:
        self._validate(self.store.generation(generation_id))
        processed = 0
        with self.store.connect() as db:
            db.execute(
                """UPDATE index_generations SET state='running',lease_owner=?,lease_expires_at=?,
                heartbeat_at=?,quiesced=0,updated_at=? WHERE generation_id=?""",
                (uuid.uuid4().hex, _lease_time(), _now(), _now(), generation_id),
            )
        while max_directories is None or processed < max_directories:
            state = self.store.generation(generation_id)
            if state["control"]:
                self._acknowledge_control(generation_id, state["control"])
                return self.store.generation(generation_id)
            pending = self._next_pending(generation_id)
            if pending is None:
                self._complete(generation_id)
                return self.store.generation(generation_id)
            self._scan_directory(generation_id, pending)
            processed += 1
        return self.store.generation(generation_id)

    def _validate(self, row: Mapping[str, Any]) -> None:
        expected = (row["disk_stable_id"], row["lineage_id"], row["filesystem_uuid"])
        if expected != self.identity.values() or Path(row["source_root"]) != self.root:
            raise IndexingError("IDENTITY_MISMATCH", "Generation identity or root does not match")
        if row["state"] not in ("queued", "starting", "running", "pause_requested", "stopping"):
            raise IndexingError("NOT_RUNNING", "Generation is not running")

    def _next_pending(self, generation_id: str) -> str | None:
        with self.store.connect() as db:
            row = db.execute(
                """SELECT relative_path FROM index_pending_directories
                WHERE generation_id=? ORDER BY relative_path LIMIT 1""",
                (generation_id,),
            ).fetchone()
        return None if row is None else row["relative_path"]

    def _scan_directory(self, generation_id: str, relative: str) -> None:
        rows: list[tuple[Any, ...]] = []
        child_directories: list[str] = []
        try:
            with os.scandir(_contained(self.root, relative)) as entries:
                for entry in entries:
                    child = PurePosixPath(relative) / entry.name if relative else PurePosixPath(entry.name)
                    child_text = _relative_text(child)
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                        entry_type = _entry_type(metadata.st_mode)
                        size = metadata.st_size if entry_type == "file" else 0
                        rows.append(
                            (
                                child_text,
                                relative,
                                entry.name,
                                entry_type,
                                size,
                                metadata.st_mtime,
                                datetime.fromtimestamp(metadata.st_mtime, UTC).isoformat(),
                                metadata.st_mode,
                                metadata.st_ino,
                                _now(),
                            )
                        )
                        if entry_type == "directory":
                            child_directories.append(child_text)
                    except OSError as error:
                        self._record_error(generation_id, child_text, error)
        except OSError as error:
            self._record_error(generation_id, relative, error)
        self._publish(generation_id, rows)
        timestamp = _now()
        with self.store.connect() as db:
            db.executemany(
                "INSERT OR IGNORE INTO index_pending_directories VALUES(?,?,?)",
                ((generation_id, child, timestamp) for child in child_directories),
            )
            db.execute(
                "DELETE FROM index_pending_directories WHERE generation_id=? AND relative_path=?",
                (generation_id, relative),
            )
            db.execute(
                """UPDATE index_generations SET entries_seen=entries_seen+?,files_seen=files_seen+?,
                directories_seen=directories_seen+?,logical_bytes=logical_bytes+?,current_relative_path=?,
                heartbeat_at=?,lease_expires_at=?,updated_at=?
                WHERE generation_id=?""",
                (
                    len(rows),
                    sum(row[3] == "file" for row in rows),
                    sum(row[3] == "directory" for row in rows),
                    sum(row[4] for row in rows),
                    relative,
                    timestamp,
                    _lease_time(),
                    timestamp,
                    generation_id,
                ),
            )

    def _publish(self, generation_id: str, rows: Sequence[tuple[Any, ...]]) -> None:
        if not rows:
            return
        directory = self.store.fragment_root / generation_id
        directory.mkdir(parents=True, exist_ok=True)
        name = f"fragment-{uuid.uuid4().hex}.parquet"
        final, temporary = directory / name, directory / f".{name}.tmp"
        connection = duckdb.connect(":memory:")
        try:
            connection.execute("""CREATE TABLE entries(relative_path VARCHAR,parent_path VARCHAR,name VARCHAR,
                entry_type VARCHAR,size_bytes UBIGINT,mtime_epoch DOUBLE,mtime_utc VARCHAR,mode UBIGINT,
                inode UBIGINT,observed_at VARCHAR)""")
            connection.executemany("INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            connection.execute("COPY entries TO ? (FORMAT PARQUET, COMPRESSION ZSTD)", [str(temporary)])
        finally:
            connection.close()
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, final)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO index_fragments(
                    generation_id,relative_path,row_count,logical_bytes,registered_at
                ) VALUES(?,?,?,?,?)""",
                (
                    generation_id,
                    final.relative_to(self.store.fragment_root).as_posix(),
                    len(rows),
                    sum(row[4] for row in rows),
                    _now(),
                ),
            )

    def _record_error(self, generation_id: str, relative: str, error: OSError) -> None:
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO index_path_errors(
                    generation_id,relative_path,error_code,detail,observed_at
                ) VALUES(?,?,?,?,?)""",
                (generation_id, relative, error.__class__.__name__, str(error)[:MAX_ERROR_DETAIL], _now()),
            )

    def _acknowledge_control(self, generation_id: str, control: str) -> None:
        state, finished = ("paused", None) if control == "pause" else ("stopped", _now())
        with self.store.connect() as db:
            db.execute(
                """UPDATE index_generations SET state=?,control=NULL,lease_owner=NULL,
                lease_expires_at=NULL,heartbeat_at=NULL,quiesced=1,finished_at=?,updated_at=?
                WHERE generation_id=?""",
                (state, finished, _now(), generation_id),
            )

    def _complete(self, generation_id: str) -> None:
        timestamp = _now()
        with self.store.connect() as db:
            pending = db.execute(
                "SELECT COUNT(*) FROM index_pending_directories WHERE generation_id=?", (generation_id,)
            ).fetchone()[0]
            if pending:
                raise RuntimeError("cannot complete while pending work remains")
            db.execute(
                """UPDATE index_generations SET state=CASE WHEN EXISTS (
                    SELECT 1 FROM index_path_errors e WHERE e.generation_id=index_generations.generation_id
                ) THEN 'completed_with_errors' ELSE 'completed' END,is_partial=0,control=NULL,
                lease_owner=NULL,lease_expires_at=NULL,heartbeat_at=NULL,quiesced=1,finished_at=?,updated_at=?
                WHERE generation_id=?""",
                (timestamp, timestamp, generation_id),
            )


def _entry_type(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "special"


class IndexQuery:
    """Bounded queries over exactly the registered, contained fragments."""

    def __init__(self, store: IndexStore):
        self.store = store

    def search(
        self, generation_id: str, text: str = "", limit: int = 50, timeout_seconds: float = 2.0
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), MAX_QUERY_LIMIT))
        fragments = self.store.registered_fragments(generation_id)
        generation = self.store.generation(generation_id)
        if not fragments:
            return {"partial": bool(generation["is_partial"]), "rows": []}
        sql = """SELECT relative_path,parent_path,name,entry_type,size_bytes,mtime_utc
            FROM read_parquet(?) WHERE name ILIKE ? ESCAPE '\\' ORDER BY relative_path LIMIT ?"""
        rows = self._execute(sql, ([str(path) for path in fragments], f"%{_escape_like(text)}%", limit), timeout_seconds)
        return {"partial": bool(generation["is_partial"]), "rows": rows}

    def summary(self, generation_id: str, timeout_seconds: float = 2.0) -> dict[str, Any]:
        fragments = self.store.registered_fragments(generation_id)
        generation = self.store.generation(generation_id)
        counts = (
            self._execute(
                """SELECT entry_type,COUNT(*) AS entries,
            COALESCE(SUM(size_bytes),0) AS logical_bytes FROM read_parquet(?)
            GROUP BY entry_type ORDER BY entry_type""",
                ([str(path) for path in fragments],),
                timeout_seconds,
            )
            if fragments
            else []
        )
        return {"partial": bool(generation["is_partial"]), "counts": counts}

    @staticmethod
    def _execute(sql: str, parameters: Sequence[Any], timeout_seconds: float) -> list[dict[str, Any]]:
        if timeout_seconds <= 0:
            raise IndexingError("QUERY_TIMEOUT", "Query timeout must be positive")
        connection = duckdb.connect(":memory:")
        expired = threading.Event()

        def cancel() -> None:
            expired.set()
            connection.interrupt()

        timer = threading.Timer(timeout_seconds, cancel)
        timer.start()
        try:
            cursor = connection.execute(sql, parameters)
            columns = [description[0] for description in cursor.description]
            return [dict(zip(columns, values, strict=True)) for values in cursor.fetchall()]
        except duckdb.Error as error:
            if expired.is_set():
                raise IndexingError("QUERY_TIMEOUT", "Query exceeded its deadline") from error
            raise
        finally:
            timer.cancel()
            connection.close()


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
