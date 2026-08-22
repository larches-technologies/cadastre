import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from cadastre.connected import MOUNT_OPTIONS, UDISKSCTL, ActionError, ConnectedDevices
from cadastre.server import create_server


def part(device="/dev/sdz1", mountpoint=None, supported=True, ambiguous=False):
    return {
        "device": device,
        "mountpoint": mountpoint,
        "supported": supported,
        "identityAmbiguous": ambiguous,
        "filesystem": "ext4",
        "filesystemLabel": "ARCHIVE",
        "filesystemUuid": "abcd-1234",
        "sizeBytes": 1,
    }


def disk(partition=None, **overrides):
    value = {
        "stableId": "usb-1",
        "device": "/dev/sdz",
        "brand": "Example",
        "model": "Vault",
        "serial": "SERIAL-1",
        "isSystem": False,
        "removable": True,
        "hotplug": True,
        "transport": "usb",
        "partitions": [] if partition is None else [partition],
    }
    value.update(overrides)
    return value


def discovery(disks):
    return lambda: SimpleNamespace(disks=disks)


def result(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class LifecycleSecurityTests(unittest.TestCase):
    def test_mount_exact_argv_and_verified_findmnt_options(self):
        states = [[disk(part())], [disk(part(mountpoint="/media/usb"))]]
        calls = []

        def discover():
            return SimpleNamespace(disks=states.pop(0) if len(states) > 1 else states[0])

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[0].endswith("findmnt"):
                return result(
                    json.dumps(
                        {
                            "filesystems": [
                                {"source": "/dev/sdz1", "target": "/media/usb", "options": "ro,nodev,nosuid,noexec"}
                            ]
                        }
                    )
                )
            return result()

        connected = ConnectedDevices(discover, runner)
        mounted = connected.mount("/dev/sdz1")
        self.assertEqual(mounted["mountpoint"], "/media/usb")
        self.assertEqual(calls[0][0], [UDISKSCTL, "mount", "-b", "/dev/sdz1", "-o", MOUNT_OPTIONS])
        self.assertIn(
            (["/usr/bin/findmnt", "--json", "--source", "/dev/sdz1", "--output", "SOURCE,TARGET,OPTIONS"]),
            [c[0] for c in calls],
        )
        self.assertNotIn("shell", calls[0][1])

    def test_mount_rejects_stale_unsupported_ambiguous_and_system(self):
        cases = [
            ([disk(part(supported=False))], "/dev/sdz1"),
            ([disk(part(ambiguous=True))], "/dev/sdz1"),
            ([disk(part(), isSystem=True)], "/dev/sdz1"),
            ([disk(part())], "/dev/../dev/sdz1"),
        ]
        for disks, device in cases:
            runner_calls = []
            with self.subTest(device=device, disks=disks), self.assertRaises(ActionError):
                ConnectedDevices(discovery(disks), lambda *a, calls=runner_calls, **k: calls.append(a)).mount(device)
            self.assertEqual(runner_calls, [])

    def test_failed_ro_verification_rolls_back_exactly(self):
        states = [[disk(part())], [disk(part(mountpoint="/media/usb"))]]
        calls = []

        def discover():
            return SimpleNamespace(disks=states.pop(0) if len(states) > 1 else states[0])

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[0].endswith("findmnt"):
                return result(json.dumps({"filesystems": [{"options": "rw,nodev,nosuid,noexec"}]}))
            return result()

        connected = ConnectedDevices(discover, runner)
        with self.assertRaisesRegex(ActionError, "not verified read-only") as raised:
            connected.mount("/dev/sdz1")
        self.assertEqual(raised.exception.code, "READ_ONLY_VERIFICATION_FAILED")
        self.assertEqual(calls[-1], [UDISKSCTL, "unmount", "-b", "/dev/sdz1"])
        self.assertNotIn("/dev/sdz1", connected.owned_mounts)

    def test_external_rw_browse_denied_but_confirmed_unmount_allowed(self):
        p = part(mountpoint="/media/external")

        def runner(argv, **kwargs):
            if argv[0].endswith("findmnt"):
                return result(
                    json.dumps({"filesystems": [{"source": "/dev/sdz1", "target": "/media/external", "options": "rw"}]})
                )
            return result()

        connected = ConnectedDevices(discovery([disk(p)]), runner)
        with self.assertRaises(ActionError) as browse:
            connected.browse("/dev/sdz1")
        self.assertEqual(browse.exception.code, "BROWSE_REQUIRES_READ_ONLY")
        connected.unmount("/dev/sdz1")

    def test_unmount_and_eject_exact_argv_and_restrictions(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[0].endswith("findmnt"):
                return result(
                    json.dumps({"filesystems": [{"source": "/dev/sdz1", "target": "/media/usb", "options": "ro"}]})
                )
            return result()

        connected = ConnectedDevices(discovery([disk(part(mountpoint="/media/usb"))]), runner)
        connected.owned_mounts.add("/dev/sdz1")
        connected.unmount("/dev/sdz1")
        self.assertEqual(calls[-1], [UDISKSCTL, "unmount", "-b", "/dev/sdz1"])
        calls.clear()
        ejectable = ConnectedDevices(discovery([disk()]), lambda argv, **k: calls.append(argv) or result())
        ejectable.eject("usb-1")
        self.assertEqual(calls, [[UDISKSCTL, "power-off", "-b", "/dev/sdz"]])
        for value in [
            disk(isSystem=True),
            disk(removable=False, hotplug=False, transport="sata"),
            disk(part(mountpoint="/x")),
        ]:
            with self.subTest(value=value), self.assertRaises(ActionError):
                ConnectedDevices(
                    discovery([value]),
                    lambda argv, **k: (
                        result(json.dumps({"filesystems": [{"options": "ro"}]}))
                        if argv[0].endswith("findmnt")
                        else self.fail("lifecycle runner called")
                    ),
                ).eject("usb-1")

    def test_timeout_and_unauthorized_mapping(self):
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], 20)

        with self.assertRaises(ActionError) as raised:
            ConnectedDevices(discovery([disk(part())]), timeout).mount("/dev/sdz1")
        self.assertEqual((raised.exception.code, raised.exception.status), ("ACTION_TIMEOUT", 504))

        def denied(*args, **kwargs):
            return result(stderr="Not authorized to perform operation", code=1)

        with self.assertRaises(ActionError) as raised:
            ConnectedDevices(discovery([disk(part())]), denied).mount("/dev/sdz1")
        self.assertEqual((raised.exception.code, raised.exception.status), ("UDISKS_UNAUTHORIZED", 403))


class BrowserContainmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "nested").mkdir()
        (self.root / "nested" / "hello.txt").write_text("hello")
        p = part(mountpoint=str(self.root))

        def runner(argv, **kwargs):
            return result(
                json.dumps({"filesystems": [{"source": "/dev/sdz1", "target": str(self.root), "options": "ro,nodev"}]})
            )

        self.connected = ConnectedDevices(discovery([disk(p)]), runner)

    def tearDown(self):
        self.temp.cleanup()

    def test_nested_listing_and_preview(self):
        listing = self.connected.browse("/dev/sdz1", "nested")
        entry = listing["entries"][0]
        self.assertEqual(entry["name"], "hello.txt")
        self.assertRegex(entry["modifiedAt"], r"[+-]00:00$")
        self.assertNotIn("mtime", entry)
        self.assertEqual(listing["context"]["disk"]["stableId"], "usb-1")
        self.assertEqual(listing["context"]["partition"]["device"], "/dev/sdz1")
        self.assertEqual(listing["context"]["partition"]["mountState"], "ro")
        preview = self.connected.preview("/dev/sdz1", "nested/hello.txt")
        self.assertEqual(preview["text"], "hello")
        self.assertTrue(preview["complete"])
        self.assertFalse(preview["truncated"])
        self.assertEqual((preview["bytesShown"], preview["totalSizeBytes"]), (5, 5))

    def test_absolute_nul_and_parent_rejected(self):
        for value in ["/etc", "a\x00b", "../etc", "a/../../etc"]:
            with self.subTest(value=value), self.assertRaises(ActionError) as raised:
                self.connected.browse("/dev/sdz1", value)
            self.assertEqual(raised.exception.code, "INVALID_PATH")

    def test_intermediate_and_final_symlinks_refused(self):
        outside = self.root.parent / (self.root.name + "-outside")
        outside.mkdir()
        self.addCleanup(lambda: outside.rmdir())
        (outside / "secret").write_text("no")
        self.addCleanup(lambda: (outside / "secret").unlink())
        (self.root / "escape").symlink_to(outside, target_is_directory=True)
        (self.root / "link.txt").symlink_to(outside / "secret")
        for method, value in [
            (self.connected.browse, "escape"),
            (self.connected.preview, "escape/secret"),
            (self.connected.preview, "link.txt"),
        ]:
            with self.subTest(value=value), self.assertRaises(ActionError) as raised:
                method("/dev/sdz1", value)
            self.assertEqual(raised.exception.code, "PATH_NOT_SAFE")

    def test_special_preview_denied(self):
        os.mkfifo(self.root / "pipe")
        with self.assertRaises(ActionError) as raised:
            self.connected.preview("/dev/sdz1", "pipe")
        self.assertIn(raised.exception.code, {"PATH_NOT_SAFE", "PREVIEW_NOT_REGULAR"})

    def test_listing_cap_and_truncation(self):
        many = self.root / "many"
        many.mkdir()
        for number in range(501):
            (many / str(number)).touch()
        listing = self.connected.browse("/dev/sdz1", "many")
        self.assertEqual(len(listing["entries"]), 500)
        self.assertTrue(listing["truncated"])

    def test_preview_size_cap_is_head_only_and_invalid_utf8_refused(self):
        (self.root / "large").write_bytes(b"x" * 262145)
        preview = self.connected.preview("/dev/sdz1", "large")
        self.assertTrue(preview["truncated"])
        self.assertFalse(preview["complete"])
        self.assertEqual(preview["bytesShown"], 262144)
        self.assertEqual(preview["totalSizeBytes"], 262145)
        self.assertEqual(len(preview["text"]), 262144)
        (self.root / "binary").write_bytes(b"\xff")
        binary = self.connected.preview("/dev/sdz1", "binary")
        self.assertEqual(binary["previewType"], "hex")
        self.assertEqual(binary["mimeType"], "application/octet-stream")
        self.assertEqual(binary["bytesShown"], 1)
        self.assertTrue(binary["text"].startswith("00000000  ff"))
        (self.root / "known.png").write_bytes(bytes(range(256)) + b"extra")
        known = self.connected.preview("/dev/sdz1", "known.png")
        self.assertEqual(known["mimeType"], "image/png")
        self.assertEqual(known["mimeSource"], "filename extension")
        self.assertEqual(known["bytesShown"], 256)
        self.assertTrue(known["truncated"])
        self.assertEqual(len(known["text"].splitlines()), 16)


class FakeConnected:
    def __init__(self):
        self.calls = []

    def live(self):
        return [disk(part()), disk(part("/dev/sdy1"), stableId="system-1", device="/dev/sdy", isSystem=True)]

    def browse(self, device, path):
        return {"path": path, "entries": [], "truncated": False}

    def preview(self, device, path):
        return {"path": path, "text": "fake", "sizeBytes": 4}

    def mount(self, device):
        self.calls.append(("mount", device))
        return {"device": device}

    def unmount(self, device):
        self.calls.append(("unmount", device))

    def eject(self, stable_id):
        self.calls.append(("eject", stable_id))


class ApiSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fake = FakeConnected()
        self.server = create_server("127.0.0.1", 0, str(Path(self.temp.name) / "db.sqlite"), self.fake)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.token = json.load(urllib.request.urlopen(self.base + "/api/session"))["actionToken"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, method="POST", token=None, origin=None, payload=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Cadastre-Action-Token"] = token
        if origin is not None:
            headers["Origin"] = origin
        return urllib.request.urlopen(
            urllib.request.Request(
                self.base + path, data=json.dumps(payload or {}).encode(), headers=headers, method=method
            )
        )

    def error(self, *args, **kwargs):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(*args, **kwargs)
        return raised.exception.code, json.load(raised.exception)["error"]["code"]

    def test_session_bootstrap_and_read_only_gets(self):
        self.assertGreater(len(self.token), 30)
        self.assertEqual(json.load(urllib.request.urlopen(self.base + "/health"))["status"], "ready")
        self.assertEqual(len(json.load(urllib.request.urlopen(self.base + "/api/connected"))["disks"]), 1)
        self.assertIn(b"CONNECTED DEVICES", urllib.request.urlopen(self.base + "/").read())

    def test_connected_system_disks_require_explicit_opt_in(self):
        default = json.load(urllib.request.urlopen(self.base + "/api/connected"))
        opted_in = json.load(urllib.request.urlopen(self.base + "/api/connected?includeSystem=true"))
        self.assertEqual([item["stableId"] for item in default["disks"]], ["usb-1"])
        self.assertEqual([item["stableId"] for item in opted_in["disks"]], ["usb-1", "system-1"])

    def test_connected_membership_uses_exact_stable_id_and_refreshes(self):
        first = json.load(urllib.request.urlopen(self.base + "/api/connected"))["disks"][0]
        self.assertEqual(first["inventoryStatus"], "not-in-inventory")
        from cadastre.store import Store

        item = {
            "stableId": "usb-1",
            "device": "/dev/different",
            "brand": "",
            "model": "",
            "sizeBytes": 1,
            "serial": "different",
            "partitionTable": "unknown",
            "filesystems": [],
            "smartStatus": "unknown",
            "transport": "usb",
            "removable": True,
            "readOnly": True,
        }
        Store(str(Path(self.temp.name) / "db.sqlite")).add_disk(item)
        second = json.load(urllib.request.urlopen(self.base + "/api/connected"))["disks"][0]
        self.assertEqual(second["inventoryStatus"], "in-inventory")

    def test_connected_membership_reports_unknown_when_inventory_degrades(self):
        from unittest.mock import patch

        from cadastre.store import Store

        with patch.object(Store, "list_disks", side_effect=OSError("degraded")):
            item = json.load(urllib.request.urlopen(self.base + "/api/connected"))["disks"][0]
        self.assertEqual(item["inventoryStatus"], "unknown")

    def test_direct_routes_serve_app_without_404(self):
        root = urllib.request.urlopen(self.base + "/").read()
        for path in ["/connected", "/inventory"]:
            response = urllib.request.urlopen(self.base + path)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), root)

    def test_ui_contract_uses_accessible_distinct_dialogs_and_semantic_links(self):
        html = urllib.request.urlopen(self.base + "/").read().decode()
        script = urllib.request.urlopen(self.base + "/app.js").read().decode()
        presentation = urllib.request.urlopen(self.base + "/file-presentation.js").read().decode()
        self.assertIn('stroke="currentColor"', presentation)
        targets = urllib.request.urlopen(self.base + "/index-targets.js").read().decode()
        self.assertIn("CadastreIndexTargets", targets)
        self.assertIn('id="browser-dialog"', html)
        self.assertIn('aria-labelledby="browser-title"', html)
        self.assertIn('id="preview-dialog"', html)
        self.assertIn('aria-labelledby="preview-title"', html)
        self.assertIn('<a class="button secondary" href="#browse"', script)
        self.assertIn('<a href="#directory" data-dir=', script)
        self.assertIn('<a href="#preview" data-preview=', script)
        self.assertNotIn("Preview text</button>", script)
        self.assertIn("function modified(value)", script)
        self.assertIn("Hex preview", script)
        self.assertIn("Text preview", script)
        self.assertIn("d.mimeType", script)
        self.assertIn("statusEl.textContent", script)
        self.assertIn("content.textContent=d.text", script)
        self.assertIn("origin.focus()", script)
        self.assertIn("previewOrigin.focus()", script)

    def test_missing_invalid_token_and_invalid_origin(self):
        self.assertEqual(self.error("/api/connected/mount", payload={"confirmed": True}), (401, "ACTION_TOKEN_REQUIRED"))
        self.assertEqual(
            self.error("/api/connected/mount", token="forged", payload={"confirmed": True}),
            (401, "ACTION_TOKEN_REQUIRED"),
        )
        self.assertEqual(
            self.error(
                "/api/connected/mount", token=self.token, origin="https://evil.test", payload={"confirmed": True}
            ),
            (403, "ORIGIN_REJECTED"),
        )
        self.assertEqual(self.fake.calls, [])

    def test_inventory_add_and_removal_are_token_protected(self):
        self.assertEqual(
            self.error(
                "/api/partitions",
                payload={"diskStableId": "forged", "incarnationIds": ["forged"]},
            ),
            (401, "ACTION_TOKEN_REQUIRED"),
        )
        self.assertEqual(
            self.error("/api/disks/forged", method="DELETE"),
            (401, "ACTION_TOKEN_REQUIRED"),
        )

    def test_confirmation_and_fake_state_changing_smoke(self):
        self.assertEqual(
            self.error("/api/connected/mount", token=self.token, payload={"device": "/dev/sdz1"}),
            (409, "CONFIRMATION_REQUIRED"),
        )
        for action, payload in [
            ("mount", {"device": "/dev/sdz1"}),
            ("unmount", {"device": "/dev/sdz1"}),
            ("eject", {"stableId": "usb-1"}),
        ]:
            response = self.request(
                "/api/connected/" + action, token=self.token, origin=self.base, payload={**payload, "confirmed": True}
            )
            self.assertTrue(json.load(response)["ok"])
        self.assertEqual(self.fake.calls, [("mount", "/dev/sdz1"), ("unmount", "/dev/sdz1"), ("eject", "usb-1")])

    def test_stale_target_error_is_mapped(self):
        class Stale(FakeConnected):
            def mount(self, device):
                raise ActionError("TARGET_STALE", "stale")

        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.server = create_server("127.0.0.1", 0, str(Path(self.temp.name) / "db2.sqlite"), Stale())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.token = json.load(urllib.request.urlopen(self.base + "/api/session"))["actionToken"]
        self.assertEqual(
            self.error("/api/connected/mount", token=self.token, payload={"confirmed": True, "device": "/dev/forged"}),
            (409, "TARGET_STALE"),
        )


class ConnectedUiContractTests(unittest.TestCase):
    def test_connected_security_copy_and_controls(self):
        js = (Path(__file__).parent.parent / "static/app.js").read_text()
        html = (Path(__file__).parent.parent / "static/index.html").read_text()
        for text in [
            "Mount read-only",
            "No valid partition action",
            "Eject unavailable",
            "256 KiB",
            "Listing truncated at 500 entries",
            "confirm(copy[action])",
            "Browse files",
        ]:
            self.assertIn(text, js)
        for text in ["CONNECTED DEVICES", "external RW mounts cannot be browsed", "Refresh connected"]:
            self.assertIn(text, html)
        self.assertIn("!d.isSystem&&p.supported&&!p.identityAmbiguous&&!p.mountpoint", js)
        self.assertIn("browse=p.browseAllowed", js)
        for text in ["In inventory", "Not in inventory", "Inventory status unknown", "popstate", "pushState"]:
            self.assertIn(text, js)
        self.assertIn('aria-label="Primary"', html)
        self.assertIn('data-view="connected"', html)
        self.assertGreaterEqual(html.count('data-view="inventory"'), 3)
        self.assertIn("await inventory();await connected()", js)
        self.assertIn('for="show-connected-system"', html)
        self.assertIn('<input type="checkbox" id="show-connected-system"> Show system drives', html)
        self.assertNotIn('id="show-connected-system" checked', html)
        self.assertIn("req('/api/connected?includeSystem='+$('#show-connected-system').checked)", js)
        self.assertIn("$('#refresh-connected').onclick=connected", js)
        self.assertIn("$('#show-connected-system').onchange=connected", js)


class FilePresentationUiContractTests(unittest.TestCase):
    def test_file_presentation_classifier_and_dom_contract(self):
        root = Path(__file__).parent.parent
        subprocess.run(
            ["node", "tests/file-presentation.test.js"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        app = (root / "static/app.js").read_text()
        presentation = (root / "static/file-presentation.js").read_text()
        html = (root / "static/index.html").read_text()
        context = app[app.index("function contextMarkup") : app.index("function closeBrowser")]
        self.assertNotIn("Current relative path", context)
        self.assertIn("CadastreFilePresentation.directoryTitle(d.context)", app)
        self.assertIn("'+esc(directoryTitle)+'", app)
        self.assertIn('class="mono directory-title"', app)
        self.assertIn('aria-hidden="true"', presentation)
        self.assertIn('stroke="currentColor"', presentation)
        targets = (root / "static/index-targets.js").read_text()
        self.assertIn("CadastreIndexTargets", targets)
        self.assertIn('<script src="/file-presentation.js"></script><script src="/app.js"></script>', html)
        indexing = (root / "static/indexing.js").read_text()
        self.assertIn("JSON.stringify({incarnationId:id})", indexing)
        self.assertIn("if(result.eligible.some(x=>x.value===previous))", indexing)
        self.assertIn("No eligible mounted filesystems", indexing)
