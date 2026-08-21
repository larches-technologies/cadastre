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


def _partitions(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Return partition metadata already present in the lsblk device tree."""
    partitions: list[dict[str, Any]] = []
    for child in node.get("children") or []:
        if child.get("type") == "part":
            device = child.get("path") or (f"/dev/{child['name']}" if child.get("name") else None)
            mountpoints = [mount for mount in (child.get("mountpoints") or []) if mount]
            partitions.append(
                {
                    "name": child.get("name") or device or "Unknown partition",
                    "device": device,
                    "sizeBytes": int(child.get("size") or 0),
                    "filesystem": child.get("fstype") or None,
                    "mountpoint": mountpoints[0] if mountpoints else None,
                }
            )
        partitions.extend(_partitions(child))
    return partitions


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
    columns = "NAME,PATH,TYPE,SIZE,MODEL,VENDOR,SERIAL,WWN,PTTYPE,FSTYPE,MOUNTPOINTS,RM,RO,TRAN"
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
                "partitions": _partitions(node),
                "filesystems": _filesystems(node),
                "smartStatus": smart,
                "smartDetail": smart_detail,
                "transport": node.get("tran") or "unknown",
                "removable": bool(node.get("rm")),
                "readOnly": bool(node.get("ro")),
                "isSystem": bool(system_reasons),
                "systemReasons": system_reasons,
            }
        )
    return DiscoveryResult(disks=disks, warnings=warnings)


def filter_system_disks(disks: list[dict[str, Any]], include_system: bool = False) -> list[dict[str, Any]]:
    """Hide disks carrying system mount points unless the operator opts in."""
    return disks if include_system else [disk for disk in disks if not disk["isSystem"]]
