# Remote-control console

A laptop-side web control plane for live CloudNativePG clusters. It holds your
kubeconfig, **discovers** what's really running, and lets you **operate**
(backup, rotate credentials), **break-glass** (PITR restore, minor upgrade,
chaos faults), and **design** (generate a starter manifest) — all from one page.
It mutates real clusters, so it's an owned, interactive UI, not a dashboard;
read-only telemetry can still live in Grafana beside it.

## Run it

```
k8ost-console                                   # pick context + cluster in the UI
k8ost-console --context prod                    # lock the picker to one context
k8ost-console --context prod --namespace prod-east --cluster orders   # pre-select
k8ost-console --target 16.6                     # offer a specific image as an upgrade
k8ost-console --grafana http://localhost:3000   # deep-link the metrics chips to Grafana
# → open http://127.0.0.1:8700   (--host/--port to change the bind; localhost by default)
```

Launch it via the workspace with `uv run --directory pg k8ost-console …`.

Nothing about the target is hardcoded — the cluster name, namespace, roles,
services, backup config, poolers and topology are all discovered.

## The core idea: capability = precondition over discovered state

A control is **not** tracked as used/unused. Each action declares a **precondition
evaluated against the live snapshot** and is enabled *iff* it holds now; a second
**`available`** predicate decides whether the action is offered at all (so a
control that needs extra config isn't a permanently-dead tile). "Disable after
use" and "multi-use" fall out of one rule.

| Action | Where | Available when | Enabled when |
| --- | --- | --- | --- |
| Take base backup | Operate · routine | always | Ready · backup configured · not busy |
| Rotate credentials | Operate · routine | always | Ready · two login roles · not busy |
| Expand storage | Operate · routine | always | Ready · not busy (modal re-checks the storage class allows expansion) |
| Run maintenance | Operate · routine | always | Ready · not busy — VACUUM (ANALYZE) / ANALYZE / CHECKPOINT |
| Restore (PITR) | Operate · break-glass | always | ≥1 completed backup · WAL window · not busy |
| Minor upgrade | Operate · break-glass | always (asks for the image on press) | Ready · not upgrading · not busy |
| Inject fault (kill / partition a pod) | Operate · break-glass | always | target pod exists · no fault in flight |
| Deploy a built manifest | Build | a manifest is generated | RBAC allows create (see `console-lab.yaml`) |

Actions **degrade safely on cluster-specific setups** rather than silently
misbehaving: Backup/Restore gate on a configured object store + WAL window,
Rotate on two login roles, and Expand storage re-checks the storage class's
`allowVolumeExpansion` on open (blocking the button if it can't grow, so the
patch can't become a silent no-op). The fault picker warns that Partition only
isolates on a CNI that enforces NetworkPolicy.

`enabled` is computed **server-side** from the snapshot (a stale browser can't
fire a disabled action) and also sent down for rendering. Restore proves the
model: it lights up the moment the snapshot shows a completed backup + a WAL
window, and greys out again if the recovery window lapses — no per-control
used/unused bookkeeping, just the precondition re-evaluated each tick.

Mutating ops also require `not busy` — an exclusivity lock so you can't stack e.g.
a PITR restore and an upgrade. Chaos faults deliberately skip the lock (they stay
available during an operation, with an ack).

## Selecting a cluster

Two header dropdowns. **Context** enumerates the kubeconfig's contexts (read
directly from the file, tolerating a missing `current-context`). Picking one
lists **every CNPG `Cluster` CR** the credentials can see via
`list_cluster_custom_object` (falling back to a namespace on RBAC 403), each with
a health dot, version and ready/total. The chosen `(context, namespace, name)`
drives everything; switching re-selects live. The console mutates clusters, so
`--context`/`--namespace` also scope the blast radius.

## Two views

A header selector switches between **Operate** and **Build**:

- **Operate** — the live cluster: the SCADA topology, the conditions strip, the
  recovery window, a **Health & runbooks** panel (below), and the routine actions
  (backup, rotate, expand storage, run maintenance). The destructive **break-glass**
  actions (PITR restore, minor upgrade, inject a fault) sit below in a cordoned
  danger zone that stays disabled until you **arm** it — so you can't fire one by
  reflex or without realizing you're in a dangerous mode.
- **Build** — the guided manifest builder + observability, and a one-click **Deploy**
  of what you built into the selected namespace. No cluster needed to design.

Every mutating action confirms before it runs.

## Health & runbooks

An ORR checklist evaluated live from the snapshot. A self-contained health query
(one psql on the primary, so it works even without a metrics pipeline) returns
transaction-ID age (wraparound), longest transaction, oldest connection,
idle-in-transaction, dead-tuple % (bloat) and cache-hit %. The panel scores those
plus disk / connection saturation / replication slots / backup freshness against
built-in thresholds. At-risk signals surface as WARN/CRIT rows with an inline
remediation (Run VACUUM, Expand storage, Take backup) and a **Runbook** link to
embedded guidance; healthy checks collapse into one green summary line. The
longer-form runbooks live in [runbooks.md](runbooks.md), which is also the target
of each alert's `runbook_url`.

## Discovered state (one snapshot)

A background timer reads the selected cluster and produces the snapshot the whole
UI renders from. To keep cluster load fixed regardless of viewers, discovery runs
**once on a shared timer** (not per SSE connection), split into two tiers: a 2s
**fast** tier (topology, roles, phase, replication, archiver) and a ~20s **heavy**
tier (disk `df`, connections, replication slots, data size, services, and the ORR
`health` signals) merged in.

Key fields: `cluster`/`namespace`, `ready`/`phase`/`conditions`,
`primary`/`replicas`/`zones`, `instances[]` (role, node, zone, healthy,
`sync_state`, `lag_bytes`), `poolers[]`, `version`/`target`, `storage_size`,
`sync_policy`, `object_store`, `archived_wal` (archived, last, failed,
`last_time`, current, `lag_segments`), `archiving` (the CNPG ContinuousArchiving
condition), `schedules[]` + `retention`, `backup_configured` + `backups_completed`
+ `backups[]` + `recoverability_point` + `pitr_window`, `upgrading`, `database` +
`login_roles` + `blue_green`, `credentials` (active role + rotated-at from the
cluster annotation), `disk{}`, `data_size`, `connections`, `slots[]`, `services[]`,
`health` (the ORR query: xid age, longest txn, oldest connection, idle-in-txn,
dead-tuple %, cache-hit %), plus `busy`/`busy_reason` and `fault_in_flight`.

## SCADA topology + health signals

`client → poolers → primary → replicas → object store → backup policy`, adapting
to what exists (no poolers / no backups ⇒ those nodes vanish). Health encoded in
form, not just number:

- **Sync vs async** — each replica shows a SYNC / ASYNC / STANDBY badge from
  `pg_stat_replication.sync_state`, plus replication lag. The `sync_policy` chip
  shows quorum/priority (e.g. `quorum · any 1`).
- **Disk headroom** — per instance `disk N%` (amber ≥75, red ≥90) from `df`.
- **Connection saturation** — `conns active/max` chip (amber ≥70%, red ≥85%).
- **Replication slots** — chip; an inactive slot (pins WAL, fills disk) turns it
  amber with the WAL held.
- **Node placement** — each instance's k8s node + zone (reason about a zone loss).
- **WAL archiving health** — the object-store node shows LIVE / `N behind` /
  STALLED (driven by the archive lag + the ContinuousArchiving condition) and the
  last-archive age. The missing half of the recovery story.
- **Backup policy + freshness** — the ScheduledBackup cron (humanized) + retention,
  and "last backup Xh ago" flagged due / OVERDUE vs the schedule's period.

Click the client / primary / a pooler / a replica to open **Connect** (below).

## Recovery window + PITR

When a base backup has completed, a recovery timeline shows the WAL-covered band
and each backup tick (with time + relative age on hover). **Restore
(Break-glass)** opens a modal: **Latest** (time shown) or **Point in time** — a
slider bound to the recoverable window that soft-snaps to backup ticks. As you
settle, it queries the primary for the **exact** number of WAL segments generated
since the chosen base backup (LSN-diff, timeline-independent), debounced. Restore
bootstraps a uniquely-named recovery cluster from the object store; the live
cluster is untouched.

## Connect & credentials

The Connect sheet shows the **in-cluster endpoints** (`<name>-rw/-ro/-r` and any
poolers, all :5432), an **External access** section (any LoadBalancer/NodePort
Service discovered, else a one-line "in-cluster only" note + a `port-forward`
command), the **app database + owner**, the **login roles and which Secret** each
password comes from, a copyable `kubectl get secret` to fetch a password, and
ready-to-paste **URI / psql / JDBC** strings (pointed at the rw pooler when there
is one).

## Credential rotation (generic)

Blue/green with no auth gap: refresh the **idle** login role's password + Secret,
then record the new active role as an annotation on the Cluster
(`k8ostester.io/active-role`). It uses the cluster's own managed roles — no app
ConfigMap/Deployment — so it works on any cluster with two login roles. The app
consumes the active role's Secret however it's wired. (The testbed's `flow.py`
does the full app-inclusive rotation and stamps the same annotation, so the two
agree.)

## The Builder

Generates a Cluster (+ optional Pooler + ScheduledBackup + app-role Secrets) from
form choices, assembled from `resources/*.tmpl.yaml` with `${VAR}` substitution —
no manifest strings in Python. **Presets** fill the form for common cases
(Reference HA / Read-heavy / Dev-minimal / Durable). **App roles for rotation**
emits two login roles (`app_a`/`app_b`) that inherit the `app` owner, each from
its own Secret (CHANGE-ME placeholders) — the prerequisite Rotate switches
between. A note explains storage is grow-only (online PVC expansion needs
`allowVolumeExpansion`, never shrinks).

**Observability** is built from the same form. Turning on a scrape (the
Prometheus PodMonitor, or an **OTEL endpoint** that also emits a collector)
unlocks an adaptive **Grafana dashboard** (`dashboard.py`, panels adapt to the
config) and a **goals** block. The dashboard covers resources (CPU/memory/disk),
throughput, and the operational/ORR signals — transaction-ID age, longest/stuck
transaction, connection health, cache-hit, checkpoints, restarts. Each goal
(replication lag, connections, WAL-archive delay, CPU, memory, disk, txn-ID age,
longest txn, connection age) becomes both a red **waterline** on the matching
panel and a **PrometheusRule** alert carrying a `runbook_url` — one `goals.py`
source of truth for both. Connection age and idle-in-transaction aren't in CNPG's
default metrics, so enabling monitoring also attaches a **custom-queries ConfigMap**
that exposes them. The output switch shows the manifest (YAML) or the dashboard
(JSON); **Copy**, **Download**, or **Deploy** it.

**Deploy** applies every doc in the generated manifest into the selected
namespace via the dynamic client, reporting created / skipped / failed. In-cluster
this needs the additive `pg/deploy/console-lab.yaml` RBAC (create rights); without
it the console reports the 403 rather than pretending it worked.

## Architecture & files

```
kubeconfig ─▶ Console (server.py) ─ shared 2s + 20s timers ─▶ snapshot cache ─SSE─▶ browser (console.html)
                 ├─ discover.py  : cluster + pods + psql/df execs → snapshot
                 ├─ control.py   : CNPG actions (precondition + available)  [kernel/control.py: the model]
                 ├─ execute.py   : gate → dispatch (chaos primitives / ops)
                 ├─ ops.py       : rotate / upgrade / restore / expand / maintenance
                 ├─ builder.py   : options → manifest (resources/*.tmpl.yaml)
                 ├─ dashboard.py : options → Grafana dashboard JSON
                 ├─ goals.py     : goals → waterlines + alert rules (+ runbook anchors)
                 └─ registry.py  : image tag discovery + pull checks (upgrades)
```

- **kernel** supplies the generic capability model (`control.py`), the k8s client,
  and the chaos primitives.
- **pg** supplies the CNPG actions, discovery, executor, ops, builder, dashboard,
  registry, the stdlib `ThreadingHTTPServer`, and the single-file SPA `console.html`.
- HTTP surface — GET: `/` (SPA), `/api/stream` (SSE), `/api/contexts`,
  `/api/clusters?context=`, `/api/secret?name=`, `/api/image-tags?image=`,
  `/api/image-check?image=`, `/api/storage-expandable`. POST: `/api/action`,
  `/api/select`, `/api/manifest`, `/api/dashboard`, `/api/deploy`, `/api/wal-count`.

## Deploy in-cluster (control plane)

The same image runs laptop-side (kubeconfig) or **in the cluster** as a control
plane. In a pod it uses the mounted **ServiceAccount** (`config.load_incluster_config`)
and execs into pods over the **API stream** (no kubectl needed). Deploy it:

```
kubectl -n <ns> apply -f pg/deploy/console.yaml          # SA + Role + Deployment + ClusterIP Service
kubectl -n <ns> port-forward svc/k8ost-console 8700:8700 # port-forward IS the auth gate (needs RBAC)
```

- **RBAC is the blast radius.** The console can do exactly what `pg/deploy/console.yaml`'s
  Role grants — a readable, revocable YAML boundary. Alongside the CNPG/pod/secret
  grants it also reads `storageclasses` + `persistentvolumeclaims` (the Expand-storage
  expandability pre-check) and creates/deletes `networkpolicies` (the partition fault).
  `pg/deploy/rbac-clusterwide.yaml` is the fleet-wide (ClusterRole) variant, which also
  reads nodes for zone info. Note `pods/exec` needs **both `get` and `create`** (the
  websocket-stream exec is a GET).
- **Build → Deploy is opt-in.** `pg/deploy/console.yaml` grants only operate/read;
  applying the additive `pg/deploy/console-lab.yaml` adds the create rights the
  Builder's Deploy needs. Leave it off for a pure operator console.
- **Port-forward by default.** The Service is ClusterIP; the default way in is
  `port-forward`, which already requires Kubernetes RBAC — so k8s authn/authz is
  the login.
- **Audit** comes largely from the cluster's API audit log (every mutation is
  attributed to the ServiceAccount) plus the console's own Activity record.

## Exposing it on a DNS name

To reach it on a hostname you go through your **gateway controller**: DNS name →
gateway (TLS termination + auth) → the ClusterIP Service (plain HTTP) → the pod. The
Helm chart creates just the route that hands your hostname to the Service — a Gateway
API `HTTPRoute` (`gatewayRoute.*`, the vendor-neutral option) or an `Ingress`
(`ingress.*`). TLS and the certificate live on the gateway; the console speaks plain
HTTP behind it, and its SSE stream works as long as the gateway doesn't buffer.

**Auth is the gateway's job.** The console has **no login of its own**, so a DNS name
is a mutating control plane open to anyone who can reach it. Enforce authentication on
the gateway controller — its auth/external-auth extension, or an authenticating proxy
(e.g. an OIDC forward-auth proxy) in front. The chart **refuses to render an
unauthenticated Ingress** unless you explicitly set `ingress.insecureNoAuth=true`. See
the [chart README](../pg/deploy/helm/k8ost-console/README.md#exposing-it-on-a-dns-name-and-auth).

Still open on the exposure track: RBAC-aware action gating (grey out what the SA can't
do) and typed confirmations for the most destructive break-glass ops.
