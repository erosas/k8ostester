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

Requires Python >= 3.14.

```bash
uv tool install --editable ./k8ostester-core   # installs the `k8ost` CLI

k8ost env check                              # what can this cluster do?
k8ost run                                    # pick an experiment, watch it in the TUI
k8ost run experiments/postgres-cnpg/02-cnpg-single   # TUI on a terminal; --view live|plain
k8ost session experiments/postgres-cnpg/03-cnpg-ha-3node   # interactive lab: scale load, fire faults
k8ost session --attach my-namespace          # attach to an EXISTING cluster as a chaos control plane
k8ost runs                                   # list recorded runs
k8ost report --group pooling --open          # comparison graphs for a run group
```

Runs write artifacts to `results/<experiment>/<run-id>/` (events timeline, journal, metrics,
summary). Add `--keep` to leave the run namespace up for inspection, `--group` to group runs
for reporting.

To develop the framework itself: `cd k8ostester-core && uv run pytest`.

## Layout

Platform code and experiments are strictly separated: `k8ostester-core/` is the framework
(what gets installed), `experiments/` is what an end user's config repo looks like.

- `k8ostester-core/` — the platform, a self-contained Python project:
  - `src/k8ostester/` — the source: CLI (`k8ost`), runner, workers, goals, reports, common
    infra, built-in technology drivers (D20)
  - `tests/` — the framework test suite, mirroring the source layout
- `experiments/<tech>/<experiment>/` — the example/regression experiment suite; each experiment
  is a directory with `experiment.yaml` + `manifests/`. A user config repo needs nothing else —
  built-in drivers resolve by the `technology:` name, and a custom `driver.py` placed above an
  experiment overrides them (D15)
- `results/` — per-run artifacts (gitignored)
