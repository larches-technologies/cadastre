# Resumable metadata indexing

Cadastre indexes **metadata only** under an explicit root supplied by its caller. Phase 1 does not discover disks or mount filesystems. It never reads file contents, hashes files, expands archives, or follows symlinks. Symlinks and special files are recorded as metadata and never traversed.

## Identity and control plane

SQLite schema version 4 is an additive coordinator. Each generation pins exact physical disk stable ID, filesystem lineage and filesystem UUID, plus the supplied root. Start and resume fail closed on mismatch. One globally active generation is allowed. Pending directories, controls, counts, bounded errors, heartbeat/lease fields, state and fragment manifests are durable.

The engine is standalone and synchronous. It uses short SQLite transactions and checks pause/stop between directories. Pause is acknowledged only after the current directory handle is closed and its fragment is published. Stop retains registered partial fragments. A startup recovery helper converts abandoned active states to `interrupted` or `waiting_for_media` based only on availability supplied by the caller. A fresh engine can resume the durable queue after exact identity validation.

## Parquet publication and queries

Fragments live under `<database>.fragments/<generation>/`. DuckDB writes a temporary Parquet file, closes it, Cadastre atomically renames it, then registers its relative path in SQLite. Queries construct their file list solely from this manifest, so orphan temporary or unregistered files are ignored. Search and limits are parameters, limits are capped at 200, and callers cannot provide SQL or paths. Results label partial versus complete truth.

Each entry stores contained relative/parent path, name, type, regular-file size, mtime epoch/UTC, generation-scoped mode/inode evidence and observation timestamp. Per-path error text is truncated to 1000 characters. Query limits are hard-capped at 200 and a timer interrupts over-deadline DuckDB work.

## Growth, retention and crash semantics

Growth is approximately one compressed Parquet row per observed directory entry plus SQLite pending/error/manifest bookkeeping. Frequent small batches improve checkpoints but add fragment overhead. This slice does not compact, expire, or deduplicate generations: budget storage proportional to retained observations. A crash before rename leaves a temporary orphan; after rename but before registration leaves an ignored orphan; after registration the fragment is committed truth. Pending work remains resumable. Complete means the durable queue drained; partial means only registered fragments are authoritative.

## Phase 2 boundary and deferred work

Phase 1 intentionally has no HTTP routes, worker process, UI, polling, mount/eject/power controls, lifecycle interlocks, deployment, retention, or compaction. Those integrations belong to Phase 2 and must revalidate live read-only availability before starting or resuming this engine. Content-derived features remain out of scope.

## Phase 2A backend contract

The local server adds a non-blocking single-worker supervisor with persisted lease, heartbeat, current relative path, counters, partial truth, and explicit quiescence. Locked states are `queued`, `starting`, `running`, `pause_requested`, `paused`, `waiting_for_remount`, `stopping`, `stopped`, `interrupted`, `completed`, `completed_with_errors`, and `failed`; pause/stop remain separate control requests. Acknowledgement occurs only at a directory checkpoint after fragment close/registration and filesystem-handle closure.

Routes are `GET /api/indexes`, `GET /api/indexes/{id}`, `POST /api/indexes/start`, `POST /api/indexes/{id}/pause|resume|stop`, `GET /api/indexes/{id}/search`, and `GET /api/indexes/{id}/summary`. Existing same-origin action-token protection applies to writes. Resume ignores client identity and mount claims: fresh ConnectedDevices discovery must match persisted disk stable ID, lineage, filesystem UUID, and an authorized read-only mount. Related unmount/eject/inventory removal fails closed with `INDEX_ACTIVE` until server-side quiescence is confirmed.

Phase 2B owns UI, deployment/restart, retention, and fragment compaction.
