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
from cadastre.indexing import FilesystemIdentity, IndexingError, IndexQuery, IndexStore
from cadastre.simulation import PROVENANCE, SimulatedDevices, discover_simulated
from cadastre.store import Store
from cadastre.supervisor import IndexSupervisor

LOG = logging.getLogger("cadastre")
STATIC = Path(__file__).parent.parent / "static"


def create_server(
    host: str = "127.0.0.1",
    port: int = 8741,
    db_path: str = "data/cadastre.db",
    connected_devices: ConnectedDevices | None = None,
    index_supervisor: IndexSupervisor | None = None,
    simulated: bool = False,
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Cadastre MVP0 only permits a local-only bind")
    store = Store(db_path)
    store.initialize()
    if simulated and connected_devices is not None:
        raise ValueError("Simulation mode cannot mix injected or real connected devices")
    connected = SimulatedDevices() if simulated else (connected_devices or ConnectedDevices())
    discoverer = discover_simulated if simulated else discover_disks
    index_store = IndexStore(db_path)
    index_store.initialize()
    supervisor = index_supervisor or IndexSupervisor(index_store, connected)
    action_token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = f"Cadastre/{__version__}"

        def log_message(self, fmt: str, *args: object) -> None:
            LOG.info(fmt, *args)

        def json_response(self, status: int, data: object) -> None:
            if simulated and isinstance(data, dict):
                data = {**data, "provenance": PROVENANCE.copy()}
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Cadastre-Provenance", "simulated" if simulated else "real")
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
                    {"status": "ready", "version": __version__, "scope": "local-read-only", "simulation": simulated},
                )
            elif parsed.path == "/api/session":
                self.json_response(200, {"actionToken": action_token, "simulation": simulated})
            elif parsed.path == "/api/connected":
                try:
                    include_system = parse_qs(parsed.query).get("includeSystem", ["false"])[0].lower() == "true"
                    disks = filter_system_disks(connected.live(), include_system)
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
            elif parsed.path == "/api/indexes":
                limit = parse_qs(parsed.query).get("limit", ["50"])[0]
                self.json_response(200, {"generations": index_store.generations(int(limit))})
            elif parsed.path.startswith("/api/indexes/"):
                parts = parsed.path.split("/")
                try:
                    generation_id = parts[3]
                    if len(parts) == 4:
                        data = index_store.generation(generation_id)
                    elif len(parts) == 5 and parts[4] == "search":
                        query = parse_qs(parsed.query)
                        data = IndexQuery(index_store).search(
                            generation_id, query.get("q", [""])[0], int(query.get("limit", ["50"])[0])
                        )
                    elif len(parts) == 5 and parts[4] == "summary":
                        data = IndexQuery(index_store).summary(generation_id)
                    else:
                        raise IndexingError("INDEX_ROUTE_NOT_FOUND", "Index route not found")
                    self.json_response(200, data)
                except (IndexingError, ValueError) as exc:
                    self.index_error(exc)
            elif parsed.path == "/api/discovery":
                try:
                    result = discoverer()
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
            elif parsed.path in {"/", "/index.html", "/connected", "/inventory", "/indexing"}:
                self.serve_file(STATIC / "index.html", "text/html; charset=utf-8")
            elif parsed.path == "/app.css":
                self.serve_file(STATIC / "app.css", "text/css; charset=utf-8")
            elif parsed.path in {"/app.js", "/indexing.js", "/file-presentation.js"}:
                self.serve_file(STATIC / parsed.path.lstrip("/"), "text/javascript; charset=utf-8")
            else:
                self.error_response(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Route not found")

        def serve_file(self, path: Path, content_type: str) -> None:
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Cadastre-Provenance", "simulated" if simulated else "real")
            self.end_headers()
            self.wfile.write(body)

        def index_error(self, error: Exception) -> None:
            code = getattr(error, "code", "INVALID_REQUEST")
            status = 404 if code in {"INDEX_NOT_FOUND", "INDEX_ROUTE_NOT_FOUND"} else 409
            self.error_response(status, code, str(error))

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
                active = index_store.active_for(stable_id)
                if active:
                    self.error_response(
                        409, "INDEX_ACTIVE", "Indexing is active or not confirmed quiesced", data={"generations": active}
                    )
                    return
                removed = store.remove_disk(stable_id)
                self.json_response(HTTPStatus.OK, {"removed": removed})
            except KeyError:
                self.error_response(HTTPStatus.NOT_FOUND, "INVENTORY_DISK_NOT_FOUND", "Inventory disk not found")

        def do_POST(self) -> None:  # noqa: N802
            if not self.authorized_action():
                return
            action_path = urlparse(self.path).path
            if action_path.startswith("/api/indexes"):
                if simulated:
                    self.error_response(
                        409,
                        "SIMULATION_INDEX_UNAVAILABLE",
                        "Simulated indexing is disabled to prevent host filesystem access",
                    )
                    return
                try:
                    payload = self.request_json()
                    if action_path == "/api/indexes/start":
                        partition = store.get_partition(payload.get("incarnationId", ""))
                        identity = FilesystemIdentity(
                            partition["diskStableId"], partition["lineageId"], partition["filesystemUuid"]
                        )
                        self.json_response(202, supervisor.start(identity))
                        return
                    parts = action_path.split("/")
                    generation_id, action = parts[3], parts[4]
                    result = (
                        supervisor.resume(generation_id)
                        if action == "resume"
                        else supervisor.control(generation_id, action)
                    )
                    self.json_response(202, result)
                except (IndexingError, KeyError, ValueError, IndexError) as exc:
                    self.index_error(exc)
                return
            if action_path in {"/api/connected/mount", "/api/connected/unmount", "/api/connected/eject"}:
                try:
                    payload = self.request_json()
                    if payload.get("confirmed") is not True:
                        self.error_response(409, "CONFIRMATION_REQUIRED", "Explicit confirmation is required")
                        return
                    if action_path.endswith("unmount"):
                        active = []
                        for disk in connected.live():
                            for part in disk.get("partitions", []):
                                if part.get("device") == payload.get("device", ""):
                                    active = index_store.active_for(
                                        disk["stableId"], part.get("lineageId") or part.get("incarnationId")
                                    )
                    elif action_path.endswith("eject"):
                        active = index_store.active_for(payload.get("stableId", ""))
                    else:
                        active = []
                    if active:
                        self.error_response(
                            409,
                            "INDEX_ACTIVE",
                            "Indexing is active or not confirmed quiesced",
                            data={"generations": active},
                        )
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
                result = discoverer()
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


def run(host: str = "127.0.0.1", port: int = 8741, db_path: str | None = None, *, simulated: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    path = (
        (db_path or "data/cadastre-simulation.db")
        if simulated
        else (db_path or os.environ.get("CADASTRE_DB", "data/cadastre.db"))
    )
    server = create_server(host, port, path, simulated=simulated)
    LOG.info(
        "Cadastre %s listening on http://%s:%s (database=%s mode=%s)",
        __version__,
        host,
        port,
        path,
        "SIMULATED" if simulated else "real",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
