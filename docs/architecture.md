# Architecture

What exists today and how it fits together. The target end-state is in
[plan.md](plan.md); the reasoning behind the choices is in [decisions.md](decisions.md).

## Big picture

K8osTester validates a Kubernetes configuration of a stateful technology against declarative
goals. The unit of work is an **experiment** — a directory holding the config manifests under
test plus an `experiment.yaml` describing load, faults, verification steps, and goals. The
framework runs it end-to-end and emits a per-goal pass/fail verdict.

```
experiments/<tech>/<name>/          k8ostester/ (framework)              results/<name>/<run-id>/
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

| Path | Role |
|---|---|
| `k8ostester/cli.py` | typer CLI (`k8ost`): `validate`, `run [--keep] [--context]`, `env check`, `env contexts` |
| `core/experiment.py` | pydantic models for `experiment.yaml` + loader; durations like `2m`/`30s` validated up front |
| `core/runner.py` | lifecycle orchestration; technology-blind — all specifics go through the driver |
| `core/k8s.py` | `ClusterClient`: kubeconfig-context-bound API access, namespace lifecycle, `kubectl apply` shell-out, workload readiness polling |
| `core/helm.py` | helm CLI wrapper (`repo add`, `upgrade --install --wait`, uninstall) for infra prerequisites |
| `core/capabilities.py` | cluster probe: nodes, storage classes, snapshot CRDs, operators (by CRD), helm — used to skip/flag goals a cluster can't exercise |
| `core/events.py` | append-only JSONL event log |
| `core/metrics.py` | append-only JSONL metric store + percentile helper (authoritative tier for goal verdicts) |
| `core/infra.py` | COMMON prerequisites, installed idempotently (D15): SeaweedFS + backup bucket, monitoring stack, chaos-mesh (D16); tech-specific pins live in each driver |
| `drivers/base.py` | `TechnologyDriver` contract: prereqs, deploy, readiness, topology, run_load, ensure_backup, verify |
| `drivers/__init__.py` | driver discovery (D15): nearest `driver.py` above the experiment dir, loaded dynamically; built-ins as fallback |
| `drivers/generic.py` | built-in deploy-anything driver; smoke tests now, seed of the test-your-own-app mode later |
| `technologies/postgres-cnpg/driver.py` | tech-owned CNPG driver: operator pin + install, Cluster CR lifecycle, topology, loadgen Job, integrity/backup/PITR verification |
| `technologies/postgres-cnpg/loadgen.py` | the journal load runner (ships via ConfigMap, D12): HikariCP-style pooled clients, bounded timeouts, `load.workers` Indexed pod pool; `load.image`/`load.pull_secret` for private clusters (prebuilt via `loadgen.Dockerfile`) |
| `technologies/postgres-cnpg/resources/pgbench-job.yaml` | the pgbench load runner (D17): `load.runner: pgbench` for industry-comparable tps on tuning experiments; per-transaction log parsed into the shared op-record stream |
| `technologies/<tech>/experiments/` | each technology's experiments live beside its driver (D15); directories (and `name:`) carry a numeric prefix (`01-cnpg-baseline`, …) so the progression reads in order — dashes, not underscores, because the name becomes part of the run namespace (DNS label) |
| `core/goals.py` | goal evaluators: rto (gap-based, D14), rpo (from integrity reconciliation), availability, latency percentiles, connect error rate, procedural checks |
| `workers/` | fault workers: `pod_kill` (grace 0), `process_kill` (kill -9 PID 1 — in-place container crash, scoped stand-in for node loss), `node_drain` (cordon + evict run pods, uncordon cleanup), `network_partition`/`network_loss`/`network_delay` (NetworkChaos CRs via Chaos Mesh, D16); targets resolve at injection time via driver topology |
| `core/report.py` | `k8ost report`: self-contained HTML comparing runs — goal matrix + overlaid per-second throughput/latency graphs with fault markers, crosshair tooltips, light/dark |
| `infra/monitoring/` | kube-prometheus-stack 87.10.1 (Grafana disabled, D7) + Perses 0.22.0 with provisioned Prometheus datasource; PodMonitor discovery is cluster-wide |
| `infra/seaweedfs/` | SeaweedFS manifests (S3 store for Barman backups/WAL, D6/D7) |
| `infra/chaos-mesh/` | Chaos Mesh 2.8.3 values (containerd socket, dashboard/DNS server off) — engine for the `network_*` workers (D16), installed via the `chaos-mesh` infra entry into `k8ost-chaos` |
| `experiments/` | experiment directories (the configs being validated) |
| `infra/` | shared cluster prerequisites (operator pins, SeaweedFS, monitoring) — phase 2+ |

## The driver contract

The runner never knows what Postgres is. A driver owns: prerequisite installation (operator,
object store), deploying the config, readiness, **topology** (role → pod, so faults can target
"the primary"), the load generator, **integrity checking** (reconcile the loadgen's acked-write
journal against the database — this is how RPO/data loss is measured), and backup/PITR verbs.
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

## Metrics: two tiers

1. **Verdict tier (authoritative):** per-operation records from the loadgen journal, stored in
   `results/<run>/`. Goals are evaluated only against this — Prometheus scrape resolution is too
   coarse for second-level RTO, and RPO requires journal-vs-database reconciliation.
2. **Observability tier (installed via the `monitoring` infra entry):** kube-prometheus-stack
   in `k8ost-monitoring` — Prometheus discovers *all* Pod/ServiceMonitors, so a CNPG cluster
   with `monitoring.enablePodMonitor: true` is scraped automatically and its metrics outlive
   the run namespace (7d retention). Dashboards: **Perses** (Apache 2.0 — Grafana is AGPL,
   excluded by D7) with a provisioned default Prometheus datasource; reach it with
   `kubectl port-forward -n k8ost-monitoring svc/perses 8080:8080`.

Cross-run comparison graphs do NOT come from Prometheus — `k8ost report` builds them from the
runs' `metrics.jsonl` (the verdict tier), so reports work offline and after cluster teardown.
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
