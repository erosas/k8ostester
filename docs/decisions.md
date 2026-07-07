# Decisions

Why the architecture is the way it is. Numbered so code and docs can reference them (D1, D2…).
Full context in [plan.md](plan.md).

## D1 — Primary target: the existing Docker Desktop kind-mode cluster
4 workers, so node-failure experiments are meaningful locally. Node containers live inside the
Docker Desktop VM (invisible to host `docker`), so node faults use `kubectl debug` (privileged
nsenter → kill kubelet) and cordon+drain — which also works unchanged against remote clusters.

## D2 — Prove the framework with a failing config first
Experiment order: `cnpg-single` (1 instance — expected to FAIL RTO/availability goals under
pod/node kill) then `cnpg-ha-3node` (same goals — expected to PASS). Same goals, different
config, opposite verdicts is the framework's reason to exist.

## D3 — Custom in-cluster load generator with an acked-write journal
No existing tool measures what we need: pgbench dies on failover (exactly the moment under
test) and nothing journals acknowledged writes for RPO reconciliation. Loadgen runs in-cluster
(realistic data path; port-forward only carries control traffic via its HTTP API). Async Python
+ psycopg3. pgbench remains pluggable later for pure throughput.

## D4 — Wrap vs build principle
Wrap a tool when it's the engine for a commodity problem; build only differentiators or where a
wrapper outweighs its payload. Concretely: wrap monitoring (Prometheus stack), backup mechanics
(CNPG/Barman), network/IO faults (Chaos Mesh, later), HTTP load (k6, later), cluster plumbing
(python k8s client, helm CLI, kubectl apply). Build: experiment spec, runner/timeline, simple
faults (pod kill/drain/PVC delete — each ~one API call; Litmus would add operator+CRDs+RBAC to
do `delete pod`), the journal + integrity/PITR verification, goal verdicts, reports.

## D5 — Two-tier metrics
Goal verdicts come only from the loadgen journal (JSONL in `results/`): Prometheus scrape
intervals are too coarse for second-level RTO, and RPO is only measurable by reconciling acked
writes against the database. Prometheus (+dashboards) is the live observability layer, never
the verdict source. The two agree by construction — same loadgen emits both.

## D6 — Backups/PITR via Barman + object store, not volume snapshots
CNPG's native object-store backup (base backups + WAL archiving) provides PITR and works on any
cluster — including this one, where `local-path` storage has no snapshot support. Volume
snapshots are an additional capability-gated check where available.

## D7 — Permissive licenses only (no AGPL)
User policy. Object store: **SeaweedFS** (Apache 2.0) — MinIO and Garage are both AGPL and
excluded. Open consequence: Grafana is AGPL too; Perses (CNCF, Apache 2.0) is the candidate
alternative — decide in phase 4. Check licenses before adding any dependency, chart, or image.

## D8 — Namespace-per-run isolation
Each run creates its own labeled namespace; teardown = delete namespace (`--keep` to inspect).
Cluster-level prerequisites (operators, object store, monitoring) are shared infra, installed
idempotently, never torn down per run.

## D9 — Shell out to kubectl/helm for manifests and charts
`kubectl apply -R` and `helm upgrade --install` are wrapped, not reimplemented — server-side
apply semantics and chart rendering are not our business. Programmatic operations (namespaces,
readiness, topology, faults) use the python kubernetes client bound to an explicit context.

## D10 — UI: Grafana-or-Perses + per-run HTML reports; no custom web UI
Live view comes from the observability stack (with fault annotations); post-hoc results are
self-contained HTML reports per run plus `k8ost compare`. A run-browser UI stays deferred until
HTML reports stop being enough.

## D11 — Generic-app driver for testing arbitrary applications
Because drivers are plugins, a `generic-app` driver tests any app that exposes metrics: target
locator (context + namespace + label selector) for fault aiming, goals evaluated via PromQL
instead of the journal, `deploy: none` supported. Limitation: no RPO/data-loss or PITR
verification without a journal — those remain driver-specific. Goal evaluators therefore read
from an abstract `MetricSource` (journal or PromQL), chosen per goal.

## D12 — Loadgen ships as a ConfigMap script on a stock image; journal via pod logs
No registry or image build needed: the driver puts `loadgen.py` in a ConfigMap and runs it as a
Job on `python:3.12-slim` (pinned psycopg installed at container start). Works unchanged against
any cluster, local or remote — the image-distribution problem disappears. Amends D3: there is no
HTTP control API in v1 — load phases are pre-declared in the spec (fault timing needs no runtime
coordination; correlation happens offline via timestamps), and the journal is one JSON line per
operation on stdout, retrieved from pod logs after the Job completes. A prebuilt image and a
control API return only if startup cost or interactive control ever matter.
**Private clusters (no PyPI/Docker Hub egress):** build `loadgen.Dockerfile` (psycopg baked in),
push it to the cluster's registry, and set `load.image` (+ `load.pull_secret` if the repo needs
auth). The Job bootstrap is idempotent — it pip-installs only when psycopg is absent — so one
template serves both paths, and the script still ships via ConfigMap either way. The pipeline is
already private-network-safe by construction: journal retrieval, exec, apply and helm are all
mediated by the Kubernetes API server, so reaching the API server is the only connectivity needed.

## D13 — PITR verification targets a deliberate pause phase
The load plan includes a zero-rate pause; the PITR target is the middle of it. Every acked write
before the pause must be in the restored cluster, nothing after it — an exact row-set assertion,
immune to client/server clock skew and commit-vs-statement timestamp gaps at the boundary.

## D15 — Technologies own their directory: driver + experiments + prerequisites
`technologies/<tech>/` contains `driver.py` (+ helpers like `loadgen.py`) and `experiments/`.
Drivers are discovered by walking up from the experiment directory to the nearest `driver.py`
(loaded dynamically; `DRIVER` attr or the single TechnologyDriver subclass); built-ins (generic)
remain a core fallback. Tech-specific prerequisites and their version pins (e.g. the CNPG
operator chart) live in the tech driver; core `InfraManager` only owns **common** infra
(SeaweedFS, monitoring) and drivers delegate those entries to it. Future: per-tech Python
dependencies declared in the tech dir (uv extras) — not needed yet.

## D16 — Network faults wrap Chaos Mesh; one CR template per action
Per D4: tc/iptables-level injection inside a pod's netns is genuinely hard, so `network_partition`
/ `network_loss` / `network_delay` are thin workers that render a NetworkChaos CR into the run
namespace — same `Worker` interface as the built-ins, so experiments only swap the `worker:` name.
Chaos Mesh (Apache 2.0, pinned chart) is a **common infra** entry (`chaos-mesh`) since any
technology can use it; the daemon attaches to the node's containerd socket. Faults carry a
required `duration` (Chaos Mesh auto-heals) plus a cleanup that deletes the CR, so an aborted run
can't leave a partition behind. Each action gets its own complete template file (D-templates rule)
rather than one template mutated in Python. Workers now receive the whole `FaultSpec`, not just
the target — duration and worker-specific `params` (loss %, latency) ride along.

## D17 — Load runners are pluggable per technology; pgbench is the Postgres alternative
`load.runner` selects the instrument: `journal` (default — the acked-write loadgen, full goal
set) or a technology-standard benchmark (`pgbench` for Postgres: TPC-B shape, industry-comparable
tps, runs from the same operand image the database pods use). Each runner declares what it can
measure and the spec validator enforces the boundary up front: pgbench has no journal and its
clients abort on connection loss, so faults, rpo/integrity/pitr, availability and connect goals
are rejected at load time — it serves `tps` (new goal metric), `write_latency_*`, and the backup
check. Runner output is normalized into the same per-op record stream (pgbench's `--log`
per-transaction lines are parsed into op records), so the verdict tier, goal evaluators and
report graphs are runner-agnostic. Future drivers follow the pattern (Kafka:
`kafka-producer-perf-test`; ES: rally) — journaled runner for fault/loss experiments,
ecosystem benchmark for throughput credibility.

## D18 — SLOs are calibrated per fault class; uptime replaces op-count availability under faults
Goals must be achievable by a well-tuned config or they measure nothing: the partition arms
inherited the pod_kill arms' SLOs (rto 10s, connect_error_rate 1%) and every config failed them,
including the best possible one. Partition detection via the operator has a floor (~15-20s tuned:
readiness probe expiry + status propagation + promotion), so partition-class goals are rto ≤ 25s
and uptime ≥ 88%; connect_error_rate is dropped there because a whole-run ratio just counts the
retry storm during the outage (80-90% regardless of config — no discrimination). **uptime** is the
time-bucketed availability from plan §9: % of seconds with ≥1 successful op. It replaces op-count
availability in every fault-bearing experiment because pooled clients that cannot get a connection
attempt nothing — op-count availability scored 100.00% across a 40.6s outage. Op-count
availability remains for fault-free load tests (04/05), where attempts never stop. Historical
runs keep the verdicts of the goals they ran under; recalibration applies from re-run onward.

## D14 — RTO is a gap between loadgen timestamps; fault events only locate the window
Fault timestamps live on the framework clock, op records on the loadgen pod's clock. Mixing them
in arithmetic would bake host↔pod clock skew into RTO. So the evaluator finds the largest gap
between consecutive successful writes *starting* within a window around the fault — both ends of
the gap are on the same clock, and skew merely shifts the window. The gap's far end is unbounded
(an outage longer than the window must be reported at full length, not truncated — the partition
arms proved this the hard way), and a gap still open at the last recorded op is counted up to
that op as a censored lower bound. Fault targets resolve at injection
time, not run start: after a failover, "primary" is a different pod. Cluster-level fault
mutations (cordons) return cleanup callables run at teardown — a namespace delete won't undo
them.

