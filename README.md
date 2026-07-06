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
k8ost run technologies/postgres-cnpg/experiments/cnpg-single
k8ost runs                                   # list recorded runs
k8ost report --group pooling --open          # comparison graphs for a run group
k8ost dashboard                              # live metrics (Perses; needs monitoring infra)
```

Runs write artifacts to `results/<experiment>/<run-id>/` (events timeline, journal, metrics,
summary). Add `--keep` to leave the run namespace up for inspection, `--group` to group runs
for reporting.

## Layout

- `k8ostester/` — the framework core (CLI `k8ost`): runner, workers, goals, reports, common infra
- `technologies/<tech>/` — each technology owns its `driver.py` and its `experiments/` (D15)
- `infra/` — common cluster prerequisites (object store, monitoring)
- `results/` — per-run artifacts (gitignored)
