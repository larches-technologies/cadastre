import json
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cadastre.discovery import DiscoveryError, discover_disks, filter_system_disks, normalize_filesystem
from cadastre.server import create_server
from cadastre.store import Store


def completed(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class DiscoveryTests(unittest.TestCase):
    @patch("cadastre.discovery.shutil.which", return_value=None)
    def test_critical_failure_when_lsblk_missing(self, _):
        with self.assertRaisesRegex(DiscoveryError, "lsblk is required"):
            discover_disks()

    @patch(
        "cadastre.discovery.shutil.which",
        side_effect=lambda n: "/usr/bin/" + n if n == "lsblk" else None,
    )
    def test_system_disk_identified_from_child_mount(self, _):
        data = {
            "blockdevices": [
                {
                    "name": "sda",
                    "path": "/dev/sda",
                    "type": "disk",
                    "size": 1000,
                    "model": "Boot",
                    "children": [{"type": "part", "mountpoints": ["/"], "fstype": "ext4"}],
                }
            ]
        }
        result = discover_disks(runner=lambda *a, **k: completed(json.dumps(data)))
        self.assertTrue(result.disks[0]["isSystem"])
        self.assertEqual(result.disks[0]["systemReasons"], ["/"])
        self.assertEqual(result.disks[0]["filesystems"], ["ext4"])

    @patch(
        "cadastre.discovery.shutil.which",
        side_effect=lambda n: "/usr/bin/" + n if n == "lsblk" else None,
    )
    def test_lsblk_failure_is_structured(self, _):
        with self.assertRaisesRegex(DiscoveryError, "permission denied"):
            discover_disks(runner=lambda *a, **k: completed(stderr="permission denied", code=1))

    def test_system_disk_filtering_defaults_to_hidden(self):
        disks = [
            {"stableId": "system", "isSystem": True},
            {"stableId": "offline", "isSystem": False},
        ]
        self.assertEqual([disk["stableId"] for disk in filter_system_disks(disks)], ["offline"])
        self.assertEqual(len(filter_system_disks(disks, include_system=True)), 2)


class StoreTests(unittest.TestCase):
    def test_add_disk_is_idempotent_and_history_arrays_exist(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "test.db")
            store.initialize()
            disk = {
                "stableId": "serial-1",
                "device": "/dev/sdz",
                "brand": "Acme",
                "model": "Archive",
                "sizeBytes": 1000,
                "serial": "serial-1",
                "partitionTable": "gpt",
                "filesystems": ["ntfs"],
                "smartStatus": "passed",
                "smartDetail": None,
                "transport": "usb",
                "removable": True,
                "readOnly": False,
            }
            first, created = store.add_disk(disk)
            second, created_again = store.add_disk({**disk, "device": "/dev/sdy"})
            self.assertTrue(created)
            self.assertFalse(created_again)
            self.assertEqual(len(store.list_disks()), 1)
            self.assertEqual(second["device"], "/dev/sdy")
            self.assertEqual(first["indexAttempts"], [])
            self.assertEqual(first["indexDates"], [])


class AdditionalDiscoveryTests(unittest.TestCase):
    @patch("cadastre.discovery.shutil.which", side_effect=lambda n: "/usr/bin/" + n if n == "lsblk" else None)
    def test_partition_metadata_is_structured_and_truthful(self, _):
        data = {
            "blockdevices": [
                {
                    "name": "sdb",
                    "path": "/dev/sdb",
                    "type": "disk",
                    "size": 3000,
                    "children": [
                        {
                            "name": "sdb1",
                            "path": "/dev/sdb1",
                            "type": "part",
                            "size": 1000,
                            "fstype": "ntfs",
                            "mountpoints": [None],
                        },
                        {
                            "name": "sdb2",
                            "path": "/dev/sdb2",
                            "type": "part",
                            "size": 2000,
                            "fstype": None,
                            "mountpoints": ["/media/archive"],
                        },
                    ],
                }
            ]
        }
        partitions = discover_disks(runner=lambda *a, **k: completed(json.dumps(data))).disks[0]["partitions"]
        self.assertEqual([p["device"] for p in partitions], ["/dev/sdb1", "/dev/sdb2"])
        self.assertEqual([p["sizeBytes"] for p in partitions], [1000, 2000])
        self.assertEqual([p["filesystem"] for p in partitions], ["ntfs", None])
        self.assertEqual(partitions[1]["mountpoint"], "/media/archive")


class UiRenderingTests(unittest.TestCase):
    def test_already_added_is_a_non_action_and_partition_states_are_present(self):
        source = (Path(__file__).parent.parent / "static" / "app.js").read_text()
        self.assertIn("added.has(p.incarnationId)", source)
        self.assertIn("Already added", source)
        self.assertIn("No partitions detected by lsblk.", source)
        self.assertIn("p.device||p.name", source)
        self.assertIn("eligible?", source)
        self.assertIn("disabled", source)


class PartitionIdentityTests(unittest.TestCase):
    def test_filesystem_normalization_and_allowlist_rendering(self):
        self.assertEqual(normalize_filesystem("FAT32"), "vfat")
        self.assertEqual(normalize_filesystem("EXT4"), "ext4")
        self.assertEqual(normalize_filesystem("apfs"), "apfs")
        source = (Path(__file__).parent.parent / "static" / "app.js").read_text()
        self.assertIn("Filesystem not supported", source)
        self.assertIn("data-partition", source)
        self.assertIn("disabled", source)

    @patch("cadastre.discovery.shutil.which", side_effect=lambda n: "/usr/bin/" + n if n == "lsblk" else None)
    def test_missing_uuid_fallback_and_duplicate_uuid_ambiguity(self, _):
        data = {
            "blockdevices": [
                {
                    "name": "sdb",
                    "path": "/dev/sdb",
                    "type": "disk",
                    "serial": "disk-b",
                    "children": [
                        {
                            "name": "sdb1",
                            "path": "/dev/sdb1",
                            "type": "part",
                            "partn": 1,
                            "partuuid": "part-1",
                            "fstype": "ext4",
                        },
                        {
                            "name": "sdb2",
                            "path": "/dev/sdb2",
                            "type": "part",
                            "partn": 2,
                            "uuid": "DUP",
                            "fstype": "ntfs",
                        },
                    ],
                },
                {
                    "name": "sdc",
                    "path": "/dev/sdc",
                    "type": "disk",
                    "serial": "disk-c",
                    "children": [
                        {
                            "name": "sdc1",
                            "path": "/dev/sdc1",
                            "type": "part",
                            "partn": 1,
                            "uuid": "dup",
                            "fstype": "ntfs",
                        },
                    ],
                },
            ]
        }
        disks = discover_disks(runner=lambda *a, **k: completed(json.dumps(data))).disks
        fallback = disks[0]["partitions"][0]
        self.assertEqual(fallback["incarnationId"], "disk-b:partuuid:part-1")
        self.assertTrue(fallback["requiresIdentityConfirmation"])
        self.assertTrue(disks[0]["partitions"][1]["identityAmbiguous"])
        self.assertFalse(disks[1]["partitions"][0]["supported"])

    def test_partition_idempotency_and_reformat_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "test.db")
            store.initialize()
            disk = {
                "stableId": "disk-1",
                "device": "/dev/sdb",
                "brand": "Acme",
                "model": "Archive",
                "sizeBytes": 9,
                "serial": "disk-1",
                "partitionTable": "gpt",
                "filesystems": ["ext4"],
                "smartStatus": "unavailable",
                "smartDetail": None,
                "transport": "usb",
                "removable": True,
                "readOnly": False,
            }
            base = {
                "device": "/dev/sdb1",
                "name": "sdb1",
                "number": 1,
                "partuuid": "slot-1",
                "filesystem": "ext4",
                "sizeBytes": 9,
                "identityConfidence": "high",
            }
            first, created = store.add_partition(
                disk, {**base, "filesystemUuid": "fs-old", "incarnationId": "disk-1:fsuuid:fs-old"}
            )
            _, created_again = store.add_partition(
                disk, {**base, "filesystemUuid": "fs-old", "incarnationId": "disk-1:fsuuid:fs-old"}
            )
            second, reformatted = store.add_partition(
                disk, {**base, "filesystemUuid": "fs-new", "incarnationId": "disk-1:fsuuid:fs-new"}
            )
            self.assertTrue(created)
            self.assertFalse(created_again)
            self.assertTrue(reformatted)
            self.assertEqual(store.get_partition(first["incarnationId"])["status"], "replaced")
            self.assertEqual(second["replaces"], first["incarnationId"])
            self.assertEqual(len(store.list_partitions()), 2)


class PartitionApiTests(unittest.TestCase):
    def _disk(self, **partition_changes):
        partition = {
            "name": "sdb1",
            "device": "/dev/sdb1",
            "number": 1,
            "partuuid": "part-1",
            "sizeBytes": 10,
            "filesystem": "ext4",
            "filesystemUuid": "fs-1",
            "mountpoint": None,
            "incarnationId": "disk-1:fsuuid:fs-1",
            "identityConfidence": "high",
            "requiresIdentityConfirmation": False,
            "identityAmbiguous": False,
            "supported": True,
        }
        partition.update(partition_changes)
        return {
            "stableId": "disk-1",
            "device": "/dev/sdb",
            "brand": "Acme",
            "model": "Archive",
            "sizeBytes": 10,
            "serial": "disk-1",
            "partitionTable": "gpt",
            "partitions": [partition],
            "filesystems": [partition["filesystem"]] if partition["filesystem"] else [],
            "smartStatus": "unavailable",
            "smartDetail": None,
            "transport": "usb",
            "removable": True,
            "readOnly": False,
            "isSystem": False,
            "systemReasons": [],
        }

    def _post(self, disk, payload):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch("cadastre.server.discover_disks", return_value=SimpleNamespace(disks=[disk], warnings=[])),
        ):
            server = create_server("127.0.0.1", 0, str(Path(folder) / "test.db"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/partitions",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                return exc.code, json.load(exc)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_backend_rejects_forged_or_stale_partition_selection(self):
        status, body = self._post(self._disk(), {"diskStableId": "disk-1", "incarnationIds": ["forged"]})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "PARTITION_SELECTION_STALE")

    def test_backend_rejects_unsupported_partition(self):
        disk = self._disk(filesystem="apfs", supported=False)
        status, body = self._post(
            disk, {"diskStableId": "disk-1", "incarnationIds": [disk["partitions"][0]["incarnationId"]]}
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "FILESYSTEM_NOT_SUPPORTED")

    def test_backend_requires_degraded_identity_confirmation(self):
        disk = self._disk(
            filesystemUuid=None,
            incarnationId="disk-1:partuuid:part-1",
            identityConfidence="degraded",
            requiresIdentityConfirmation=True,
        )
        status, body = self._post(
            disk, {"diskStableId": "disk-1", "incarnationIds": [disk["partitions"][0]["incarnationId"]]}
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "DEGRADED_IDENTITY_CONFIRMATION_REQUIRED")

    def test_existing_mvp0_database_migrates_additively(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.db"

            with sqlite3.connect(path) as db:
                db.executescript("""
                    CREATE TABLE disks (stable_id TEXT PRIMARY KEY, device TEXT NOT NULL, brand TEXT NOT NULL,
                    model TEXT NOT NULL, size_bytes INTEGER NOT NULL, serial TEXT NOT NULL,
                    partition_table TEXT NOT NULL,
                    filesystems_json TEXT NOT NULL, smart_status TEXT NOT NULL, smart_detail TEXT,
                    transport TEXT NOT NULL,
                    removable INTEGER NOT NULL, read_only INTEGER NOT NULL, first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL);
                    CREATE TABLE index_attempts (id INTEGER PRIMARY KEY, stable_id TEXT NOT NULL,
                    attempted_at TEXT NOT NULL,
                    status TEXT NOT NULL, detail TEXT);
                    INSERT INTO disks VALUES (
                    'legacy','/dev/sdz','Old','Disk',1,'serial','gpt','[]','unavailable',NULL,
                    'usb',1,0,'then','then');
                """)
            store = Store(path)
            store.initialize()
            self.assertEqual(store.list_disks()[0]["stableId"], "legacy")
            self.assertEqual(store.list_partitions(), [])
            with store.connect() as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
