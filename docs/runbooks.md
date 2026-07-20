# CloudNativePG runbooks

Remediation for the operational signals the console watches. Each section is the
target of a console **Runbook** link and of the `runbook_url` on the matching
Prometheus alert, so an alert or a red health row lands you here.

Most sections have a one-click action in the console (Operate → Health & runbooks,
or the Routine actions). The console runs only safe, whitelisted operations; the
sharper remediations below (dropping a slot, cancelling a backend, `VACUUM FULL`)
are things you do deliberately from `psql`.

Format: **what it means → why it matters → diagnose → fix → prevent.**

---

<a id="disk"></a>
## Disk filling

**Means** a data volume is past its warn/crit fill line. **Matters** because a full
volume takes the instance read-only or crashes it, and storage is grow-only.

- **Diagnose** — check the Disk panel per instance. Rule out WAL being pinned (see
  [replication slots](#slots)) or archiving being stuck (see [WAL archive delay](#archive)) before assuming it's real data growth.
- **Fix** — **Expand storage** (Operate) raises `spec.storage.size`; the operator
  resizes each PVC online **if** the storage class has `allowVolumeExpansion: true`.
  Grow a little at a time — you can't shrink back.
- **Prevent** — set a disk goal so the alert fires with headroom, and size volumes
  for growth. If expansion isn't supported, plan a migrate/restore to a bigger volume.

<a id="xid"></a>
## Transaction-ID age (wraparound)

**Means** the oldest unfrozen transaction ID is climbing toward the ~2.1 billion
limit. **Matters** because at the limit PostgreSQL **stops accepting writes** to
protect against wraparound — a hard outage.

- **Diagnose** — the Txn-ID age panel/health row. Autovacuum normally freezes old
  rows; steady growth means it's being **blocked**.
- **Fix** — run **VACUUM (ANALYZE)** (Run maintenance) on the app database. If age
  keeps rising, find what's holding the xmin horizon: a [long transaction](#longtxn),
  an [idle-in-transaction](#idletxn) backend, or an inactive [replication slot](#slots).
  Clear that, then vacuum.
- **Prevent** — keep autovacuum healthy (don't disable it on hot tables), alert at
  ~150M so you have weeks of runway, and watch bloat.

<a id="bloat"></a>
## Table bloat (dead tuples)

**Means** dead tuples are a large share of the live+dead rows. **Matters** because
bloat wastes disk and slows scans, and unvacuumed dead rows feed wraparound.

- **Diagnose** — the Bloat health row (dead-tuple %). Identify the worst tables in
  `pg_stat_user_tables` (`n_dead_tup`).
- **Fix** — **VACUUM (ANALYZE)** reclaims dead tuples without locking. For a single
  severely bloated table, a per-table `VACUUM FULL` during a window rewrites it
  compactly — but it takes an `ACCESS EXCLUSIVE` lock, so the console deliberately
  doesn't offer it; run it by hand only when you can afford the lock.
- **Prevent** — tune autovacuum to be more aggressive on high-churn tables
  (`autovacuum_vacuum_scale_factor`), and avoid bulk delete-then-reinsert patterns.

<a id="longtxn"></a>
## Long-running transaction

**Means** a transaction has been open a long time. **Matters** because it holds
locks and pins the xmin horizon — blocking vacuum and feeding wraparound and bloat.

- **Diagnose** — `SELECT pid, now()-xact_start AS age, state, query FROM pg_stat_activity ORDER BY age DESC;`
- **Fix** — if it's a runaway, cancel it: `SELECT pg_cancel_backend(<pid>)` (or
  `pg_terminate_backend` if it won't yield). Otherwise fix the app that left it open.
- **Prevent** — set `statement_timeout` / `idle_in_transaction_session_timeout`; keep
  transactions short; don't hold one open across slow external calls.

<a id="idletxn"></a>
## Idle in transaction

**Means** backends are sitting in `idle in transaction` — a transaction opened and
never committed/rolled back. **Matters** for the same reasons as a long transaction:
held locks and a pinned xmin horizon.

- **Diagnose** — `SELECT pid, now()-state_change AS idle, query FROM pg_stat_activity WHERE state='idle in transaction';`
- **Fix** — fix the application (commit/rollback promptly; don't `BEGIN` then wait on
  I/O). Terminate the worst offenders if needed.
- **Prevent** — set `idle_in_transaction_session_timeout` as a backstop; review the
  app's transaction boundaries and connection pooling.

<a id="connage"></a>
## Old connections

**Means** the oldest client connection is long-lived. **Matters** because long-lived
connections accumulate memory (cached plans, temp buffers) and miss configuration
changes; they should be recycled.

- **Diagnose** — the Oldest connection health row; `SELECT max(now()-backend_start) FROM pg_stat_activity WHERE backend_type='client backend';`
- **Fix** — recycle them. With a **pooler**, set PgBouncer `server_lifetime` (and
  `server_idle_timeout`) so backends retire periodically. Without one, have the app's
  pool rotate connections.
- **Prevent** — always front the DB with a pooler in transaction mode and set a
  sane `server_lifetime`.

<a id="cachehit"></a>
## Low cache hit ratio

**Means** reads are missing the buffer cache and going to disk. **Matters** because
disk reads are orders of magnitude slower — latency and I/O climb.

- **Diagnose** — the Cache hit panel. A brief dip after a restart or a big scan is
  normal; a **sustained** dip is the signal.
- **Fix** — the working set likely exceeds `shared_buffers`: raise instance **memory**
  and `shared_buffers`. Or add an index so hot queries touch far fewer pages.
- **Prevent** — size memory for the working set; watch for missing indexes on new
  query patterns.

<a id="connsat"></a>
## Connection saturation

**Means** active backends are near `max_connections`. **Matters** because hitting the
cap refuses new connections (an outage from the app's view), and each backend costs
memory.

- **Diagnose** — the Connection saturation row (`active/max`). Check whether it's real
  concurrency or the app leaking connections.
- **Fix** — put a **pooler** in front (PgBouncer, transaction mode) so many clients
  share few backends. Prefer this over raising `max_connections`.
- **Prevent** — pool by default; cap the app's own pool sizes; alert at ~70%.

<a id="slots"></a>
## Inactive replication slot

**Means** a replication slot is inactive but still retaining WAL. **Matters** because
it pins WAL on the primary (filling the disk) and holds back vacuum via its xmin.

- **Diagnose** — the Replication slots row; `SELECT slot_name, active, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) FROM pg_replication_slots;`
- **Fix** — if the consumer is gone for good, drop the slot:
  `SELECT pg_drop_replication_slot('<name>')`. If it's a real replica, bring it back
  so it resumes consuming.
- **Prevent** — set `max_slot_wal_keep_size` so a stuck slot can't fill the disk;
  clean up slots when tearing down replicas/subscribers.

<a id="backups"></a>
## Backups

**Means** no completed backup, or backups are stale. **Matters** because without a
recent base backup + WAL there's **no point-in-time recovery** — a bad delete or a
corrupt page is unrecoverable.

- **Diagnose** — the Backups health row and the Recovery window. Check the object
  store is reachable and archiving is healthy ([WAL archive delay](#archive)).
- **Fix** — configure a barman object store and **Take base backup** (Operate). Add a
  ScheduledBackup for regular base backups.
- **Prevent** — schedule base backups, alert on freshness, and **test a restore**
  periodically — an untested backup isn't a backup.

<a id="repl-lag"></a>
## Replication lag

**Means** a replica is falling behind the primary. **Matters** because it widens the
data-loss window on failover and serves staler reads.

- **Diagnose** — the Replication lag panel; check the replica's CPU/IO and network,
  and whether a long query on the replica is delaying apply.
- **Fix** — relieve the replica (kill a heavy query, add resources); ensure the
  network between instances is healthy.
- **Prevent** — size replicas like the primary; keep `hot_standby_feedback` and query
  load in mind; alert on lag.

<a id="archive"></a>
## WAL archive delay

**Means** WAL isn't reaching the object store promptly. **Matters** because the PITR
window stops advancing and WAL piles up on the primary's disk.

- **Diagnose** — the archiving panels and the `ContinuousArchiving` condition. Check
  object-store reachability and credentials.
- **Fix** — restore connectivity/credentials to the object store; clear any archiver
  error. WAL resumes shipping once archiving succeeds.
- **Prevent** — monitor archive delay and archive failures; alert before the disk
  fills.

<a id="cpu"></a>
## High CPU

**Means** an instance is using more CPU than its goal. **Matters** because saturated
CPU means query latency and, at the limit, throttling.

- **Diagnose** — the CPU panel (needs cAdvisor metrics). Find the heavy queries in
  `pg_stat_activity` / `pg_stat_statements`.
- **Fix** — optimize or index the hot queries; raise the CPU request/limit; scale
  reads onto replicas.
- **Prevent** — set requests≈limits for predictable scheduling; watch slow queries.

<a id="memory"></a>
## High memory

**Means** an instance's working set is near its goal/limit. **Matters** because an
OOM kill restarts the instance (and can trigger a failover).

- **Diagnose** — the Memory panel. Consider `work_mem` × concurrent sorts/hashes,
  `shared_buffers`, and connection count (each backend costs memory — see
  [connection saturation](#connsat)).
- **Fix** — raise the memory request/limit, lower `work_mem` or connection counts, or
  pool connections.
- **Prevent** — size memory for peak concurrency; front with a pooler; set
  requests≈limits (Guaranteed QoS) so the pod isn't evicted first under pressure.
