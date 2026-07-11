# Architecture

What exists today and how it fits together. The target end-state is in
[plan.md](plan.md); the reasoning behind the choices is in [decisions.md](decisions.md).

## Big picture

K8osTester validates a Kubernetes configuration of a stateful technology against declarative
goals. The unit of work is an **experiment** — a directory holding the config manifests under
test plus an `experiment.yaml` describing load, faults, verification steps, and goals. The
framework runs it end-to-end and emits a per-goal pass/fail verdict.

```
experiments/<tech>/<name>/          k8ostester-core/ (framework)         results/<name>/<run-id>/
┌─────────────────────┐            ┌──────────────────────────┐         ┌──────────────────┐
│ experiment.yaml     │──validate──▶ core/experiment.py       │         │ experiment.json  │
│ manifests/*.yaml    │            │ core/runner.py ──────────┼─writes──▶ events.jsonl     │
└─────────────────────┘            │   │        │             │         │ summary.json     │
                                   │   ▼        ▼             │         │ metrics.jsonl(*) │
                                   │ drivers/  workers/(*)    │         └──────────────────┘
                                   │   │                      │
                                   │   ▼                      │              (*) = phase 2/3
                                   │ core/k8s.py, core/helm.py│
                                   └───────────┼──────────────┘
                                               ▼
                                     any kubeconfig context
                                     (local docker-desktop or remote)
```

## Run lifecycle (implemented in `core/runner.py`)

```
load spec → capability check → install prereqs (idempotent, cluster-level)
→ create run namespace → apply manifests → wait workloads ready
→ [phase 2: load + verification] → [phase 3: faults + goal evaluation]
→ teardown (skipped with --keep) → summary.json
```

- **Namespace-per-run isolation:** each run creates `<exp-name>-<suffix>` labeled
  `k8ostester.io/run=<run-id>`; teardown is a single namespace delete. Cluster-level
  prerequisites (operators, object store) are shared and never torn down per run.
- **Every notable moment** is appended to `events.jsonl` (type, message, wall time, offset from
  run start). Fault/recovery timestamps for goal evaluation come from here; reports will render
  it as the timeline.
- Teardown failures never mask the original run error.

## Components

Framework paths are relative to the package root `k8ostester-core/src/k8ostester/`.

| Path | Role |
|---|---|
| `cli/` | typer CLI (`k8ost`), split by command group: `run.py` (validate, run + experiment picker), `report.py` (report, runs), `env.py` (check, contexts); `app.py` holds the bare app; `live.py` the in-terminal live run panel; `tui.py` the full-screen single-page dashboard (metrics + topology + events together) — the default on a terminal, `--view live|plain` for the inline panel or log lines; `session.py` the interactive lab (`k8ost session`): the same dashboard plus a controls bar — scale load pods, kill primary/replica, partition |
| `core/experiment.py` | pydantic models for `experiment.yaml` + loader; durations like `2m`/`30s` validated up front |
| `core/runner.py` | lifecycle orchestration; technology-blind — all specifics go through the driver |
| `core/session.py` | interactive sessions (`k8ost session`): deploy the config, load as a scalable Deployment of self-contained loadgen pods (pod count = the load knob), faults on demand via a command queue; no timeline, no verdict — the human is the experiment plan |
| `core/k8s.py` | `ClusterClient`: kubeconfig-context-bound API access, namespace lifecycle, `kubectl apply` shell-out, workload readiness polling |
| `core/helm.py` | helm CLI wrapper (`repo add`, `upgrade --install --wait`, uninstall) for infra prerequisites |
| `core/capabilities.py` | cluster probe: nodes, storage classes, snapshot CRDs, operators (by CRD), helm — used to skip/flag goals a cluster can't exercise |
| `core/events.py` | append-only JSONL event log |
| `core/metrics.py` | append-only JSONL metric store + percentile helper (authoritative tier for goal verdicts) |
| `core/infra.py` | COMMON prerequisites, installed idempotently (D15): SeaweedFS + backup bucket, chaos-mesh (D16); tech-specific pins live in each driver |
| `drivers/base.py` | `TechnologyDriver` contract: prereqs, deploy, readiness, topology, run_load, ensure_backup, verify |
| `drivers/__init__.py` | driver discovery (D15): nearest `driver.py` above the experiment dir, loaded dynamically; built-ins as fallback |
| `drivers/generic.py` | built-in deploy-anything driver; smoke tests now, seed of the test-your-own-app mode later |
| `technologies/postgres_cnpg/driver.py` | built-in CNPG driver (D20): operator pin + install, Cluster CR lifecycle, topology, loadgen Job, integrity/backup/PITR verification |
| `technologies/postgres_cnpg/loadgen.py` | the journal load runner (ships via ConfigMap, D12): HikariCP-style pooled clients, bounded timeouts, `load.workers` Indexed pod pool; `load.image`/`load.pull_secret` for private clusters (prebuilt via `loadgen.Dockerfile`) |
| `technologies/postgres_cnpg/resources/` | pgbench runner Job (D17), loadgen Job, backup + PITR templates |
| `experiments/<tech>/` (repo top level) | the framework's example/regression experiment suite — outside the platform project, exactly the shape of an end user's config repo (D20); numeric prefixes order the progression; a custom `driver.py` beside experiments overrides the built-in (D15) |
| `k8ostester-core/tests/` | the framework test suite, mirroring the source layout |
| `core/goals.py` | goal evaluators: rto (gap-based, D14), rpo (from integrity reconciliation), availability, latency percentiles, connect error rate, procedural checks |
| `workers/` | fault workers: `pod_kill` (grace 0), `process_kill` (kill -9 PID 1 — in-place container crash, scoped stand-in for node loss), `node_drain` (cordon + evict run pods, uncordon cleanup), `network_partition`/`network_loss`/`network_delay` (NetworkChaos CRs via Chaos Mesh, D16); targets resolve at injection time via driver topology |
| `core/report.py` | `k8ost report`: self-contained HTML comparing runs — goal matrix + overlaid per-second throughput/latency graphs with fault markers, crosshair tooltips, light/dark |
| `resources/infra/` | packaged common-infra defaults: SeaweedFS manifests (D6/D7), chaos-mesh 2.8.3 values (D16) — overridable via `infra/...` under the CWD (D20) |

## The driver contract

The runner never knows what Postgres is. A driver owns: prerequisite installation (operator,
object store), deploying the config, readiness, **topology** (role → pod, so faults can target
"the primary") plus **topology_graph** (a nodes/edges component graph for the live view — the
framework owns the schema and rendering, the driver owns discovery; CNPG combines the Cluster CR
with `pg_stat_replication` on the primary for per-replica sync/async/quorum state and Pooler CR
detection for the client path), the load generator, **integrity checking** (reconcile the
loadgen's acked-write journal against the database — this is how RPO/data loss is measured),
and backup/PITR verbs.
New technology = new folder under `drivers/` implementing the same contract; runner, workers,
goals, metrics, and reports are shared.

## The CNPG run, concretely (phase 2, working)

1. **Prereqs** (idempotent): CNPG operator via pinned helm chart; SeaweedFS + `backups` bucket
   in `k8ost-infra`.
2. **Deploy**: the experiment's manifests are applied with `${K8OST_NAMESPACE}` substituted, so
   each run's Barman catalog path (`s3://backups/<run-namespace>`) is unique. Readiness = the
   Cluster CR reports "healthy" with all instances ready.
3. **Base backup before load** (when `verify` includes backup/pitr): a Backup CR, waited to
   `completed` — PITR replays WAL forward from it through the whole run.
4. **Load**: `loadgen.py` runs as a Job (ConfigMap + stock python image, D12), executing the
   pre-declared phases; every op is a JSON line on stdout. After completion the driver pulls pod
   logs, writes `loadgen.log` (raw), `metrics.jsonl` (all records), `journal.jsonl` (acked
   writes) into the run dir.
5. **Verify**:
   - *integrity* — every acked write's id+checksum must be in the database;
   - *backup* — the Backup CR completed (LSN range recorded);
   - *pitr* — a second cluster (`pg-pitr`) is bootstrapped from the object store with
     `targetTime` = middle of the load plan's zero-rate pause phase (D13), then the restored row
     set must equal the pre-pause acked set exactly.
6. Any verify failure → run status `failed` (vs `error` for framework/infra problems).

## Metrics: app-perspective, journal-only (D19)

Every metric — verdicts and report graphs alike — comes from the loadgen journal's per-operation
records in `results/<run>/`. It's the only source precise enough for second-level RTO, the only
one that can measure RPO (journal-vs-database reconciliation), and it measures what the
application actually experienced. There is no monitoring stack to install (D19 removed it):
reports work offline and after cluster teardown, and the same pipeline works unchanged on
private clusters because journal retrieval rides the Kubernetes API (pod logs, D12).
Runs are grouped via `group:` in experiment.yaml or `k8ost run --group`, recorded in
`summary.json`; `k8ost report --group X` collects and graphs the whole group.

## Cluster targeting

Everything reaches the cluster through `ClusterClient(context)` — no global kube state. An
experiment pins its context in `experiment.yaml` (`cluster.context`); `k8ost run --context`
overrides it. Remote clusters work identically; `capabilities.probe()` reports what the target
can and cannot exercise (e.g. worker count for node faults, snapshot support).

## Current dev environment

Docker Desktop's kind-mode Kubernetes: 1 control plane + 4 workers (`desktop-worker[1-4]`),
v1.36, arm64, `local-path` storage (no volume snapshots → object-store backups are the backup
path). Node containers are inside the Docker Desktop VM — not reachable via host `docker`, which
is why node faults use `kubectl debug`/drain.
