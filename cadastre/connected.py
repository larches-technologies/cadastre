"""Constrained connected-device lifecycle and read-only browsing."""

from __future__ import annotations

import codecs
import json
import logging
import os
import stat
import subprocess
import threading
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from cadastre.discovery import discover_disks

UDISKSCTL = "/usr/bin/udisksctl"
FUSER = "/usr/bin/fuser"
MOUNT_OPTIONS = "ro"
BROWSE_ENTRY_LIMIT = 500
PREVIEW_BYTE_LIMIT = 262144
LOG = logging.getLogger("cadastre.actions")


class ActionError(RuntimeError):
    def __init__(self, code, message, status=409, data=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.data = data


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
            disk["kind"] = "physical-disk"
            for part in disk["partitions"]:
                part["kind"] = "partition"
                state = self.mount_state(part["device"], part["mountpoint"]) if part["mountpoint"] else None
                owned = part["device"] in self.owned_mounts
                part["mountState"] = state
                part["cadastreOwned"] = owned
                part["accessState"] = (
                    "unmounted"
                    if not part["mountpoint"]
                    else "mounted-ro-cadastre"
                    if state == "ro" and owned
                    else "mounted-ro-external"
                    if state == "ro"
                    else "mounted-rw-external"
                    if state == "rw"
                    else "mounted-inaccessible-unknown"
                )
                part["browseAllowed"] = state == "ro"
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
            lower = detail.lower()
            unauthorized = "not authorized" in lower or "authorization" in lower
            if "already mounted" in lower:
                code, status = "ALREADY_MOUNTED", 409
            elif "option" in lower and any(word in lower for word in ("not allowed", "invalid", "unsupported")):
                code, status = "MOUNT_OPTIONS_UNSUPPORTED", 409
            elif "unknown filesystem" in lower or "wrong fs type" in lower or "not supported" in lower:
                code, status = "FILESYSTEM_UNSUPPORTED", 409
            elif "busy" in lower:
                code, status = "DEVICE_BUSY", 409
            else:
                code, status = ("UDISKS_UNAUTHORIZED", 403) if unauthorized else ("UDISKS_FAILED", 409)
            raise ActionError(
                code,
                detail or "UDisks action failed",
                status,
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
            if not verified["mountpoint"] or self.mount_state(device, verified["mountpoint"]) != "ro":
                self.run([UDISKSCTL, "unmount", "-b", device])
                self.owned_mounts.discard(device)
                raise ActionError("READ_ONLY_VERIFICATION_FAILED", "Mount was not verified read-only; browsing denied")
            LOG.info("action=mount target=%s result=verified_ro", device)
            return verified

    def mount_state(self, device, expected_target=None):
        result = self.runner(
            ["/usr/bin/findmnt", "--json", "--source", device, "--output", "SOURCE,TARGET,OPTIONS"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode:
            return None
        try:
            rows = json.loads(result.stdout).get("filesystems", [])
            if len(rows) != 1 or os.path.realpath(rows[0].get("source") or "") != os.path.realpath(device):
                return None
            if expected_target and os.path.realpath(rows[0].get("target") or "") != os.path.realpath(expected_target):
                return None
            options = set(rows[0].get("options", "").split(","))
        except (json.JSONDecodeError, IndexError, AttributeError):
            return None
        return "ro" if "ro" in options and "rw" not in options else "rw"

    def unmount(self, device):
        disk, part = self.target(device=device)
        protected = ("/", "/boot", "/usr", "/var", "/home")
        mountpoint = part["mountpoint"] or ""
        if disk["isSystem"] or any(mountpoint == root or mountpoint.startswith(root + "/") for root in protected):
            raise ActionError("UNMOUNT_NOT_ALLOWED", "System, root, boot, and host data mounts cannot be unmounted")
        if not (disk["removable"] or disk["hotplug"] or disk["transport"] == "usb"):
            raise ActionError("UNMOUNT_NOT_ALLOWED", "Only removable or hotplug partitions may be unmounted")
        if not part["mountpoint"]:
            self.owned_mounts.discard(device)
            raise ActionError("NOT_MOUNTED", "Partition is not mounted")
        if self.mount_state(device, mountpoint) not in {"ro", "rw"}:
            raise ActionError("MOUNT_SOURCE_UNVERIFIED", "Mounted source could not be freshly verified")
        with self.locks.setdefault(device, threading.Lock()):
            self.run([UDISKSCTL, "unmount", "-b", device])
        self.owned_mounts.discard(device)
        LOG.info("action=unmount target=%s result=ok", device)

    def eject(self, stable_id):
        with self.locks.setdefault(stable_id, threading.Lock()):
            disk, _ = self.target(stable_id=stable_id)
            identity = self._disk_identity(disk)
            if disk["isSystem"]:
                raise ActionError("EJECT_SYSTEM_DISK", "System disks cannot be ejected", data={"disk": identity})
            if not (disk["removable"] or disk["hotplug"] or disk["transport"] == "usb"):
                raise ActionError("EJECT_NOT_REMOVABLE", "Disk is not removable or hotplug", data={"disk": identity})
            mounted = [part for part in disk["partitions"] if part.get("mountpoint")]
            blockers = []
            for part in mounted:
                self._verify_child(stable_id, part)
                blockers.extend(self._inspect(part))
            if blockers:
                raise ActionError(
                    "EJECT_BLOCKED",
                    "Processes are using this disk",
                    data={
                        "disk": identity,
                        "blockers": blockers[:32],
                        "succeededUnmounts": [],
                        "remainingMounts": self._mounts(mounted),
                    },
                )
            succeeded = []
            for original in mounted:
                try:
                    current_disk, current = self.target(device=original["device"])
                    if current_disk["stableId"] != stable_id or current is None:
                        raise ActionError("TARGET_STALE", "Partition no longer belongs to the selected disk")
                    if not current.get("mountpoint"):
                        continue
                    self._verify_child(stable_id, current)
                    self.run([UDISKSCTL, "unmount", "-b", current["device"]])
                    succeeded.append({"device": current["device"], "mountpoint": current["mountpoint"]})
                    _, verified = self.target(device=current["device"])
                    if verified and verified.get("mountpoint"):
                        raise ActionError("UNMOUNT_VERIFICATION_FAILED", "Partition remained mounted")
                except ActionError as exc:
                    fresh, _ = self.target(stable_id=stable_id)
                    remaining = self._mounts([part for part in fresh["partitions"] if part.get("mountpoint")])
                    raise ActionError(
                        "EJECT_PARTIAL" if succeeded else exc.code,
                        "Eject stopped after a partial unmount" if succeeded else str(exc),
                        exc.status,
                        {
                            "disk": identity,
                            "succeededUnmounts": succeeded,
                            "remainingMounts": remaining,
                            "cause": exc.code,
                        },
                    ) from exc
            final, _ = self.target(stable_id=stable_id)
            remaining = [part for part in final["partitions"] if part.get("mountpoint")]
            if remaining or final["device"] != disk["device"]:
                raise ActionError(
                    "EJECT_PARTIAL" if succeeded else "TARGET_STALE",
                    "Disk state changed before power-off",
                    data={"disk": identity, "succeededUnmounts": succeeded, "remainingMounts": self._mounts(remaining)},
                )
            try:
                self.run([UDISKSCTL, "power-off", "-b", final["device"]])
            except ActionError as exc:
                raise ActionError(
                    "POWER_OFF_FAILED",
                    str(exc),
                    exc.status,
                    {"disk": identity, "succeededUnmounts": succeeded, "remainingMounts": []},
                ) from exc
        LOG.info("action=eject target=%s result=ok", final["device"])
        return {"disk": identity, "succeededUnmounts": succeeded, "poweredOff": True}

    @staticmethod
    def _disk_identity(disk):
        return {
            "stableId": str(disk["stableId"])[:256],
            "device": str(disk["device"])[:256],
            "brand": str(disk.get("brand") or "Unknown")[:128],
            "model": str(disk.get("model") or "Unknown model")[:128],
            "serial": str(disk.get("serial") or "Unavailable")[:128],
        }

    @staticmethod
    def _mounts(parts):
        return [
            {"device": str(part["device"])[:256], "mountpoint": str(part["mountpoint"])[:512]} for part in parts[:32]
        ]

    def _verify_child(self, stable_id, part):
        disk, current = self.target(device=part["device"])
        if disk["stableId"] != stable_id or current is None or current.get("mountpoint") != part.get("mountpoint"):
            raise ActionError("TARGET_STALE", "Mounted partition changed during eject preflight")
        if self.mount_state(current["device"], current["mountpoint"]) not in {"ro", "rw"}:
            raise ActionError("INSPECTION_INCONCLUSIVE", "Mounted source could not be freshly verified")

    def _inspect(self, part):
        if not os.path.isfile(FUSER) or not os.access(FUSER, os.X_OK):
            raise ActionError("INSPECTION_UNAVAILABLE", "Safe process inspection is unavailable", 503)
        try:
            result = self.runner(
                [FUSER, "-m", "--", part["mountpoint"]], capture_output=True, text=True, timeout=10, check=False
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise ActionError("INSPECTION_UNAVAILABLE", "Safe process inspection failed", 503) from exc
        if result.returncode == 1:
            return []
        if result.returncode != 0:
            raise ActionError("INSPECTION_INCONCLUSIVE", "Process inspection was inconclusive")
        pids = []
        for token in (result.stdout or "").split():
            digits = "".join(character for character in token if character.isdigit())
            if digits and int(digits) not in pids:
                pids.append(int(digits))
        if not pids:
            raise ActionError("INSPECTION_INCONCLUSIVE", "Inspector reported activity without process IDs")
        return [self._blocker(part, pid) for pid in pids[:16]]

    @staticmethod
    def _blocker(part, pid):
        root = os.path.realpath(part["mountpoint"])
        try:
            process = Path(f"/proc/{pid}/comm").read_text(errors="replace").strip()[:128] or "unknown"
        except OSError:
            process = "unknown"
        paths = []
        candidates = [Path(f"/proc/{pid}/cwd")]
        with suppress(OSError):
            candidates.extend(list(Path(f"/proc/{pid}/fd").iterdir())[:64])
        for candidate in candidates:
            target = os.path.realpath(candidate)
            if target == root or target.startswith(root + os.sep):
                relative = os.path.relpath(target, root)
                value = "/" if relative == "." else "/" + relative
                if value[:512] not in paths:
                    paths.append(value[:512])
            if len(paths) >= 8:
                break
        return {
            "device": str(part["device"])[:256],
            "mountpoint": str(part["mountpoint"])[:512],
            "pid": pid,
            "process": process,
            "openPaths": paths,
        }

    def _open_target(self, device, relative, directory):
        disk, part = self.target(device=device)
        if not part["mountpoint"] or self.mount_state(device, part["mountpoint"]) != "ro":
            raise ActionError(
                "BROWSE_REQUIRES_READ_ONLY",
                "Browsing requires a verified read-only mount; read-write mounts are blocked "
                "because reads may update atime",
                403,
            )
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
            fresh_disk, fresh_part = self.target(device=device)
            if (
                fresh_disk.get("stableId") != disk.get("stableId")
                or fresh_part.get("mountpoint") != part.get("mountpoint")
                or self.mount_state(device, fresh_part.get("mountpoint")) != "ro"
            ):
                raise ActionError("TARGET_STALE", "Device or verified read-only mount changed during access")
            return fd, fresh_disk, fresh_part
        except ActionError:
            if fd is not None:
                os.close(fd)
            raise
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            raise ActionError("PATH_NOT_SAFE", "Path is absent, inaccessible, or crosses a symlink", 400) from exc

    @staticmethod
    def _browser_context(disk, part, relative):
        return {
            "disk": {
                "brand": disk.get("brand") or "Unknown",
                "model": disk.get("model") or "Unknown model",
                "serial": disk.get("serial") or "Unavailable",
                "stableId": disk.get("stableId") or "Unavailable",
            },
            "partition": {
                "device": part["device"],
                "filesystem": part.get("filesystem") or "Unknown",
                "filesystemLabel": part.get("filesystemLabel"),
                "filesystemUuid": part.get("filesystemUuid"),
                "mountpoint": part["mountpoint"],
                "mountState": "ro",
            },
            "relativePath": relative,
        }

    def browse(self, device, relative=""):
        fd, disk, part = self._open_target(device, relative, True)
        entries = []
        truncated = False
        try:
            with os.scandir(fd) as scan:
                for item in scan:
                    if len(entries) >= BROWSE_ENTRY_LIMIT:
                        truncated = True
                        break
                    try:
                        info = item.stat(follow_symlinks=False)
                        modified = datetime.fromtimestamp(info.st_mtime, UTC).isoformat()
                    except (OSError, OverflowError, ValueError):
                        info = None
                        modified = None
                    entries.append(
                        {
                            "name": item.name,
                            "type": file_type(info.st_mode) if info else "unavailable",
                            "sizeBytes": info.st_size if info else None,
                            "modifiedAt": modified,
                            "readable": bool(info and (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))),
                        }
                    )
        finally:
            os.close(fd)
        return {
            "path": relative,
            "context": self._browser_context(disk, part, relative),
            "entries": entries,
            "truncated": truncated,
            "entryLimit": BROWSE_ENTRY_LIMIT,
        }

    def preview(self, device, relative):
        fd, disk, part = self._open_target(device, relative, False)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ActionError("PREVIEW_NOT_REGULAR", "Only regular files can be previewed", 400)
            chunks = []
            remaining = PREVIEW_BYTE_LIMIT
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
        finally:
            os.close(fd)
        truncated = info.st_size > len(data)
        try:
            if truncated:
                decoder = codecs.getincrementaldecoder("utf-8")()
                text = decoder.decode(data, final=False)
                buffered, _ = decoder.getstate()
                shown = len(data) - len(buffered)
            else:
                text = data.decode("utf-8")
                shown = len(data)
        except UnicodeDecodeError as exc:
            raise ActionError("PREVIEW_NOT_UTF8", "Binary or non-UTF-8 preview rejected", 415) from exc
        return {
            "path": relative,
            "context": self._browser_context(disk, part, relative),
            "text": text,
            "bytesShown": shown,
            "totalSizeBytes": info.st_size,
            "truncated": truncated,
            "complete": not truncated,
            "previewByteLimit": PREVIEW_BYTE_LIMIT,
        }
