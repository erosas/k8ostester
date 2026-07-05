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

## D13 — PITR verification targets a deliberate pause phase
The load plan includes a zero-rate pause; the PITR target is the middle of it. Every acked write
before the pause must be in the restored cluster, nothing after it — an exact row-set assertion,
immune to client/server clock skew and commit-vs-statement timestamp gaps at the boundary.

