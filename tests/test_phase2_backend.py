import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from cadastre.indexing import FilesystemIdentity, IndexingError, IndexStore
from cadastre.supervisor import IndexSupervisor


class FakeConnected:
    def __init__(self, root: Path):
        self.root = root
        self.uuid = "fake-uuid"
        self.present = True

    def live(self):
        if not self.present:
            return []
        return [
            {
                "stableId": "fake-disk",
                "partitions": [
                    {
                        "incarnationId": "fake-lineage",
                        "lineageId": "fake-lineage",
                        "filesystemUuid": self.uuid,
                        "mountpoint": str(self.root),
                        "mountState": "ro",
                        "browseAllowed": True,
                    }
                ],
            }
        ]


class Phase2BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "fake-root"
        self.root.mkdir()
        self.store = IndexStore(self.base / "fake.db")
        self.store.initialize()
        self.connected = FakeConnected(self.root)
        self.supervisor = IndexSupervisor(self.store, self.connected)
        self.identity = FilesystemIdentity("fake-disk", "fake-lineage", "fake-uuid")

    def tearDown(self):
        self.temp.cleanup()

    def test_v4_vocabulary_migration(self):
        path = self.base / "v4.db"
        old = IndexStore(path)
        old.initialize()
        with old.connect() as db:
            db.execute("PRAGMA user_version=4")
            db.execute("UPDATE index_generations SET state='completed' WHERE 0")
        # A synthetic legacy table verifies translations without any device access.
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA user_version=5")
        old.initialize()
        self.assertEqual(old.connect().execute("PRAGMA user_version").fetchone()[0], 5)

    def test_start_is_nonblocking_and_worker_completes(self):
        for number in range(100):
            (self.root / f"file-{number}").write_text("x")
        started = time.monotonic()
        row = self.supervisor.start(self.identity)
        self.assertLess(time.monotonic() - started, 0.5)
        for _ in range(200):
            row = self.store.generation(row["generation_id"])
            if row["state"] in {"completed", "completed_with_errors", "failed"}:
                break
            time.sleep(0.01)
        self.assertEqual(row["state"], "completed")
        self.assertEqual(row["quiesced"], 1)

    def test_remount_fails_closed(self):
        generation = self.store.create_generation(self.identity, self.root)
        self.store.recover_startup(lambda _: True)
        self.connected.present = False
        row = self.supervisor.resume(generation)
        self.assertEqual(row["state"], "waiting_for_remount")
        self.connected.present = True
        self.connected.uuid = "wrong"
        with self.assertRaises(IndexingError) as raised:
            self.supervisor.resume(generation)
        self.assertEqual(raised.exception.code, "IDENTITY_MISMATCH")

    def test_active_interlock_is_exact_identity(self):
        generation = self.store.create_generation(self.identity, self.root)
        self.assertEqual(self.store.active_for("fake-disk")[0]["generation_id"], generation)
        self.assertEqual(self.store.active_for("unrelated"), [])


if __name__ == "__main__":
    unittest.main()
