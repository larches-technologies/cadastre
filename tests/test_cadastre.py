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
        self.assertTrue(disks[0]["partitions"][1]["possibleClone"])
        self.assertTrue(disks[1]["partitions"][0]["possibleClone"])
        self.assertFalse(disks[0]["partitions"][1]["identityAmbiguous"])

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


class CanonicalLineageAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "lineage.db")
        self.store.initialize()
        self.disk = {
            "stableId": "disk-1",
            "device": "/dev/sdb",
            "brand": "Acme",
            "model": "Archive",
            "sizeBytes": 100,
            "serial": "one",
            "partitionTable": "gpt",
            "filesystems": ["ext4"],
            "smartStatus": "unavailable",
            "smartDetail": None,
            "transport": "usb",
            "removable": True,
            "readOnly": False,
        }
        self.part = {
            "incarnationId": "disk-1:fsuuid:fs-1",
            "device": "/dev/sdb1",
            "name": "sdb1",
            "number": 1,
            "partuuid": "part-a",
            "startBytes": 2048,
            "sizeBytes": 80,
            "filesystem": "ext4",
            "filesystemUuid": "fs-1",
            "identityConfidence": "high",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_uuid_geometry_change_stays_current_one_lineage(self):
        self.store.add_partition(self.disk, self.part)
        changed = {
            **self.part,
            "device": "/dev/sdb3",
            "name": "sdb3",
            "number": 3,
            "partuuid": "part-b",
            "startBytes": 4096,
            "sizeBytes": 90,
        }
        current, created = self.store.add_partition(self.disk, changed)
        self.assertFalse(created)
        self.assertEqual(current["displayStatus"], "Current")
        self.assertEqual(current["lineageId"], self.part["incarnationId"])
        self.assertEqual(len(current["geometryObservations"]), 2)
        self.assertEqual(len(self.store.list_partitions()), 1)

    def test_changed_uuid_replaces_old(self):
        old, _ = self.store.add_partition(self.disk, self.part)
        new = {**self.part, "incarnationId": "disk-1:fsuuid:fs-2", "filesystemUuid": "fs-2"}
        current, _ = self.store.add_partition(self.disk, new)
        self.assertEqual(self.store.get_partition(old["incarnationId"])["displayStatus"], "Historical")
        self.assertEqual(current["displayStatus"], "Current")

    def test_additive_migration_from_v2(self):
        with self.store.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            columns = {r["name"] for r in db.execute("PRAGMA table_info(partition_incarnations)")}
        self.assertTrue({"lineage_id", "start_bytes"} <= columns)


class DuplicateUuidAcceptanceTests(unittest.TestCase):
    @patch("cadastre.discovery.shutil.which", side_effect=lambda n: "/usr/bin/" + n if n == "lsblk" else None)
    def test_cross_disk_uuid_is_distinct_possible_clone(self, _):
        data = {
            "blockdevices": [
                {
                    "name": "sdb",
                    "path": "/dev/sdb",
                    "type": "disk",
                    "serial": "a",
                    "children": [{"name": "sdb1", "type": "part", "uuid": "same", "fstype": "ext4"}],
                },
                {
                    "name": "sdc",
                    "path": "/dev/sdc",
                    "type": "disk",
                    "serial": "b",
                    "children": [{"name": "sdc1", "type": "part", "uuid": "same", "fstype": "ext4"}],
                },
            ]
        }
        disks = discover_disks(runner=lambda *a, **k: completed(json.dumps(data))).disks
        parts = [d["partitions"][0] for d in disks]
        self.assertNotEqual(parts[0]["incarnationId"], parts[1]["incarnationId"])
        self.assertTrue(all(p["possibleClone"] and not p["identityAmbiguous"] for p in parts))

    @patch("cadastre.discovery.shutil.which", side_effect=lambda n: "/usr/bin/" + n if n == "lsblk" else None)
    def test_duplicate_uuid_within_disk_fails_closed(self, _):
        data = {
            "blockdevices": [
                {
                    "name": "sdb",
                    "path": "/dev/sdb",
                    "type": "disk",
                    "serial": "a",
                    "children": [
                        {"name": "sdb1", "type": "part", "uuid": "same", "fstype": "ext4"},
                        {"name": "sdb2", "type": "part", "uuid": "same", "fstype": "ext4"},
                    ],
                }
            ]
        }
        parts = discover_disks(runner=lambda *a, **k: completed(json.dumps(data))).disks[0]["partitions"]
        self.assertTrue(all(p["identityAmbiguous"] and not p["supported"] for p in parts))


class StructuredEjectUiAcceptanceTests(unittest.TestCase):
    def test_req_preserves_structured_error_and_renderer_escapes(self):
        source = (Path(__file__).parent.parent / "static" / "app.js").read_text()
        self.assertIn("e.code=d.error?.code;e.data=d.error?.data", source)
        self.assertIn("Partial eject: disk was not powered off", source)
        self.assertIn("Succeeded unmounts:", source)
        self.assertIn("Remaining mounts:", source)
        for value in ("disk.brand", "disk.device", "b.device", "b.mountpoint", "b.pid", "b.process", "esc(x)"):
            self.assertIn(value, source)
        self.assertNotIn("cmdline", source)
        self.assertNotIn("environ", source)

    def test_structured_error_survives_req_and_renders_in_failure_surface(self):
        source = (Path(__file__).parent.parent / "static" / "app.js").read_text()
        req_source = source[source.index("async function req(") : source.index("async function health(")]
        render_source = source[source.index("function ejectFailure(") : source.index("async function lifecycle(")]
        payload = {
            "error": {
                "code": "EJECT_BLOCKED",
                "message": "blocked",
                "data": {
                    "disk": {"brand": "A<", "model": "B&", "device": "/dev/sdz"},
                    "blockers": [
                        {
                            "device": "/dev/sdz1",
                            "mountpoint": "/m<",
                            "pid": 42,
                            "process": "bad<script>",
                            "openPaths": ["/x&y"],
                        }
                    ],
                    "succeededUnmounts": [],
                    "remainingMounts": [{"device": "/dev/sdz1", "mountpoint": "/m<"}],
                },
            }
        }
        script = f"""
        let actionToken='token';
        const entities={{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}};
        const esc=s=>String(s??'').replace(/[&<>"']/g,c=>entities[c]);
        global.fetch=async()=>({{ok:false,status:409,json:async()=>({json.dumps(payload)})}});
        {req_source}
        {render_source}
        req('/eject').then(()=>process.exit(2)).catch(e=>{{
          const rendered=ejectFailure(e);
          if(e.code!=='EJECT_BLOCKED'||!e.data||!rendered.includes('/dev/sdz1')||!rendered.includes('PID 42')||
             !rendered.includes('bad&lt;script&gt;')||!rendered.includes('/x&amp;y')||
             rendered.includes('<script>')) process.exit(3);
        }});
        """
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


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
                headers={
                    "Content-Type": "application/json",
                    "X-Cadastre-Action-Token": json.load(
                        urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/session")
                    )["actionToken"],
                },
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
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)


class InventoryRemovalTests(unittest.TestCase):
    def test_remove_and_cancel_contract(self):
        with tempfile.TemporaryDirectory() as folder, patch("subprocess.run") as shell:
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
            part = {
                "device": "/dev/sdb1",
                "name": "sdb1",
                "number": 1,
                "partuuid": "slot",
                "filesystem": "ext4",
                "filesystemUuid": "fs1",
                "sizeBytes": 9,
                "identityConfidence": "high",
                "incarnationId": "disk-1:fsuuid:fs1",
            }
            store.add_partition(disk, part)
            removed = store.remove_disk("disk-1")
            self.assertEqual(removed["partitionRecordsRemoved"], 1)
            self.assertEqual(store.list_disks(), [])
            self.assertEqual(store.list_partitions(), [])
            shell.assert_not_called()
            with self.assertRaises(KeyError):
                store.remove_disk("absent")
        source = (Path(__file__).parent.parent / "static" / "app.js").read_text()
        self.assertIn("function removalConfirmed", source)
        self.assertIn("if(!removalConfirmed(event.target.returnValue)||!pendingRemoval)", source)
        html = (Path(__file__).parent.parent / "static" / "index.html").read_text()
        self.assertIn("Remove from inventory", html)
        self.assertIn("not undoable in MVP0", html)


class InventoryRemovalApiTests(unittest.TestCase):
    def test_absent_disk_returns_404(self):
        with tempfile.TemporaryDirectory() as folder:
            server = create_server("127.0.0.1", 0, str(Path(folder) / "test.db"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            token = json.load(urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/session"))[
                "actionToken"
            ]
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/disks/absent",
                headers={"X-Cadastre-Action-Token": token},
                method="DELETE",
            )
            try:
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request)
                self.assertEqual(raised.exception.code, 404)
                self.assertEqual(json.load(raised.exception)["error"]["code"], "INVENTORY_DISK_NOT_FOUND")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
