# K8osTester — Plan (north star)

> Companion docs: [architecture.md](architecture.md) (what exists and how it fits together),
> [decisions.md](decisions.md) (why it is this way). Update **Status** below as phases land.

## Status

| Phase | State | Notes |
|---|---|---|
| 0 — environment | ✅ done (2026-07-05) | helm v4 installed; `k8ost env check` verified against the 4-worker docker-desktop cluster |
| 1 — skeleton | ✅ done (2026-07-05) | spec models, runner lifecycle, generic driver, events/metrics stores, helm/kubectl wrappers; nginx-smoke run green end-to-end |
| 2 — CNPG happy path | ✅ done (2026-07-05) | cnpg-baseline green: CNPG 1.29.1 + SeaweedFS, 2938-op load, integrity + backup verified, PITR restored the 601 pre-pause rows exactly |
| 3 — faults + goals | ✅ done (2026-07-05) | D2 proof landed: cnpg-single FAILED (RTO 10.1s, 12 acked writes lost) vs cnpg-ha-3node PASSED (RTO 1.7s, RPO 0) under the identical primary kill |
| 4 — observability & reporting | ✅ done (2026-07-06) | run groups + `k8ost report` (self-contained HTML comparison graphs); kube-prometheus-stack (Grafana excluded) + Perses with provisioned datasource; CNPG PodMonitor scrape verified |
| 5 — scale-out | ⬜ next | pooling comparison (cnpg-pgbouncer), Chaos Mesh adapter, Kafka driver, CI mode |

---

A framework for validating Kubernetes configurations of stateful technologies (Postgres, Kafka,
Elasticsearch, MongoDB, …) against explicit resilience and performance goals. An **experiment** =
a config under test + a load profile + a fault scenario + a set of goals. The framework runs the
experiment end-to-end and produces a pass/fail verdict per goal, plus a report.

First concrete target: **CloudNativePG (CNPG)** with backups/snapshots and PITR, validated under
pod/node failures with recovery-time and data-loss goals.

---

## 1. Core concepts (the domain model)

| Concept | What it is |
|---|---|
| **Technology driver** | Plugin that knows how to install an operator, deploy a config, talk to the workload (e.g. psycopg for PG), verify data integrity, and drive backup/restore verbs. One per technology. |
| **Experiment** | A directory: the config manifests under test + `experiment.yaml` (load profile, fault timeline, goals). Versioned in git — this is the artifact you iterate on. |
| **Load generator** | Per-technology client that applies load (possibly variable phases) and emits raw measurements: per-op latency, success/failure, and *acked writes* (for data-loss accounting). |
| **K8osWorker (fault worker)** | An action injected during the run: kill primary pod, drain/kill node, delete PVC, network partition, trigger failover, etc. Scheduled on a timeline. |
| **Goals** | Declarative SLOs evaluated against collected metrics: RTO (recovery time), RPO (data loss), availability %, latency percentiles, and *procedural* goals ("PITR restore to time T succeeds and is consistent"). |
| **Run / Report** | One execution of an experiment → `results/<experiment>/<run-id>/` with raw metrics, events timeline, and a verdict report (md + json). |

### Experiment lifecycle (the runner's state machine)

```
provision → wait-ready → baseline load → fault timeline (load continues) →
recovery observation → verification (integrity, backup/PITR checks) →
goal evaluation → report → teardown (optional --keep)
```

---

## 2. Repository layout

```
K8osTester/
├── plan.md
├── pyproject.toml
├── k8ostester/                    # the framework (installable package, CLI entry point `k8ost`)
│   ├── cli.py                     # typer CLI: run, list, report, compare, teardown
│   ├── core/
│   │   ├── experiment.py          # load & validate experiment.yaml (pydantic models)
│   │   ├── runner.py              # lifecycle orchestration
│   │   ├── timeline.py            # schedules faults/phases relative to run start
│   │   ├── metrics.py             # metric store (JSONL/SQLite per run) + derived stats
│   │   ├── goals.py               # goal evaluators (RTO, RPO, availability, latency, procedural)
│   │   ├── k8s.py                 # k8s client wrapper (kubeconfig context aware), helm wrapper
│   │   ├── events.py              # run event log (fault injected, pod ready, leader changed…)
│   │   └── report.py              # verdict + markdown/HTML report generation
│   ├── workers/                   # fault workers (technology-agnostic)
│   │   ├── base.py
│   │   ├── pod_kill.py            # delete pod(s) by selector / role (e.g. CNPG primary)
│   │   ├── node_fail.py           # drain / docker-stop a kind node (multi-node clusters only)
│   │   ├── pvc_delete.py
│   │   └── network.py             # (later) via Chaos Mesh if installed
│   ├── drivers/
│   │   └── postgres_cnpg/
│   │       ├── driver.py          # install operator, deploy Cluster CR, readiness, role discovery
│   │       ├── loadgen.py         # psycopg-based read/write load with acked-write journal
│   │       ├── integrity.py       # data consistency check (checksum/sequence table)
│   │       └── backup.py          # trigger backup/snapshot, run PITR restore, verify target state
│   └── loadgen/
│       └── base.py                # load phases, rate control, measurement emission
├── experiments/
│   └── postgres/
│       ├── cnpg-baseline/         # experiment 1: single instance, barman backups to SeaweedFS
│       │   ├── experiment.yaml
│       │   └── manifests/         # the config under test: Cluster CR, storage, backup config
│       ├── cnpg-ha-3node/         # experiment 2: 3 replicas, sync replication, failover goals
│       ├── cnpg-pgbouncer/        # experiment 3: + Pooler CR (PgBouncer), connection-churn load
│       └── ...
├── infra/                         # shared cluster prerequisites (not per-experiment)
│   ├── seaweedfs/                 # S3 object store (Apache 2.0) for barman WAL archiving + base backups
│   ├── kind/                      # kind cluster config (3 workers) + snapshot CSI setup
│   └── operators/                 # operator install pins (CNPG version, helm values)
└── results/                       # gitignored; one dir per run
```

**Framework vs experiment separation:** the framework never hardcodes a config. Everything you
want to validate lives in `experiments/<tech>/<name>/` — copy a directory, tweak the manifests
or goals, re-run. `k8ost compare` diffs verdicts across runs/experiments.

---

## 3. The experiment spec

`experiment.yaml` (pydantic-validated) — example for the first CNPG experiment:

```yaml
name: cnpg-baseline
technology: postgres-cnpg
cluster:
  context: docker-desktop      # any kubeconfig context → local or remote cluster
  namespace: exp-cnpg-baseline # each run gets an isolated namespace

infra:                          # prerequisites, installed if missing (idempotent)
  - operator: cnpg              # pinned version from infra/operators/
  - seaweedfs                   # S3 backup target (permissive alternative to MinIO/Garage)

config:                         # the thing under test
  manifests: ./manifests       # applied in order; contains the CNPG Cluster CR

load:
  endpoint: auto                    # which Service to hit: auto (driver default, e.g. cnpg -rw),
                                    # or explicit name — e.g. the PgBouncer Pooler service
  clients: {count: 20, mode: persistent}   # or mode: churn (connect/op) — pooling experiments
  phases:
    - {duration: 2m, rate: 50/s, mix: {read: 0.7, write: 0.3}}   # baseline
    - {duration: 5m, rate: 200/s, mix: {read: 0.5, write: 0.5}}  # pressure + faults land here
    - {duration: 1m, clients: {count: 300, mode: churn}}          # connection storm

faults:
  - at: 3m
    worker: pod_kill
    target: {role: primary}         # driver resolves "primary" to the actual pod
  - at: 5m
    worker: node_fail               # skipped with a warning on single-node clusters
    target: {node_of: primary}

verify:                             # procedural checks after load/faults
  - integrity                       # all acked writes present, checksums match
  - backup: {type: barman}          # a base backup completed during the run
  - pitr: {target: "fault[0].at - 10s"}  # restore to just before the first fault, verify rows

goals:
  - {metric: rto, max: 30s}         # time from fault to first successful write
  - {metric: rpo, max: 0}           # zero acked-but-lost writes
  - {metric: availability, min: 99.0%, window: whole-run}
  - {metric: write_latency_p99, max: 250ms, window: steady-state}
  - {metric: connect_latency_p99, max: 50ms, window: storm}   # where pooling wins/loses
  - {metric: connect_error_rate, max: 0.1%, window: storm}
  - {check: pitr, must: pass}
```

### Architecture-variant experiments (e.g. connection pooling)

Comparing "app → PG directly" vs "app → PgBouncer → PG" is just two experiment directories with
identical goals: `cnpg-direct/` and `cnpg-pgbouncer/` (the latter adds a CNPG **`Pooler` CR** —
CNPG manages PgBouncer natively, so it's one extra manifest, and `load.endpoint` points at the
pooler Service). `k8ost compare` diffs the verdicts and metrics. Three spec features exist
specifically to make such comparisons meaningful:

- **`load.clients`** models connection behavior (count, persistent vs per-op churn, storm
  phases) — pooling differences are invisible to a pure op-rate load.
- **Connection metrics**: the loadgen journals connection-establishment latency and connection
  errors, not just query latency.
- **The pooler is a fault target too** (`target: {role: pooler}`): does killing it mask or
  amplify an outage? Does PgBouncer's pause/resume improve measured RTO during failover?

The same pattern covers future variants: sync vs async replication, different storage classes,
resource limits, PG tuning parameters — any "same goals, different config" question.

### How the key metrics are actually measured

- **RPO / data loss:** the load generator journals every *acknowledged* write (id + payload
  checksum) locally. After recovery/restore, `integrity.py` reconciles the journal against the
  database. Missing acked rows = data loss. This is the only trustworthy way to measure RPO.
- **RTO:** timestamp of fault injection (event log) → first subsequent successful write.
- **Availability:** fraction of load-gen ops that succeeded, computed per-second so the report
  can show the outage window visually.
- **PITR:** pick a target time between two known writes; restore a new Cluster CR from the
  backup + WAL to that time; assert row N exists and row N+1 does not.

---

## 4. Technology driver interface

```python
class TechnologyDriver(Protocol):
    def install_prereqs(self, infra: list[InfraSpec]) -> None      # operator, SeaweedFS…
    def deploy(self, config_dir: Path, ns: str) -> None
    def wait_ready(self, timeout: s) -> None
    def make_loadgen(self, spec: LoadSpec) -> LoadGenerator
    def topology(self) -> Topology                                  # who is primary/replica → fault targeting
    def integrity_check(self, journal: WriteJournal) -> IntegrityResult
    def backup_ops(self) -> BackupOps | None                        # trigger/list/restore/PITR
```

Kafka/Elasticsearch/Mongo later = new folder under `drivers/` implementing the same protocol
(Kafka's "integrity" is acked-offset reconciliation; ES's is doc-count + checksum, etc.).
The runner, workers, goals, metrics, and reporting are all shared.

### Generic-app driver (test *your own* applications)

Because drivers are plugins, a `generic-app` driver drops the tech-specific parts and tests any
application that already exposes metrics: the experiment supplies a **target locator**
(`cluster.context` + namespace + label selector — that's all the K8osWorkers need to aim
faults) and goals are evaluated against the app's **own Prometheus metrics via PromQL**
(error rate, latency, availability) instead of the loadgen journal. Load is either the app's
real traffic or a user-supplied load job. Consequence for the core: goal evaluators read from
an abstract `MetricSource` (loadgen journal *or* Prometheus query), chosen per goal.
Limitation, stated honestly: without an acked-write journal there is no RPO/data-loss or PITR
verification — those stay driver-specific. Deployment can also be skipped (`deploy: none`) to
run faults against an app that's already installed.

---

## 5. Environment strategy (important findings)

Current local setup: **Docker Desktop running a kind-based multi-node cluster** — context
`docker-desktop`, 1 control plane + **4 workers** (`desktop-worker`…`desktop-worker4`),
Kubernetes v1.36.1, arm64. `helm` is not installed yet (brew, one command).

Verified capabilities and their consequences:
1. **Multi-node ✓** — node-failure experiments are meaningful. But the node containers are
   *not* visible to the host `docker` CLI (they live inside the Docker Desktop VM), so
   node-kill can't be `docker stop <node>`. Instead the `node_fail` worker uses
   `kubectl debug node/<name>` (privileged nsenter pod) to kill kubelet / halt the node, plus
   `cordon+drain` as the graceful variant. Bonus: this approach is **cluster-agnostic** — the
   same worker works against remote clusters, no Docker access assumed.
2. **No CSI snapshot support** — storage is `rancher.io/local-path` and the snapshot CRDs/
   controller aren't installed, so `VolumeSnapshot` doesn't work out of the box.
   → **Backups/PITR via Barman + SeaweedFS** (CNPG's native object-store backup) is the default
   backup path — works on *any* cluster and is what provides PITR. Volume snapshots become an
   additional, capability-gated check for clusters that support them.
- **Remote clusters need nothing special:** the experiment's `cluster.context` selects any
  kubeconfig context. The framework probes capabilities at start (node count, snapshot support)
  and skips/flags goals that the cluster can't exercise rather than failing confusingly.
- Tooling to install: `helm` (brew). A separate kind cluster is no longer needed — the existing
  Docker Desktop cluster is the primary local target; `infra/kind/` stays only as an optional
  reproducible-cluster recipe for other machines/CI.

**Load generator placement — settled: in-cluster from day one.** The loadgen runs as a
Deployment in the experiment namespace, connecting to the database via its in-cluster Service —
the realistic data path, and availability is measured from where real clients live. The
framework controls it through a small **HTTP control API** on the loadgen pod (port-forwarded):
`POST /phase` (rate, r/w mix), `GET /status`, `GET /journal` (the acked-write ledger),
`GET /metrics` (Prometheus). Key property: a port-forward blip only affects *control* traffic,
never the measured data path.

Why a custom image instead of reusing pgbench/k6: no existing tool provides the
**acked-write journal** needed for RPO/data-loss verification, and pgbench does not survive
connection loss/failover mid-run — precisely the moments we're testing. The loadgen is a few
hundred lines of async Python (psycopg3) sharing the repo's language; `pgbench` can be added
later as an optional pure-throughput worker behind the same interface. Image distribution to
the cluster (local registry vs public registry) is a phase-2 implementation detail to solve.

---

## 5b. Metrics: two tiers — authoritative records + live dashboards

Two different jobs, two different systems:

1. **Verdict tier (authoritative):** per-operation records from the loadgen journal (op,
   timestamp, latency, ack status, checksum), pulled via the control API and stored per-run in
   `results/<run>/`. Goals are evaluated **only** against this tier. Rationale: Prometheus
   scrape resolution (even at 5s) is too coarse for RTOs measured in seconds, and RPO can only
   come from journal-vs-database reconciliation. This tier is also what makes runs durable and
   comparable after the cluster is gone.
2. **Observability tier (Prometheus + Grafana, in-cluster):** `kube-prometheus-stack` installed
   once under `infra/monitoring/`. CNPG exposes Prometheus metrics natively (PodMonitor);
   loadgen exposes `/metrics`; kube-state-metrics covers pod/node churn. A pre-built Grafana
   dashboard per technology shows live latency/throughput/replication-lag, and the framework
   posts **Grafana annotations when faults fire**, so you watch the kill land on the graph in
   real time. This *is* the "proper dashboard" — off-the-shelf, no custom UI to build.

The two tiers agree by construction (same loadgen emits both); Grafana is for eyes,
the journal is for verdicts.

## 6. Wrap vs build — the principle

**Wrap a tool when it's the engine for a commodity problem; build only what differentiates us
or where a wrapper would outweigh the code it replaces.** By layer:

| Layer | Decision |
|---|---|
| Monitoring/dashboards | **Wrap**: kube-prometheus-stack, Grafana, CNPG PodMonitor. Zero custom. |
| Backup/PITR mechanics | **Wrap**: CNPG/Barman does the work; we trigger verbs and verify outcomes. |
| Faults: pod kill, drain, PVC delete | **Build**: each is ~one k8s API call. Wrapping Litmus here means operator + CRDs + RBAC to do `delete pod` — wrapper ≫ payload. |
| Faults: network partition, packet loss, IO latency | **Wrap Chaos Mesh** (later): kernel/tc-level injection is genuinely hard. Hidden behind the same `Worker` interface. |
| DB load + integrity journal | **Build**: the acked-write journal is the RPO/PITR verification and no existing tool has it (pgbench dies on failover — exactly the moment we measure). `pgbench` wrappable later as a throughput-only worker. |
| HTTP load (generic-app driver) | **Wrap k6**: best-in-class, and the journal concept doesn't apply to HTTP anyway. |
| Cluster plumbing | **Wrap**: python kubernetes client + shell out to `helm`. |

What remains ours is small and load-bearing: experiment spec, runner/timeline, journal +
integrity/PITR verification, goal verdicts, reports. Orchestrate, don't reimplement.

---

## 7. Delivery phases

**Phase 0 — environment (small):** install helm; `k8ost env check` command that probes a
context and reports capabilities (node count, snapshot support, storage classes, operators).

**Phase 1 — skeleton (1–2 days):** package layout, typer CLI, pydantic experiment models,
k8s/helm wrappers, runner lifecycle with no faults, event log, JSONL metric store,
namespace-per-run isolation, `--keep`/teardown.

**Phase 2 — CNPG happy path (2–3 days):** CNPG driver (operator install pinned, Cluster CR
deploy, readiness, primary discovery); SeaweedFS infra; **in-cluster loadgen image** (async
psycopg3, control API, acked-write journal, /metrics) + image distribution to the cluster;
integrity check; **backup + PITR verification** working end-to-end with *no* faults.
Milestone: `k8ost run experiments/postgres/cnpg-baseline` passes on the local cluster.

**Phase 3 — faults + goals (2–3 days):** timeline executor, pod_kill + node_fail + pvc_delete
workers, RTO/RPO/availability/latency goal evaluators, verdicts. Milestone — the two-experiment
proof:
  1. `cnpg-single` (1 instance): kill primary / kill its node → **goals FAIL** (long RTO,
     availability breach; node-kill with node-local storage may strand the PVC entirely).
  2. `cnpg-ha-3node` (3 instances, failover): same faults, same goals → **PASS**.
  Same goals, different config, opposite verdicts — the framework's reason to exist.

**Phase 4 — observability & reporting (2 days):** `infra/monitoring/` kube-prometheus-stack,
CNPG PodMonitor, loadgen scrape, Grafana dashboard per technology + fault annotations; per-run
report (markdown + self-contained HTML timeline), `k8ost compare run-A run-B`.

**Phase 5 — scale-out (as needed):** Chaos Mesh adapter (network faults), second technology
driver (Kafka is the best next test of the abstraction — very different integrity model),
CI mode (exit codes, JUnit XML), run-browser dashboard if HTML reports stop being enough.

**UI:** Grafana (phase 4) is the live dashboard; per-run HTML reports cover post-hoc results.
A custom run-browser UI stays deferred — nothing in the design blocks it.

---

## 8. Decisions log

1. **Primary target:** the existing Docker Desktop kind-mode cluster (4 workers); node-kill via
   `kubectl debug`/drain (cluster-agnostic, works on remote too).
2. **Experiment progression:** `cnpg-single` first — deliberately expected to FAIL fault goals —
   then `cnpg-ha-3node` to PASS the same goals. Failing verdicts are the first success criterion.
3. **Loadgen:** custom in-cluster image (async psycopg3) with HTTP control API and acked-write
   journal; pgbench optionally pluggable later. In-cluster from day one.
4. **Chaos Mesh:** deferred until network faults are needed; homegrown workers first.
5. **Metrics:** two tiers — loadgen journal (JSONL in `results/`) is authoritative for goal
   verdicts; kube-prometheus-stack + Grafana (with fault annotations) for live observability.
6. **UI:** Grafana + HTML reports; no custom web UI for now (but see license caveat in §9).
7. **License policy: permissive only** (Apache 2.0 / MIT / BSD) — no AGPL. Object store is
   **SeaweedFS** (Apache 2.0); MinIO and Garage are both AGPL and excluded. Check licenses
   before adding any dependency, chart, or image.

## 9. Still open

- ~~Grafana is AGPL~~ **Resolved:** Perses (Apache 2.0) is installed with a provisioned
  Prometheus datasource + metric explorer. Follow-up: author per-technology Perses dashboards
  as code (provisioning ConfigMap) and a fault-annotation equivalent.
- Loadgen journal durability if the loadgen pod itself dies mid-run (v1: accept as a framework
  error; later: persist journal to PVC). Journal retrieval is via pod logs (D12), so a deleted
  pod loses the journal.
- Pin the SeaweedFS image to a digest (currently `latest`).
- **Investigate: single-instance CNPG lost 12 acked writes under `kill -9`** (cnpg-single run
  20260705-232309). Suspects: storage-stack fsync honesty (local-path in the Docker Desktop VM)
  vs commit settings. Sync-rep HA lost none, which points at local storage.
- Availability measured by op-count is soft when clients back off during outages (few attempts
  → few failures). Consider a time-bucketed availability metric (fraction of seconds with ≥1
  successful op).