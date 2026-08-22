import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cadastre.indexing import FilesystemIdentity, IndexingError, IndexQuery, IndexStore, MetadataEngine, _contained


class IndexingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "fake-root"
        self.root.mkdir()
        self.store = IndexStore(self.base / "cadastre.sqlite")
        self.store.initialize()
        self.identity = FilesystemIdentity("fake-disk", "fake-lineage", "fake-uuid")

    def tearDown(self):
        self.temp.cleanup()

    def engine(self):
        return MetadataEngine(self.store, self.root, self.identity)

    def test_v3_migration_preserves_data_and_constraints(self):
        path = self.base / "migration.sqlite"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE legacy(value TEXT)")
            db.execute("INSERT INTO legacy VALUES('preserved')")
            db.execute("PRAGMA user_version=3")
        store = IndexStore(path)
        store.initialize()
        with store.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT value FROM legacy").fetchone()[0], "preserved")
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue(
                {"index_generations", "index_pending_directories", "index_fragments", "index_path_errors"} <= names
            )

    def test_identity_containment_and_single_active_constraint(self):
        generation = self.engine().start()
        wrong = MetadataEngine(self.store, self.root, FilesystemIdentity("other", "fake-lineage", "fake-uuid"))
        with self.assertRaises(IndexingError):
            wrong.run(generation)
        with self.assertRaises(IndexingError):
            self.engine().start()
        for path in ("../escape", "/absolute", "a/../../escape"):
            with self.assertRaises(IndexingError):
                _contained(self.root, path)

    def test_symlink_and_special_metadata_are_not_followed(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret").write_text("unread")
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        os.mkfifo(self.root / "pipe")
        generation = self.engine().start()
        self.engine().run(generation)
        rows = IndexQuery(self.store).search(generation)["rows"]
        self.assertEqual({row["name"]: row["entry_type"] for row in rows}, {"link": "symlink", "pipe": "special"})

    def test_durable_resume_partial_completion_and_recovery(self):
        (self.root / "child").mkdir()
        (self.root / "child" / "file").write_text("x")
        generation = self.engine().start()
        self.assertEqual(self.engine().run(generation, max_directories=1)["is_partial"], 1)
        self.store.recover_startup(lambda identity: identity == self.identity)
        fresh = self.engine()
        fresh.resume(generation)
        result = fresh.run(generation)
        self.assertEqual((result["state"], result["is_partial"]), ("completed", 0))
        self.assertEqual(IndexQuery(self.store).search(generation, "file")["rows"][0]["name"], "file")

    def test_pause_control_is_cooperative(self):
        generation = self.engine().start()
        self.store.set_control(generation, "pause")
        self.assertEqual(self.engine().run(generation)["state"], "paused")
        self.engine().resume(generation)
        self.assertEqual(self.engine().run(generation)["state"], "completed")

    def test_atomic_registration_and_orphans(self):
        (self.root / "file").write_text("data")
        generation = self.engine().start()
        with (
            mock.patch("cadastre.indexing.os.replace", side_effect=OSError("crash")),
            self.assertRaises(OSError),
        ):
            self.engine().run(generation)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM index_fragments").fetchone()[0], 0)
        self.store.recover_startup(lambda identity: True)
        self.engine().resume(generation)
        self.engine().run(generation)
        orphan = self.store.fragment_root / generation / "orphan.parquet"
        orphan.write_bytes(b"invalid")
        self.assertEqual(len(self.store.registered_fragments(generation)), 1)

    def test_registered_only_parameter_safety_cap_summary_and_timeout(self):
        for number in range(205):
            (self.root / f"file-{number:03}.txt").write_text("x")
        generation = self.engine().start()
        self.engine().run(generation)
        query = IndexQuery(self.store)
        self.assertEqual(query.search(generation, "%' OR 1=1 --")["rows"], [])
        self.assertEqual(len(query.search(generation, limit=9999)["rows"]), 200)
        self.assertEqual(query.summary(generation)["counts"][0]["entries"], 205)
        with self.assertRaises(IndexingError):
            query.search(generation, timeout_seconds=0)


if __name__ == "__main__":
    unittest.main()
