"""Constrained connected-device lifecycle and read-only browsing."""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import PurePosixPath

from cadastre.discovery import discover_disks

UDISKSCTL = "/usr/bin/udisksctl"
MOUNT_OPTIONS = "ro,nodev,nosuid,noexec"
LOG = logging.getLogger("cadastre.actions")


class ActionError(RuntimeError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code = code
        self.status = status


def safe_relative(value):
    if "\x00" in value or value.startswith("/"):
        raise ActionError("INVALID_PATH", "Path must be relative", 400)
    path = PurePosixPath(value or ".")
    if ".." in path.parts:
        raise ActionError("INVALID_PATH", "Parent traversal is not permitted", 400)
    return path


def file_type(mode):
    for check, name in (
        (stat.S_ISDIR, "directory"),
        (stat.S_ISREG, "file"),
        (stat.S_ISLNK, "symlink"),
        (stat.S_ISFIFO, "fifo"),
        (stat.S_ISSOCK, "socket"),
        (stat.S_ISBLK, "block-device"),
        (stat.S_ISCHR, "character-device"),
    ):
        if check(mode):
            return name
    return "special"


class ConnectedDevices:
    def __init__(self, discoverer=discover_disks, runner=subprocess.run):
        self.discoverer = discoverer
        self.runner = runner
        self.locks = {}
        self.owned_mounts = set()

    def live(self):
        disks = self.discoverer().disks
        for disk in disks:
            for part in disk["partitions"]:
                part["mountState"] = self.mount_state(part["device"]) if part["mountpoint"] else None
                part["cadastreOwned"] = part["device"] in self.owned_mounts
        return disks

    def target(self, device=None, stable_id=None):
        for disk in self.live():
            if stable_id == disk["stableId"]:
                return disk, None
            for part in disk["partitions"]:
                if device == part["device"]:
                    return disk, part
        raise ActionError("TARGET_STALE", "Target is absent or stale")

    def run(self, argv):
        try:
            result = self.runner(argv, capture_output=True, text=True, timeout=20, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ActionError("ACTION_TIMEOUT", "UDisks action timed out", 504) from exc
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            unauthorized = "not authorized" in detail.lower()
            raise ActionError(
                "UDISKS_UNAUTHORIZED" if unauthorized else "UDISKS_FAILED",
                detail or "UDisks action failed",
                403 if unauthorized else 409,
            )
        return result

    def mount(self, device):
        disk, part = self.target(device=device)
        if disk["isSystem"] or not part["supported"] or part["identityAmbiguous"]:
            raise ActionError("MOUNT_NOT_ALLOWED", "Only supported non-system partitions may be mounted")
        if part["mountpoint"]:
            raise ActionError("ALREADY_MOUNTED", "Partition is already mounted")
        with self.locks.setdefault(device, threading.Lock()):
            self.run([UDISKSCTL, "mount", "-b", device, "-o", MOUNT_OPTIONS])
            self.owned_mounts.add(device)
            _, verified = self.target(device=device)
            if not verified["mountpoint"] or self.mount_state(device) != "ro":
                self.run([UDISKSCTL, "unmount", "-b", device])
                self.owned_mounts.discard(device)
                raise ActionError("READ_ONLY_VERIFICATION_FAILED", "Mount was not verified read-only; browsing denied")
            LOG.info("action=mount target=%s result=verified_ro", device)
            return verified

    def mount_state(self, device):
        result = self.runner(
            ["/usr/bin/findmnt", "--json", "--source", device, "--output", "TARGET,OPTIONS"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode:
            return None
        try:
            options = set(json.loads(result.stdout).get("filesystems", [])[0].get("options", "").split(","))
        except (json.JSONDecodeError, IndexError, AttributeError):
            return None
        return "ro" if "ro" in options and "rw" not in options else "rw"

    def unmount(self, device):
        _, part = self.target(device=device)
        if device not in self.owned_mounts:
            raise ActionError("UNMOUNT_NOT_OWNED", "Only Cadastre-initiated mounts may be unmounted")
        if not part["mountpoint"]:
            self.owned_mounts.discard(device)
            raise ActionError("NOT_MOUNTED", "Partition is not mounted")
        with self.locks.setdefault(device, threading.Lock()):
            self.run([UDISKSCTL, "unmount", "-b", device])
        self.owned_mounts.discard(device)
        LOG.info("action=unmount target=%s result=ok", device)

    def eject(self, stable_id):
        disk, _ = self.target(stable_id=stable_id)
        if disk["isSystem"] or not (disk["removable"] or disk["hotplug"] or disk["transport"] == "usb"):
            raise ActionError("EJECT_NOT_ALLOWED", "Disk is not safely ejectable")
        if any(p["mountpoint"] for p in disk["partitions"]):
            raise ActionError("EJECT_MOUNTED", "Unmount every child partition first")
        with self.locks.setdefault(stable_id, threading.Lock()):
            self.run([UDISKSCTL, "power-off", "-b", disk["device"]])
        LOG.info("action=eject target=%s result=ok", disk["device"])

    def _open_target(self, device, relative, directory):
        _, part = self.target(device=device)
        if not part["mountpoint"] or self.mount_state(device) != "ro":
            raise ActionError("BROWSE_REQUIRES_READ_ONLY", "Browsing requires a freshly verified read-only mount", 403)
        components = [part for part in safe_relative(relative).parts if part not in {".", ""}]
        # O_NONBLOCK prevents special files such as FIFOs from hanging before
        # fstat can reject them; it does not change regular-file reads.
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = None
        try:
            fd = os.open(part["mountpoint"], flags | os.O_DIRECTORY)
            for index, component in enumerate(components):
                child_flags = flags | (os.O_DIRECTORY if directory or index < len(components) - 1 else 0)
                child = os.open(component, child_flags, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            raise ActionError("PATH_NOT_SAFE", "Path is absent, inaccessible, or crosses a symlink", 400) from exc

    def browse(self, device, relative=""):
        fd = self._open_target(device, relative, True)
        entries = []
        truncated = False
        try:
            with os.scandir(fd) as scan:
                for item in scan:
                    if len(entries) >= 500:
                        truncated = True
                        break
                    info = item.stat(follow_symlinks=False)
                    entries.append(
                        {
                            "name": item.name,
                            "type": file_type(info.st_mode),
                            "sizeBytes": info.st_size,
                            "mtime": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
                            "readable": stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode),
                        }
                    )
        finally:
            os.close(fd)
        return {"path": relative, "entries": entries, "truncated": truncated}

    def preview(self, device, relative):
        fd = self._open_target(device, relative, False)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ActionError("PREVIEW_NOT_REGULAR", "Only regular files can be previewed", 400)
            if info.st_size > 262144:
                raise ActionError("PREVIEW_TOO_LARGE", "Preview limited to 256 KiB", 413)
            data = os.read(fd, 262145)
        finally:
            os.close(fd)
        if len(data) > 262144:
            raise ActionError("PREVIEW_TOO_LARGE", "Preview limited to 256 KiB", 413)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ActionError("PREVIEW_NOT_UTF8", "Binary or non-UTF-8 preview rejected", 415) from exc
        return {"path": relative, "text": text, "sizeBytes": len(data)}
