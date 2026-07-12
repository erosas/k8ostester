# K8osTester — instructions

Validate Kubernetes configurations of stateful technologies (Postgres first) against explicit
resilience and performance goals — or attach to a live cluster and drive chaos by hand.
Every code block below is runnable (IntelliJ: click the gutter icon), from the repo root.

## Setup

Requires Python ≥ 3.14, `kubectl` and `helm` on PATH, and a kubeconfig context
(local docker-desktop/kind or remote — everything rides the Kubernetes API).

```bash
uv tool install --editable ./k8ostester-core
```

```bash
k8ost env check          # what can this cluster do? (nodes, storage, snapshot CRDs, operators)
```

## Scenario A — validate a config against assumptions

An **experiment** is a directory: `manifests/` (the config under test) + `experiment.yaml`
(load plan, fault timeline, verification steps, goals). The runner deploys into a throwaway
namespace, drives load, injects the faults on schedule, verifies, evaluates goals, tears down.

```bash
k8ost validate experiments/postgres-cnpg/16-cnpg-dr-drill
```

```bash
k8ost run        # interactive picker + full-screen dashboard (the default on a terminal)
```

```bash
k8ost run experiments/postgres-cnpg/03-cnpg-ha-3node          # a specific experiment
```

```bash
k8ost run experiments/postgres-cnpg/02-cnpg-single --view plain   # CI-style log lines
```

Exit codes: `0` passed · `2` goals/verification failed · `1` framework/infra error.
Compare arms of an investigation (same `group:` in experiment.yaml):

```bash
k8ost run experiments/postgres-cnpg/16-cnpg-dr-drill --group dr-drill
```

```bash
k8ost run experiments/postgres-cnpg/17-cnpg-dr-drill-sync-first --group dr-drill
```

```bash
k8ost report --group dr-drill --open      # overlaid graphs + goal matrix, self-contained HTML
```

```bash
k8ost runs                                # every recorded run
```

## Scenario B — the interactive lab (`k8ost session`)

Deploys the experiment's config, then hands you the controls: no timeline, no verdict —
you are the experiment plan. Load runs **in-cluster as containers** (each load pod =
`--clients` connections at `--rate` ops/s, forever), so the laptop is only the driver.

```bash
k8ost session experiments/postgres-cnpg/17-cnpg-dr-drill-sync-first --pods 1 --rate 20
```

| Control | Key | What it does |
|---|---|---|
| `load −` / `load +` | `-` / `+` | scale the load pool by one pod (≈ one `--rate` unit) |
| `rate −` / `rate +` | `[` / `]` | change ops/s **per pod** ±5 — the pool rolls at the new rate |
| target dropdown | — | `primary (auto)`, `any replica (auto)`, or a specific instance |
| `kill` | `k` | pod_kill the selected target (grace 0) |
| `partition 30s` | `p` | full network partition of the target (needs `chaos-mesh` in the experiment's infra) |
| `q` | `q` | stop, collect artifacts, tear down |

The dashboard shows live ops/s + error %, live goal scores (same evaluator as the verdict),
and the topology tree — roles by shape (`▷` client, `◆` pooler, `●` primary, `○` replica),
**status by color** (green healthy/streaming, yellow transitioning, red failed/detached),
replication mode + lag on the edges (`─sync─▶`, `─async +2.1s─▶`).

### Backup & point-in-time restore (tech ops row)

The `tech ops` row is driver-defined (a Kafka driver would offer different ops). For CNPG
it appears when the cluster archives to an object store, and works like real DR tooling:

1. **`base backup`** — takes a Barman base backup. This **opens the restore window**:
   PITR can restore to any point between the end of the earliest backup and *now*
   (WAL archiving covers the span continuously — there is no separate "snapshot" step;
   the base backup *is* the anchor. Volume-snapshot backups exist in CNPG but need
   snapshot-class support, which `k8ost env check` reports).
2. **`restore (PITR)`** — appears once the window exists. Its dropdown shows the window
   (`12:01:33Z → now`) and concrete points inside it (`now − 1m`, `now − 5m`, …,
   `window start`). Pick one, click: a second cluster `<name>-pitr` bootstraps from the
   object store at that instant and reports its row count. Targets outside the window
   clamp to it. The restore cluster is a k8ost artifact — removed at session end.

## Scenario C — attach to an existing cluster (chaos control plane)

No experiment dir, no deploy. The technology is auto-detected (CNPG: its Cluster CR),
controls are live in about a second, and **teardown never touches the namespace** —
only k8ost's own artifacts (load pool, configmap, PITR restore cluster) are removed.

```bash
k8ost session --attach my-namespace
```

```bash
k8ost session --attach my-namespace --context prod-cluster --technology postgres-cnpg
```

The load pool starts at **0 pods**: your applications drive the load and you drive the
chaos, watching their telemetry. Press `load +` at any moment to add k8ost's own
journaled load against the cluster's `rw` service (its metrics then appear in the
dashboard). Network faults require chaos-mesh on that cluster.

## Results

Every run/session writes `results/<name>/<stamp>[-session]/`:
`events.jsonl` (the full timeline — every fault, topology change, sample),
`summary.json`, `metrics.jsonl` + `journal.jsonl` (per-operation records / acked
writes), `loadgen.log` (raw pool output), `experiment.json` (resolved spec).

## Images & data flow

### What runs where

```
laptop (CLI)                         │  cluster
─────────────────────────────────────┼────────────────────────────────────────
k8ost (python) ── kube API ─────────▶│  namespace-per-run (or attached ns)
  │  kubectl apply/exec (shell-out)  │   ├─ postgres pods        (your manifests)
  │  helm (infra installs)           │   ├─ pgbouncer Pooler     (your manifests)
  │                                  │   ├─ loadgen pods         (k8ost-created)
  ├─ reads pod logs ◀── journal ─────│───┘   └─ stdout = the journal (JSON/op)
  ├─ reads CR status + psql exec ◀───│  topology, sync/async, lag, health
  └─ writes results/ locally         │
                                     │  cluster-level, shared, never torn down:
                                     │   ├─ cnpg operator        (helm, pinned)
                                     │   ├─ seaweedfs + bucket   (object store for backups/WAL)
                                     │   └─ chaos-mesh           (network faults)
```

Key properties: **no agent** is installed in the cluster; all measurement rides the
Kubernetes API (pod logs, CR status, exec). Backup/WAL traffic flows pod → object store
entirely in-cluster. The laptop sends control calls and pulls logs — which is why remote
clusters behave identically and the laptop can drive load tests without carrying the load.

### Every container image involved

| Image | Runs as | Pulled when | Pinning | Override / injection point |
|---|---|---|---|---|
| `python:3.12-slim` | loadgen pods (Job for runs, Deployment for sessions); pip-installs `psycopg` at start | any load plan / session pool | tag | `load.image` + `load.pull_secret` in experiment.yaml — see prebuilt image below |
| `ghcr.io/cloudnative-pg/cloudnative-pg` (operator ≈1.29.1) | CNPG operator | infra `- operator: cnpg` | helm chart `0.28.3` | preinstall the operator out-of-band — the driver detects the CRD and touches nothing |
| `ghcr.io/cloudnative-pg/postgresql:<v>` | database pods **and** the pgbench runner (D17: no extra image) | your manifests deploy | your `cluster.yaml` (`imageName`) | fully manifest-controlled |
| CNPG's default PgBouncer image | Pooler pods | pooler experiments | operator default | `Pooler` spec in your manifests |
| `chrislusf/seaweedfs:latest` | object store (`k8ost-infra` ns) | infra `- seaweedfs` | **unpinned (known TODO)** | place `infra/seaweedfs/` under your CWD to override the packaged manifests |
| `ghcr.io/chaos-mesh/*:v2.8.3` | chaos controller + per-node daemons | infra `- chaos-mesh` | helm chart `2.8.3` | `infra/chaos-mesh/values.yaml` under CWD (e.g. different containerd socket); preinstalled release tolerated |

### Private / air-gapped clusters

The only runtime dependency on the public internet *inside the cluster* is the loadgen's
`pip install psycopg`. Kill it by building the prebuilt loadgen image and pointing the
experiment at it:

```bash
docker build -f k8ostester-core/src/k8ostester/technologies/postgres_cnpg/loadgen.Dockerfile \
  -t my-registry.example.com/k8ost-loadgen:latest \
  k8ostester-core/src/k8ostester/technologies/postgres_cnpg
```

```bash
docker push my-registry.example.com/k8ost-loadgen:latest
```

```yaml
# experiment.yaml
load:
  image: my-registry.example.com/k8ost-loadgen:latest
  pull_secret: my-registry-creds        # imagePullSecret in the run namespace
```

Mirror-list for a fully air-gapped install: the loadgen image (above), your postgres
image, the CNPG operator image, seaweedfs, and the three chaos-mesh images. Helm chart
repos are only needed at install time — both the operator and chaos-mesh installs
tolerate an already-installed release when the chart repo is unreachable.

## Development

```bash
cd k8ostester-core && uv run pytest
```

```bash
uv run --project k8ostester-core k8ost run experiments/generic/01-nginx-smoke   # 20s smoke
```
