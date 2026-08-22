import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from cadastre.server import create_server


class FakeConnected:
    def live(self):
        return []


class Phase2BUIContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server(
            port=0, db_path=str(Path(self.temp.name) / "fake.db"), connected_devices=FakeConnected()
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as response:
            return response.status, response.read().decode()

    def test_indexing_route_and_accessible_contract(self):
        status, body = self.get("/indexing")
        self.assertEqual(status, 200)
        for text in ["Build a searchable file map", "Start generation", "Bounded metadata search", 'aria-live="polite"']:
            self.assertIn(text, body)

    def test_ui_has_truthful_states_controls_polling_and_no_percentage(self):
        _, body = self.get("/indexing.js")
        for text in [
            "completed_with_errors",
            "waiting_for_remount",
            "interrupted",
            "Pause",
            "Resume",
            "Stop",
            "AbortController",
            "document.hidden",
            "X-Cadastre-Action-Token",
        ]:
            self.assertIn(text, body)
        self.assertNotIn("percent", body.lower())
        self.assertIn("incarnationId:id", body)
        for forbidden in ["mountpoint:", "device:", "filesystemUuid:", "lineageId:"]:
            self.assertNotIn(forbidden, body)

    def test_query_limit_is_capped_and_error_is_structured(self):
        _, body = self.get("/indexing.js")
        self.assertIn("Math.min(200", body)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.get("/api/indexes/missing/search?q=x&limit=999")
        payload = json.loads(raised.exception.read())
        self.assertEqual(payload["error"]["code"], "INDEX_NOT_FOUND")

    def test_existing_routes_remain(self):
        for route in ["/connected", "/inventory", "/app.js"]:
            self.assertEqual(self.get(route)[0], 200)


if __name__ == "__main__":
    unittest.main()
