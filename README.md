# K8osTester

[![ci](https://github.com/erosas/k8ostester/actions/workflows/ci.yml/badge.svg)](https://github.com/erosas/k8ostester/actions/workflows/ci.yml)
[![codeql](https://github.com/erosas/k8ostester/actions/workflows/codeql.yml/badge.svg)](https://github.com/erosas/k8ostester/actions/workflows/codeql.yml)
[![coverage](https://raw.githubusercontent.com/erosas/k8ostester/badges/coverage.svg)](https://github.com/erosas/k8ostester/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Prove that a stateful Kubernetes configuration is **resilient** and **operable**.
Deploy a config under load, inject faults (kill/partition the primary, drop a
replica, drain a zone) or drive real operations (backup, credential rotation, PG
upgrade, PITR restore), and get a machine-checkable verdict from real,
app-perspective metrics. PostgreSQL / CloudNativePG first.

## How it's built

A small [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/) —
a thin kernel of primitives plus one vertical per technology. There is
deliberately **no generic framework**: verticals are direct, tech-specific
scripts on the kernel. See [docs/architecture-restructure.md](docs/architecture-restructure.md).

```
kernel/   primitives: k8s client, chaos (kill/partition/drain), SLO-query verdict,
          the Run helper, and a cluster capability probe
  console/  the shared, persistent Prometheus + Grafana (viewing + cross-run compare)
pg/       the PostgreSQL/CloudNativePG vertical
  src/k8ostester_pg/   harness (deploy the ideal config) + SLO checks
  experiments/         linear experiment scripts (deploy → chaos → verify → verdict)
  testbed/             the production-readiness golden path (backup, rotate, upgrade, PITR)
  loadgen/             the k8os-loadgen image (the app-perspective load generator)
```

## The model

An **experiment** is a linear script that reads top to bottom:

```python
harness.deploy_ideal_config(k8s, NS, EXPERIMENT)   # config + app + healthy
chaos.kill_pod(k8s, NS, primary)                    # a kernel primitive
run.verify("primary_moved", ...)                    # correctness (data compare)
verdict = run.verdict(fetch, default_checks(EXPERIMENT))   # + SLO range-queries
```

The **verdict** = correctness verify-steps (RPO, integrity, PITR) **and** SLO
checks evaluated as Prometheus range queries over the run window (error rate,
latency, availability — averaged, so a sub-second blip is not an outage). Live
viewing and **cross-run comparison** happen in Grafana over the shared, persistent
Prometheus (each run's metrics are labelled by `experiment`/`run` and outlive it).

## Quick start

Requires Python ≥ 3.14, `kubectl`, `helm`, and a cluster with the CloudNativePG
operator + the shared console ([kernel/console](kernel/console)).

```bash
uv sync                                              # the workspace

# what can this cluster run? (zones, NetworkPolicy enforcement, snapshots, operators)
uv run python -m k8ostester_kernel.capabilities --context my-ctx

# a resilience experiment (deploy → kill the primary → verify → verdict)
uv run python pg/experiments/kill-primary/run.py --context my-ctx --prometheus http://localhost:9090

# the production-readiness golden path (deploy → backup → rotate → upgrade → PITR)
uv run python pg/testbed/flow.py --context my-ctx --keep
```

See [pg/README.md](pg/README.md) and [pg/testbed/README.md](pg/testbed/README.md).

## Images

One published image (multi-arch, on a version tag — see
[.github/workflows/release.yml](.github/workflows/release.yml)):

| Image | What it is | Override |
| --- | --- | --- |
| [`bytestream89/k8os-loadgen`](https://hub.docker.com/r/bytestream89/k8os-loadgen) | the app-perspective load generator (python + psycopg) | the app manifest / `K8OST_LOADGEN_IMAGE` |

Built on a minimal [Chainguard Wolfi](https://github.com/wolfi-dev) base
(continuously rebuilt → ~0 CVEs), with an SBOM + provenance. Behind a proxy,
mirror it and point the app manifest at your registry.

## Docs

- **[docs/architecture-restructure.md](docs/architecture-restructure.md)** — the kernel + verticals design
- **[docs/productionization.md](docs/productionization.md)** — the testbed golden path
- **[docs/remote-control.md](docs/remote-control.md)** — the planned web control plane (ops + chaos)

## Develop

```bash
uv sync
uv run ruff check .
uv run --directory kernel pytest
uv run --directory pg pytest
```

MIT licensed.
