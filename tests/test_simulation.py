import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from cadastre.connected import ActionError
from cadastre.server import create_server
from cadastre.simulation import SimulatedDevices, discover_simulated, fixtures


class SimulationProviderTests(unittest.TestCase):
    def test_fixture_coverage_and_provenance(self):
        disks = fixtures()
        self.assertEqual(len(disks), 5)
        self.assertTrue(all(d["provenance"]["simulated"] for d in disks))
        self.assertTrue(any(d["isSystem"] for d in disks))
        self.assertTrue(any(p["filesystem"] == "zfs" and not p["supported"] for d in disks for p in d["partitions"]))
        self.assertTrue(any(p["identityAmbiguous"] for d in disks for p in d["partitions"]))
        self.assertEqual(discover_simulated().disks, disks)

    @patch("subprocess.run", side_effect=AssertionError("subprocess forbidden"))
    def test_lifecycle_and_browser_never_use_subprocess(self, _run):
        devices = SimulatedDevices()
        mounted = devices.mount("sim://partition/usb-normal")
        self.assertEqual(mounted["mountState"], "ro")
        devices.unmount("sim://partition/usb-normal")
        root = devices.browse("sim://partition/browseable")
        self.assertEqual({x["name"] for x in root["entries"]}, {"README.txt", "Documents"})
        preview = devices.preview("sim://partition/browseable", "Documents/notes.txt")
        self.assertIn("simulated", preview["provenance"]["mode"])
        devices.eject("sim-disk-usb-normal")
        self.assertFalse(any(d["stableId"] == "sim-disk-usb-normal" for d in devices.live()))

    def test_critical_rejections(self):
        devices = SimulatedDevices()
        for target in ("sim://partition/system-protected", "sim://partition/unsupported", "sim://partition/ambiguous"):
            with self.assertRaises(ActionError):
                devices.mount(target)
        with self.assertRaises(ActionError):
            devices.eject("sim-disk-system-protected")
        with self.assertRaises(ActionError):
            devices.browse("sim://partition/usb-normal")
        with self.assertRaises(ActionError):
            devices.browse("sim://partition/browseable", "../escape")
        with self.assertRaises(ActionError):
            devices.preview("sim://partition/browseable", "missing.txt")


class SimulationServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "simulation.db")
        self.server = create_server("127.0.0.1", 0, self.db, simulated=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as response:
            return response, json.loads(response.read())

    def test_opt_in_provenance_and_ui_banner(self):
        response, health = self.get("/health")
        self.assertTrue(health["simulation"])
        self.assertEqual(response.headers["X-Cadastre-Provenance"], "simulated")
        _, connected = self.get("/api/connected?includeSystem=true")
        self.assertTrue(connected["provenance"]["simulated"])
        self.assertEqual(len(connected["disks"]), 5)
        with urllib.request.urlopen(self.base + "/") as response:
            page = response.read().decode()
        self.assertIn("SIMULATED DISK MODE", page)

    @patch("cadastre.server.ConnectedDevices", side_effect=AssertionError("real provider forbidden"))
    @patch("cadastre.server.discover_disks", side_effect=AssertionError("real discovery forbidden"))
    def test_simulation_wiring_does_not_construct_real_provider(self, _discover, _connected):
        server = create_server("127.0.0.1", 0, str(Path(self.temp.name) / "second.db"), simulated=True)
        server.server_close()

    def test_default_is_real_mode(self):
        with patch("cadastre.server.ConnectedDevices") as provider:
            provider.return_value.live.return_value = []
            server = create_server("127.0.0.1", 0, str(Path(self.temp.name) / "real.db"))
            provider.assert_called_once()
            server.server_close()

    def test_simulated_indexing_is_rejected(self):
        _, session = self.get("/api/session")
        request = urllib.request.Request(
            self.base + "/api/indexes/start",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", "X-Cadastre-Action-Token": session["actionToken"]},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(raised.exception.code, 409)


if __name__ == "__main__":
    unittest.main()
