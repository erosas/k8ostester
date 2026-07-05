# K8osTester

Validate Kubernetes configurations of stateful technologies (Postgres first; Kafka,
Elasticsearch, MongoDB later) against explicit resilience and performance goals: run a config
under load, inject faults (kill the primary, fail a node…), verify data integrity and
backup/PITR, and get a pass/fail verdict per goal.

## Docs

- **[docs/plan.md](docs/plan.md)** — the north star: full plan, phases, current status
- **[docs/architecture.md](docs/architecture.md)** — components and how a run works
- **[docs/decisions.md](docs/decisions.md)** — why it is this way (D1–D11)

## Quick start

```bash
uv pip install -e . -p .venv/bin/python

k8ost env check                              # what can this cluster do?
k8ost validate experiments/examples/nginx-smoke
k8ost run experiments/examples/nginx-smoke   # full lifecycle smoke test
```

Runs write artifacts to `results/<experiment>/<run-id>/` (events timeline, spec snapshot,
summary). Add `--keep` to leave the run namespace up for inspection.

## Layout

- `k8ostester/` — the framework (CLI `k8ost`)
- `experiments/` — experiment directories: the configs being validated + `experiment.yaml`
  (load, faults, goals)
- `infra/` — shared cluster prerequisites (operator pins, object store, monitoring)
- `results/` — per-run artifacts (gitignored)
