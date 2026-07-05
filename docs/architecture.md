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
| `drivers/base.py` | `TechnologyDriver` contract: prereqs, deploy, readiness, topology, loadgen, integrity, backup ops |
| `drivers/generic.py` | deploy-anything driver; smoke tests now, seed of the test-your-own-app mode later |
| `experiments/` | experiment directories (the configs being validated) |
| `infra/` | shared cluster prerequisites (operator pins, SeaweedFS, monitoring) — phase 2+ |

## The driver contract

The runner never knows what Postgres is. A driver owns: prerequisite installation (operator,
object store), deploying the config, readiness, **topology** (role → pod, so faults can target
"the primary"), the load generator, **integrity checking** (reconcile the loadgen's acked-write
journal against the database — this is how RPO/data loss is measured), and backup/PITR verbs.
New technology = new folder under `drivers/` implementing the same contract; runner, workers,
goals, metrics, and reports are shared.

## Metrics: two tiers

1. **Verdict tier (authoritative):** per-operation records from the loadgen journal, stored in
   `results/<run>/`. Goals are evaluated only against this — Prometheus scrape resolution is too
   coarse for second-level RTO, and RPO requires journal-vs-database reconciliation.
2. **Observability tier:** Prometheus stack in-cluster for live dashboards and system metrics
   (phase 4; dashboard tool pending the license decision — Grafana is AGPL).

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
