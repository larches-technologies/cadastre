"""Fully isolated deterministic disk fixtures for development testing."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from cadastre.connected import BROWSE_ENTRY_LIMIT, PREVIEW_BYTE_LIMIT, ActionError, safe_relative
from cadastre.discovery import DiscoveryResult

PROVENANCE = {"mode": "simulated", "simulated": True, "source": "cadastre-deterministic-fixtures"}


def _partition(name: str, filesystem: str, *, supported: bool = True, ambiguous: bool = False) -> dict[str, Any]:
    return {
        "name": name,
        "device": f"sim://partition/{name}",
        "number": 1,
        "partuuid": f"sim-partuuid-{name}",
        "sizeBytes": 8_000_000_000,
        "startBytes": 1_048_576,
        "filesystem": filesystem,
        "filesystemLabel": f"SIMULATED {name.upper()}",
        "filesystemUuid": f"sim-fsuuid-{name}",
        "mountpoint": None,
        "readOnly": False,
        "incarnationId": f"sim-disk-{name}:fsuuid:sim-fsuuid-{name}",
        "lineageId": f"sim-disk-{name}:fsuuid:sim-fsuuid-{name}",
        "identityConfidence": "high",
        "requiresIdentityConfirmation": False,
        "identityAmbiguous": ambiguous,
        "supported": supported and not ambiguous,
        "provenance": PROVENANCE.copy(),
    }


def _disk(name: str, model: str, part: dict[str, Any], *, system: bool = False) -> dict[str, Any]:
    return {
        "device": f"sim://disk/{name}",
        "stableId": f"sim-disk-{name}",
        "brand": "SIMULATED",
        "model": f"SIMULATED {model}",
        "sizeBytes": 16_000_000_000,
        "serial": f"SIM-{name.upper()}-0001",
        "partitionTable": "gpt",
        "partitions": [part],
        "filesystems": [part["filesystem"]],
        "smartStatus": "simulated-passed",
        "smartDetail": "Deterministic fixture; no hardware query was performed",
        "transport": "internal" if system else "usb",
        "removable": not system,
        "hotplug": not system,
        "readOnly": False,
        "isSystem": system,
        "systemReasons": ["SIMULATED protected system disk"] if system else [],
        "kind": "simulated-disk",
        "provenance": PROVENANCE.copy(),
    }


def fixtures() -> list[dict[str, Any]]:
    return [
        _disk("usb-normal", "External USB", _partition("usb-normal", "ext4")),
        _disk("system-protected", "Protected System Disk", _partition("system-protected", "ext4"), system=True),
        _disk("unsupported", "Unsupported Filesystem", _partition("unsupported", "zfs", supported=False)),
        _disk("ambiguous", "Ambiguous Identity", _partition("ambiguous", "ext4", ambiguous=True)),
        _disk("browseable", "Read-only Browse Fixture", _partition("browseable", "exfat")),
    ]


def discover_simulated() -> DiscoveryResult:
    return DiscoveryResult(fixtures(), ["SIMULATED fixtures only; no host disk tools were invoked"])


class SimulatedDevices:
    """In-memory lifecycle/browser; deliberately has no runner or host path."""

    simulated = True

    def __init__(self) -> None:
        self._disks, self._ejected = fixtures(), set()
        self._mounted = {"sim://partition/browseable"}
        self._files = {
            "sim://partition/browseable": {
                "README.txt": "SIMULATED read-only fixture. No real disk or mount is involved.\n",
                "Documents/notes.txt": "Deterministic Cadastre simulated browsing content.\n",
                "Documents/inventory.csv": "name,size\nalpha,12\nbeta,34\n",
            }
        }

    def live(self):
        disks = deepcopy([d for d in self._disks if d["stableId"] not in self._ejected])
        for disk in disks:
            for part in disk["partitions"]:
                mounted = part["device"] in self._mounted
                part.update(
                    {
                        "mountpoint": f"sim://mount/{part['name']}" if mounted else None,
                        "mountState": "ro" if mounted else None,
                        "cadastreOwned": mounted,
                        "accessState": "mounted-ro-cadastre" if mounted else "unmounted",
                        "browseAllowed": mounted and part["device"] in self._files,
                        "provenance": PROVENANCE.copy(),
                    }
                )
        return disks

    def target(self, device=None, stable_id=None):
        for disk in self.live():
            if stable_id == disk["stableId"]:
                return disk, None
            for part in disk["partitions"]:
                if device == part["device"]:
                    return disk, part
        raise ActionError("TARGET_STALE", "Simulated target is absent or stale")

    def mount(self, device):
        disk, part = self.target(device=device)
        if disk["isSystem"] or not part["supported"] or part["identityAmbiguous"]:
            raise ActionError("MOUNT_NOT_ALLOWED", "Only supported non-system simulated partitions may be mounted")
        if part["mountpoint"]:
            raise ActionError("ALREADY_MOUNTED", "Simulated partition is already mounted")
        self._mounted.add(device)
        return self.target(device=device)[1]

    def unmount(self, device):
        disk, part = self.target(device=device)
        if disk["isSystem"]:
            raise ActionError("UNMOUNT_NOT_ALLOWED", "Simulated system disks cannot be unmounted")
        if not part["mountpoint"]:
            raise ActionError("NOT_MOUNTED", "Simulated partition is not mounted")
        self._mounted.remove(device)

    def eject(self, stable_id):
        disk, _ = self.target(stable_id=stable_id)
        if disk["isSystem"]:
            raise ActionError("EJECT_SYSTEM_DISK", "Simulated system disks cannot be ejected")
        for part in disk["partitions"]:
            self._mounted.discard(part["device"])
        self._ejected.add(stable_id)
        return {"stableId": stable_id, "ejected": True, "provenance": PROVENANCE.copy()}

    def _content(self, device):
        disk, part = self.target(device=device)
        if not part["browseAllowed"]:
            raise ActionError("BROWSE_NOT_ALLOWED", "Not an authorized simulated read-only fixture", 403)
        return self._files[device], disk, part

    @staticmethod
    def _context(disk, part, relative):
        return {
            "disk": {k: disk[k] for k in ("stableId", "brand", "model")},
            "partition": {
                k: part[k]
                for k in ("device", "filesystem", "filesystemLabel", "filesystemUuid", "mountpoint", "mountState")
            },
            "relativePath": relative,
            "provenance": PROVENANCE.copy(),
        }

    def browse(self, device, relative=""):
        files, disk, part = self._content(device)
        normalized = str(safe_relative(relative))
        prefix = "" if normalized == "." else normalized.rstrip("/") + "/"
        entries = {}
        for path, text in files.items():
            if path.startswith(prefix):
                first, _, tail = path[len(prefix) :].partition("/")
                entries[first] = {
                    "name": first,
                    "type": "directory" if tail else "file",
                    "sizeBytes": None if tail else len(text.encode()),
                    "modifiedAt": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
                    "readable": True,
                }
        return {
            "path": relative,
            "context": self._context(disk, part, relative),
            "entries": list(entries.values()),
            "truncated": False,
            "entryLimit": BROWSE_ENTRY_LIMIT,
            "provenance": PROVENANCE.copy(),
        }

    def preview(self, device, relative):
        files, disk, part = self._content(device)
        path = str(safe_relative(relative))
        if path not in files:
            raise ActionError("PREVIEW_NOT_REGULAR", "Simulated target is not a regular file", 400)
        encoded = files[path].encode()
        shown = encoded[:PREVIEW_BYTE_LIMIT]
        return {
            "path": relative,
            "context": self._context(disk, part, relative),
            "text": shown.decode(),
            "bytesShown": len(shown),
            "totalSizeBytes": len(encoded),
            "truncated": len(shown) < len(encoded),
            "complete": len(shown) == len(encoded),
            "previewByteLimit": PREVIEW_BYTE_LIMIT,
            "provenance": PROVENANCE.copy(),
        }
