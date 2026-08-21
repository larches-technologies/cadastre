# Cadastre

Cadastre MVP0 is a local, read-only inventory for offline disks. It enumerates physical host disks, requires an explicit operator selection, stores observed metadata in SQLite, and presents it in a dual-theme Larches operator UI.

## Safety model

Cadastre **observes only**. Discovery invokes `lsblk`, `udevadm`, and `smartctl` for metadata. It does not mount source disks, request read-write access, walk file trees, open user files, hash files, wipe, format, delete, or modify disk state. System disks are identified from system mount points and hidden by default. Showing one still requires explicit selection and a second confirmation.

The SQLite database contains inventory records only and defaults to `data/cadastre.db` on the local machine. The server rejects non-loopback bind addresses.

## Run

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). No Python runtime dependencies are required.

```bash
uv run cadastre serve
# open http://127.0.0.1:8741
```

Optional commands:

```bash
uv run cadastre discover
uv run cadastre discover --include-system
CADASTRE_DB=/safe/local/path.db uv run cadastre serve
curl http://127.0.0.1:8741/health
```

## Host support dependencies

These are operating-system packages, not Python dependencies. Cadastre reports degraded/unknown evidence rather than pretending unavailable evidence succeeded.

- `util-linux`: required `lsblk`; `blkid` is useful for filesystem metadata diagnostics.
- `udev`: recommended `udevadm` for vendor/model/serial enrichment.
- `smartmontools`: recommended `smartctl` for SMART health. Without it, SMART displays **unavailable**.
- NTFS: `ntfs-3g` provides Linux read-only inspection support for later workflows; MVP0 identifies NTFS from block metadata only.
- exFAT: `exfatprogs`.
- FAT32: `dosfstools`.
- ext2/3/4: `e2fsprogs`.
- APFS: Linux support varies; `apfs-fuse` is read-only but must **not** be used to mount source disks as part of MVP0. Native macOS tooling may identify APFS. MVP0 displays what `lsblk` reports and makes no support claim when it cannot identify APFS.

Do not install filesystem repair utilities in order to run MVP0. No source filesystem is mounted or repaired.

## API and persisted schema

- `GET /health` and `GET /ready`: local readiness and version signal.
- `GET /api/disks`: persisted inventory.
- `GET /api/discovery?includeSystem=false`: live physical-device metadata.
- `POST /api/disks` with `{"stableId":"..."}`: explicitly add/refresh a currently discovered disk.

Every disk response includes `indexAttempts` and `indexDates`, including empty arrays. File indexing is deliberately deferred, so MVP0 does not create fake attempts or dates.

## Development gates

```bash
uv run python -m unittest discover -s tests -v
uvx ruff check .
uvx ruff format --check .
```

## Deliberately deferred

File/folder indexing, hashes, similarity, plug/inotify watching, copy or follow-up workflows, mounts, wipe, delete, repair, and remote/tenant deployment are outside MVP0.
