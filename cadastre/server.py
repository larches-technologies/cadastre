"""Dependency-free local HTTP API and operator UI."""

from __future__ import annotations

import json
import logging
import os
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cadastre import __version__
from cadastre.connected import ActionError, ConnectedDevices
from cadastre.discovery import DiscoveryError, discover_disks, filter_system_disks
from cadastre.store import Store

LOG = logging.getLogger("cadastre")
STATIC = Path(__file__).parent.parent / "static"


def create_server(
    host: str = "127.0.0.1",
    port: int = 8741,
    db_path: str = "data/cadastre.db",
    connected_devices: ConnectedDevices | None = None,
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Cadastre MVP0 only permits a local-only bind")
    store = Store(db_path)
    store.initialize()
    connected = connected_devices or ConnectedDevices()
    action_token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = f"Cadastre/{__version__}"

        def log_message(self, fmt: str, *args: object) -> None:
            LOG.info(fmt, *args)

        def json_response(self, status: int, data: object) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def error_response(self, status: int, code: str, message: str, detail: str | None = None, data=None) -> None:
            self.json_response(status, {"error": {"code": code, "message": message, "detail": detail, "data": data}})

        def authorized_action(self):
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{self.headers.get('Host')}":
                self.error_response(403, "ORIGIN_REJECTED", "State-changing requests must be same-origin")
                return False
            if not secrets.compare_digest(self.headers.get("X-Cadastre-Action-Token", ""), action_token):
                self.error_response(401, "ACTION_TOKEN_REQUIRED", "Action authorization is missing or stale")
                return False
            return True

        def request_json(self):
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path in {"/health", "/ready"}:
                self.json_response(
                    HTTPStatus.OK,
                    {"status": "ready", "version": __version__, "scope": "local-read-only"},
                )
            elif parsed.path == "/api/session":
                self.json_response(200, {"actionToken": action_token})
            elif parsed.path == "/api/connected":
                try:
                    disks = connected.live()
                except (DiscoveryError, ActionError) as exc:
                    self.error_response(getattr(exc, "status", 503), getattr(exc, "code", "DISCOVERY_FAILED"), str(exc))
                    return
                try:
                    inventory_ids = {disk["stableId"] for disk in store.list_disks()}
                except Exception:  # inventory degradation must not hide live host state
                    LOG.exception("Persisted inventory lookup failed during connected refresh")
                    for disk in disks:
                        disk["inventoryStatus"] = "unknown"
                else:
                    for disk in disks:
                        # Membership is authoritative only by the exact physical-disk stable ID.
                        disk["inventoryStatus"] = (
                            "in-inventory" if disk.get("stableId") in inventory_ids else "not-in-inventory"
                        )
                self.json_response(200, {"disks": disks, "manualRefresh": True})
            elif parsed.path in {"/api/browse", "/api/preview"}:
                try:
                    query = parse_qs(parsed.query)
                    args = (query.get("device", [""])[0], query.get("path", [""])[0])
                    self.json_response(
                        200, connected.browse(*args) if parsed.path.endswith("browse") else connected.preview(*args)
                    )
                except ActionError as exc:
                    self.error_response(exc.status, exc.code, str(exc))
            elif parsed.path == "/api/disks":
                self.json_response(
                    HTTPStatus.OK,
                    {"disks": store.list_disks(), "version": __version__},
                )
            elif parsed.path == "/api/discovery":
                try:
                    result = discover_disks()
                    include_system = parse_qs(parsed.query).get("includeSystem", ["false"])[0].lower() == "true"
                    disks = filter_system_disks(result.disks, include_system)
                    self.json_response(
                        HTTPStatus.OK,
                        {
                            "disks": disks,
                            "warnings": result.warnings,
                            "systemDisksHidden": not include_system,
                        },
                    )
                except DiscoveryError as exc:
                    self.error_response(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "DISCOVERY_FAILED",
                        "Disk discovery is unavailable",
                        str(exc),
                    )
            elif parsed.path in {"/", "/index.html", "/connected", "/inventory"}:
                self.serve_file(STATIC / "index.html", "text/html; charset=utf-8")
            elif parsed.path == "/app.css":
                self.serve_file(STATIC / "app.css", "text/css; charset=utf-8")
            elif parsed.path == "/app.js":
                self.serve_file(STATIC / "app.js", "text/javascript; charset=utf-8")
            else:
                self.error_response(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Route not found")

        def serve_file(self, path: Path, content_type: str) -> None:
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def do_DELETE(self) -> None:  # noqa: N802
            if not self.authorized_action():
                return
            parsed = urlparse(self.path)
            prefix = "/api/disks/"
            if not parsed.path.startswith(prefix) or not parsed.path[len(prefix) :]:
                self.error_response(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Route not found")
                return
            from urllib.parse import unquote

            stable_id = unquote(parsed.path[len(prefix) :])
            try:
                removed = store.remove_disk(stable_id)
                self.json_response(HTTPStatus.OK, {"removed": removed})
            except KeyError:
                self.error_response(HTTPStatus.NOT_FOUND, "INVENTORY_DISK_NOT_FOUND", "Inventory disk not found")

        def do_POST(self) -> None:  # noqa: N802
            if not self.authorized_action():
                return
            action_path = urlparse(self.path).path
            if action_path in {"/api/connected/mount", "/api/connected/unmount", "/api/connected/eject"}:
                try:
                    payload = self.request_json()
                    if payload.get("confirmed") is not True:
                        self.error_response(409, "CONFIRMATION_REQUIRED", "Explicit confirmation is required")
                        return
                    result = (
                        connected.mount(payload.get("device", ""))
                        if action_path.endswith("mount") and not action_path.endswith("unmount")
                        else connected.unmount(payload.get("device", ""))
                        if action_path.endswith("unmount")
                        else connected.eject(payload.get("stableId", ""))
                    )
                    self.json_response(200, {"ok": True, "result": result})
                except ActionError as exc:
                    self.error_response(exc.status, exc.code, str(exc), data=exc.data)
                return
            if action_path != "/api/partitions":
                self.error_response(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Route not found")
                return
            try:
                payload = self.request_json()
                disk_id = payload.get("diskStableId")
                incarnation_ids = payload.get("incarnationIds")
                if not disk_id or not isinstance(incarnation_ids, list) or not incarnation_ids:
                    self.error_response(
                        HTTPStatus.BAD_REQUEST,
                        "SELECTION_REQUIRED",
                        "Select at least one eligible discovered partition",
                    )
                    return
                result = discover_disks()
                disk = next((item for item in result.disks if item["stableId"] == disk_id), None)
                if disk is None:
                    self.error_response(HTTPStatus.CONFLICT, "DEVICE_NOT_PRESENT", "Disk is no longer present; rescan")
                    return
                if disk["isSystem"] and payload.get("confirmSystemDisk") is not True:
                    self.error_response(
                        HTTPStatus.CONFLICT,
                        "SYSTEM_DISK_CONFIRMATION_REQUIRED",
                        "System disks require explicit confirmation",
                    )
                    return
                fresh = {partition["incarnationId"]: partition for partition in disk["partitions"]}
                if len(set(incarnation_ids)) != len(incarnation_ids) or any(
                    item not in fresh for item in incarnation_ids
                ):
                    self.error_response(
                        HTTPStatus.CONFLICT,
                        "PARTITION_SELECTION_STALE",
                        "Partition selection is stale or does not match fresh discovery",
                    )
                    return
                selected = [fresh[item] for item in incarnation_ids]
                if any(not partition["supported"] or partition["identityAmbiguous"] for partition in selected):
                    self.error_response(
                        HTTPStatus.CONFLICT,
                        "FILESYSTEM_NOT_SUPPORTED",
                        "One or more selected partitions are unsupported or identity-ambiguous",
                    )
                    return
                if (
                    any(partition["requiresIdentityConfirmation"] for partition in selected)
                    and payload.get("confirmDegradedIdentity") is not True
                ):
                    self.error_response(
                        HTTPStatus.CONFLICT,
                        "DEGRADED_IDENTITY_CONFIRMATION_REQUIRED",
                        "Partitions without a filesystem UUID require explicit identity confirmation",
                    )
                    return
                persisted = [store.add_partition(disk, partition) for partition in selected]
                self.json_response(
                    HTTPStatus.CREATED if any(created for _, created in persisted) else HTTPStatus.OK,
                    {
                        "partitions": [partition for partition, _ in persisted],
                        "created": [created for _, created in persisted],
                    },
                )
            except DiscoveryError as exc:
                self.error_response(
                    HTTPStatus.SERVICE_UNAVAILABLE, "DISCOVERY_FAILED", "Disk discovery is unavailable", str(exc)
                )
            except (json.JSONDecodeError, ValueError) as exc:
                self.error_response(
                    HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "Request body must be valid JSON", str(exc)
                )

    return ThreadingHTTPServer((host, port), Handler)


def run(host: str = "127.0.0.1", port: int = 8741, db_path: str | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    path = db_path or os.environ.get("CADASTRE_DB", "data/cadastre.db")
    server = create_server(host, port, path)
    LOG.info("Cadastre %s listening on http://%s:%s (database=%s)", __version__, host, port, path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
