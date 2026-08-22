# Cadastre

Cadastre MVP0 is a local, read-only inventory for offline disks. It enumerates physical host disks, requires an explicit operator selection, stores observed metadata in SQLite, and presents it in a dual-theme Larches operator UI.

## Resumable indexing

See [docs/indexing.md](docs/indexing.md) for identity validation, SQLite/Parquet storage, crash semantics, cooperative controls, growth, and deferred work.

## Safety model

Cadastre inventory **observes only**. The separate Connected devices workflow can explicitly mount supported external partitions read-only, browse verified read-only mounts, unmount after confirmation, and safely power off eligible physical disks. It never requests read-write access, hashes files, wipes, formats, repairs, or deletes source data. System disks are protected from lifecycle actions.

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

File/folder indexing, hashes, similarity, plug/inotify watching, copy or follow-up workflows, wipe, delete, repair, and remote/tenant deployment are outside MVP0.

### Partition incarnation identity

Cadastre inventory selection is partition-level. Supported filesystems are ext2, ext3, ext4, NTFS, exFAT, and VFAT/FAT32 aliases. The server re-runs discovery and validates every selected incarnation; client metadata is never persisted directly. APFS, unknown filesystems, and duplicate filesystem UUIDs are blocked.

The preferred identity is disk stable ID plus filesystem UUID. If UUID is absent, Cadastre uses disk stable ID plus PARTUUID, then partition number/path as a deterministic degraded fallback requiring explicit confirmation. Reformatting to a new filesystem UUID creates a distinct record. A new incarnation in the same partition slot marks its prior incarnation `replaced` and records `replaces`/`replacedBy`. Historical rows are retained. Duplicate filesystem UUIDs discovered concurrently are ambiguous and cannot be added.

The additive `partition_incarnations` table is created with `CREATE TABLE IF NOT EXISTS`; existing whole-disk `disks` and `index_attempts` rows remain readable. `GET /api/disks` returns both `disks` and `partitions`; `POST /api/partitions` accepts `diskStableId`, freshly discovered `incarnationIds`, and explicit confirmation flags when required.

### Remove from inventory

`DELETE /api/disks/{stableId}` removes only the trusted persisted Cadastre disk row and its associated partition-incarnation and index-attempt history in one SQLite transaction. An absent ID returns `404 INVENTORY_DISK_NOT_FOUND`. It invokes no disk command and does not unmount, write, format, wipe, delete, or otherwise modify the physical disk/filesystems. The disk can be discovered and added again. Inventory-record removal is not undoable in MVP0 and requires explicit UI confirmation.

## Connected devices versus Inventory

**Connected devices** is freshly discovered live host state and refreshes only when the operator selects Refresh. **Inventory** is persisted Cadastre metadata and history; removing an inventory record never changes the physical disk.

Connected-device lifecycle uses only fixed-argv `/usr/bin/udisksctl` commands. UDisks/polkit may request or deny host authorization; Cadastre makes no policy, package, or configuration changes. Mount requests exactly `ro`, the portable UDisks read-only option, then independently verifies the canonical source, target, and active read-only options with `findmnt`. The previous extra option set could be rejected by UDisks/filesystem policy before a mount occurred. An existing external read-write mount remains visible but is never silently remounted and cannot be browsed because reads may update atime. If post-mount verification fails, browsing is denied and only the mount initiated by the current Cadastre process is rolled back.

Unmount requires confirmation and applies to freshly verified non-system removable/hotplug/USB partition mounts, including desktop/UDisks auto-mounts; no force or lazy unmount is used. An external read-only mount can be browsed after a Cadastre restart without process-local ownership. Eject is UDisks power-off, requires confirmation, applies only to non-system removable/hotplug/USB physical disks, and requires every child partition to be unmounted.

The browser is read-only and one-level-per-request. It performs descriptor-relative, `O_NOFOLLOW` traversal, rejects absolute/NUL/parent paths and symlink traversal, labels symlinks and special files, never opens devices/FIFOs/sockets, caps listings at 500 entries, and offers UTF-8 regular-file preview only up to 256 KiB. There is no download, recursive walk, index, hash, search, or thumbnail generation.

The HTTP service remains loopback-only. Every state-changing API, including inventory removal, requires a per-process action token and rejects cross-origin browser requests. The token is supplied to the local UI in a no-store session response and is not logged.

### Indexing workspace

Browse to `/indexing` to start and supervise metadata-only generations from server-authoritative connected inventory filesystems, inspect truthful lifecycle/quiescence states, and run bounded metadata search. See `docs/indexing.md` for workflow and limitations.

## Safe simulated-disk mode

Cadastre has an explicit, default-off development mode for UI/API exercises. Start it from the repository root with an isolated simulation database:

```sh
uv run cadastre serve --simulate-disks --db .runtime/cadastre-simulation.db
```

Open `http://127.0.0.1:8741`. A red **SIMULATED DISK MODE** banner is always shown, API objects carry `provenance.mode = "simulated"`, and responses carry `X-Cadastre-Provenance: simulated`. Stop it with `Ctrl-C`. Optionally remove only the isolated database afterward:

```sh
rm -f .runtime/cadastre-simulation.db .runtime/cadastre-simulation.db-shm .runtime/cadastre-simulation.db-wal
```

Safety boundary: simulation selects a separate in-memory provider before discovery/lifecycle wiring. It never constructs the real `ConnectedDevices` provider and has no subprocess runner, `/dev` path, mount syscall, or host filesystem root. Fixtures and lifecycle state exist only in process memory; inventory records use the explicitly isolated SQLite file. Normal `cadastre serve` remains real mode and unchanged. Never point simulated mode at the production database.

Deterministic fixtures cover a normal USB disk, protected system disk, unsupported filesystem, ambiguous identity, and an already-mounted read-only browse/preview fixture. Simulated indexing is intentionally rejected: the real index engine requires a verified host `Path`, so enabling it would weaken the isolation boundary. Ejected/mounted state resets when the process stops. This mode does not emulate kernel, udev, SMART, mount timing, permissions, media failure, or real filesystem behavior.
