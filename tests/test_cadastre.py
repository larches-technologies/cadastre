import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cadastre.discovery import DiscoveryError, discover_disks, filter_system_disks
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


if __name__ == "__main__":
    unittest.main()
