"""Non-blocking indexing worker supervision and server-side remount validation."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from cadastre.indexing import FilesystemIdentity, IndexingError, IndexStore, MetadataEngine


class IndexSupervisor:
    """One-process, one-active-worker supervisor; discovery never runs under its lock."""

    def __init__(self, store: IndexStore, connected: Any):
        self.store = store
        self.connected = connected
        self._lock = threading.Lock()
        self._workers: dict[str, threading.Thread] = {}
        self.store.recover_startup(self._identity_available)

    def _identity_available(self, identity: FilesystemIdentity) -> bool:
        try:
            self.resolve(identity)
        except IndexingError:
            return False
        return True

    def resolve(self, identity: FilesystemIdentity) -> Path:
        try:
            disks = self.connected.live()
        except Exception as error:
            raise IndexingError("REMOUNT_UNAVAILABLE", "Connected-device discovery is unavailable") from error
        disk = next((item for item in disks if item.get("stableId") == identity.disk_stable_id), None)
        if disk is None:
            raise IndexingError("REMOUNT_UNAVAILABLE", "The indexed disk is not connected")
        candidates = [
            part
            for part in disk.get("partitions", [])
            if part.get("lineageId") == identity.lineage_id
            or part.get("lineage_id") == identity.lineage_id
            or part.get("incarnationId") == identity.lineage_id
        ]
        if not candidates:
            raise IndexingError("IDENTITY_MISMATCH", "Persisted partition lineage is not present")
        part = candidates[0]
        if part.get("filesystemUuid") != identity.filesystem_uuid:
            raise IndexingError("IDENTITY_MISMATCH", "Filesystem UUID does not match the indexed generation")
        root = part.get("mountpoint")
        if not root or part.get("mountState") != "ro" or not part.get("browseAllowed"):
            raise IndexingError("REMOUNT_UNAVAILABLE", "Exact filesystem is not authorized at a read-only mount")
        path = Path(root)
        if not path.is_dir() or path.is_symlink():
            raise IndexingError("STALE_MOUNT", "Freshly discovered mount root is stale or unsafe")
        return path

    def start(self, identity: FilesystemIdentity) -> dict[str, Any]:
        root = self.resolve(identity)
        engine = MetadataEngine(self.store, root, identity)
        generation_id = engine.start()
        self._launch(generation_id, engine)
        return self.store.generation(generation_id)

    def resume(self, generation_id: str) -> dict[str, Any]:
        row = self.store.generation(generation_id)
        identity = FilesystemIdentity(row["disk_stable_id"], row["lineage_id"], row["filesystem_uuid"])
        try:
            root = self.resolve(identity)
        except IndexingError as error:
            if error.code == "REMOUNT_UNAVAILABLE":
                with self.store.connect() as db:
                    db.execute(
                        """UPDATE index_generations SET state='waiting_for_remount',quiesced=1,
                        detail=?,updated_at=datetime('now') WHERE generation_id=?""",
                        (str(error), generation_id),
                    )
                return self.store.generation(generation_id)
            raise
        engine = MetadataEngine(self.store, root, identity)
        engine.resume(generation_id)
        self._launch(generation_id, engine)
        return self.store.generation(generation_id)

    def control(self, generation_id: str, control: str) -> dict[str, Any]:
        if control not in {"pause", "stop"}:
            raise IndexingError("INVALID_CONTROL", "Unsupported index control request")
        self.store.set_control(generation_id, control)  # type: ignore[arg-type]
        return self.store.generation(generation_id)

    def _launch(self, generation_id: str, engine: MetadataEngine) -> None:
        with self._lock:
            if any(worker.is_alive() for worker in self._workers.values()):
                raise IndexingError("INDEX_ACTIVE", "Another worker is active")
            worker = threading.Thread(
                target=self._run, args=(generation_id, engine), name=f"cadastre-index-{generation_id[:8]}", daemon=True
            )
            self._workers[generation_id] = worker
            worker.start()

    def _run(self, generation_id: str, engine: MetadataEngine) -> None:
        try:
            engine.run(generation_id)
        except Exception as error:
            with self.store.connect() as db:
                db.execute(
                    """UPDATE index_generations SET state='failed',quiesced=1,control=NULL,
                    lease_owner=NULL,lease_expires_at=NULL,heartbeat_at=NULL,detail=?,updated_at=datetime('now')
                    WHERE generation_id=?""",
                    (str(error)[:1000], generation_id),
                )
        finally:
            with self._lock:
                self._workers.pop(generation_id, None)
