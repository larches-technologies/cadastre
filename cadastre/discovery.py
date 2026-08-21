"""Read-only block-device metadata discovery."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

Runner = Callable[..., subprocess.CompletedProcess[str]]

SUPPORTED_FILESYSTEMS = frozenset({"ext2", "ext3", "ext4", "ntfs", "exfat", "vfat"})
FILESYSTEM_ALIASES = {"fat": "vfat", "fat32": "vfat", "msdos": "vfat"}


class DiscoveryError(RuntimeError):
    """Discovery failed before a trustworthy device list could be produced."""


@dataclass(frozen=True)
class DiscoveryResult:
    disks: list[dict[str, Any]]
    warnings: list[str]


def _run(command: list[str], runner: Runner = subprocess.run) -> subprocess.CompletedProcess[str]:
    return runner(command, capture_output=True, text=True, timeout=10, check=False)


def _flatten_mounts(node: dict[str, Any]) -> list[str]:
    mounts = [m for m in (node.get("mountpoints") or []) if m]
    for child in node.get("children") or []:
        mounts.extend(_flatten_mounts(child))
    return mounts


def _filesystems(node: dict[str, Any]) -> list[str]:
    found: list[str] = []
    fs = node.get("fstype")
    if fs and fs not in found:
        found.append(fs)
    for child in node.get("children") or []:
        for child_fs in _filesystems(child):
            if child_fs not in found:
                found.append(child_fs)
    return found


def normalize_filesystem(value: str | None) -> str | None:
    """Normalize only known lsblk aliases; unknown values remain truthful."""
    if not value:
        return None
    normalized = value.strip().lower()
    return FILESYSTEM_ALIASES.get(normalized, normalized)


def _partitions(node: dict[str, Any], disk_stable_id: str) -> list[dict[str, Any]]:
    """Return partition metadata already present in the lsblk device tree."""
    partitions: list[dict[str, Any]] = []
    for child in node.get("children") or []:
        if child.get("type") == "part":
            device = child.get("path") or (f"/dev/{child['name']}" if child.get("name") else None)
            name = child.get("name") or device or "Unknown partition"
            number = child.get("partn")
            mountpoints = [mount for mount in (child.get("mountpoints") or []) if mount]
            filesystem = normalize_filesystem(child.get("fstype"))
            filesystem_uuid = (child.get("uuid") or "").strip() or None
            partuuid = (child.get("partuuid") or "").strip() or None
            if filesystem_uuid:
                identity_key = f"fsuuid:{filesystem_uuid.lower()}"
                confidence = "high"
            elif partuuid:
                identity_key = f"partuuid:{partuuid.lower()}"
                confidence = "degraded"
            else:
                slot = str(number) if number is not None else (device or name)
                identity_key = f"slot:{slot}"
                confidence = "degraded"
            partitions.append(
                {
                    "name": name,
                    "device": device,
                    "number": number,
                    "partuuid": partuuid,
                    "sizeBytes": int(child.get("size") or 0),
                    "startBytes": int(child.get("start") or 0),
                    "filesystem": filesystem,
                    "filesystemUuid": filesystem_uuid,
                    "mountpoint": mountpoints[0] if mountpoints else None,
                    "readOnly": bool(child.get("ro")),
                    "incarnationId": f"{disk_stable_id}:{identity_key}",
                    "identityConfidence": confidence,
                    "requiresIdentityConfirmation": confidence == "degraded",
                    "identityAmbiguous": False,
                    "supported": filesystem in SUPPORTED_FILESYSTEMS,
                }
            )
        partitions.extend(_partitions(child, disk_stable_id))
    return partitions


def _mark_duplicate_filesystem_uuids(disks: list[dict[str, Any]]) -> None:
    by_uuid: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for disk in disks:
        for partition in disk["partitions"]:
            if partition["filesystemUuid"]:
                by_uuid.setdefault(partition["filesystemUuid"].lower(), []).append((disk, partition))
    for matches in by_uuid.values():
        disk_ids = {disk["stableId"] for disk, _ in matches}
        if len(matches) <= 1:
            continue
        if len(disk_ids) == 1:
            for _, partition in matches:
                partition["identityAmbiguous"] = True
                partition["supported"] = False
        else:
            for _, partition in matches:
                partition["possibleClone"] = True
                partition["duplicateFilesystemUuid"] = True


def _udev_properties(device: str, runner: Runner) -> dict[str, str]:
    if not shutil.which("udevadm"):
        return {}
    result = _run(["udevadm", "info", "--query=property", f"--name={device}"], runner)
    if result.returncode:
        return {}
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def _smart_status(device: str, runner: Runner) -> tuple[str, str | None]:
    if not shutil.which("smartctl"):
        return "unavailable", "smartctl is not installed"
    result = _run(["smartctl", "-H", "-j", device], runner)
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return "unknown", (result.stderr.strip() or "smartctl returned invalid JSON")
    passed = (data.get("smart_status") or {}).get("passed")
    if passed is True:
        return "passed", None
    if passed is False:
        return "failed", None
    messages = data.get("messages") or []
    detail = "; ".join(m.get("string", "") for m in messages if m.get("string"))
    return "unknown", detail or result.stderr.strip() or None


def discover_disks(runner: Runner = subprocess.run) -> DiscoveryResult:
    """Enumerate physical disks without mounting or traversing their contents."""
    if not shutil.which("lsblk"):
        raise DiscoveryError("lsblk is required but was not found")
    columns = "NAME,PATH,TYPE,SIZE,MODEL,VENDOR,SERIAL,WWN,PTTYPE,FSTYPE,UUID,PARTUUID,PARTN,START,MOUNTPOINTS,RM,HOTPLUG,RO,TRAN"  # noqa: E501
    result = _run(["lsblk", "--json", "--bytes", "--output", columns], runner)
    if result.returncode:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise DiscoveryError(f"lsblk discovery failed: {detail}")
    try:
        nodes = json.loads(result.stdout).get("blockdevices", [])
    except (json.JSONDecodeError, AttributeError) as exc:
        raise DiscoveryError("lsblk returned malformed JSON") from exc

    disks: list[dict[str, Any]] = []
    warnings: list[str] = []
    for node in nodes:
        if node.get("type") != "disk":
            continue
        device = node.get("path") or f"/dev/{node.get('name')}"
        mounts = _flatten_mounts(node)
        system_reasons = sorted({m for m in mounts if m == "/" or m.startswith(("/boot", "/usr", "/var", "/home"))})
        props = _udev_properties(device, runner)
        smart, smart_detail = _smart_status(device, runner)
        if smart_detail and smart == "unknown":
            warnings.append(f"{device}: {smart_detail}")
        vendor = (node.get("vendor") or props.get("ID_VENDOR_FROM_DATABASE") or props.get("ID_VENDOR") or "").strip()
        model = (
            node.get("model") or props.get("ID_MODEL_FROM_DATABASE") or props.get("ID_MODEL") or "Unknown model"
        ).strip()
        serial = (node.get("serial") or props.get("ID_SERIAL_SHORT") or "").strip()
        stable_id = (node.get("wwn") or serial or os.path.realpath(device)).strip()
        disks.append(
            {
                "device": device,
                "stableId": stable_id,
                "brand": vendor or "Unknown",
                "model": model,
                "sizeBytes": int(node.get("size") or 0),
                "serial": serial or "Unavailable",
                "partitionTable": node.get("pttype") or "None detected",
                "partitions": _partitions(node, stable_id),
                "filesystems": _filesystems(node),
                "smartStatus": smart,
                "smartDetail": smart_detail,
                "transport": node.get("tran") or "unknown",
                "removable": bool(node.get("rm")),
                "hotplug": bool(node.get("hotplug")),
                "readOnly": bool(node.get("ro")),
                "isSystem": bool(system_reasons),
                "systemReasons": system_reasons,
            }
        )
    _mark_duplicate_filesystem_uuids(disks)
    return DiscoveryResult(disks=disks, warnings=warnings)


def filter_system_disks(disks: list[dict[str, Any]], include_system: bool = False) -> list[dict[str, Any]]:
    """Hide disks carrying system mount points unless the operator opts in."""
    return disks if include_system else [disk for disk in disks if not disk["isSystem"]]
