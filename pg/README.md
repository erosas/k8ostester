# k8ostester-pg

The **PostgreSQL / CloudNativePG vertical** — direct, tech-specific logic on the
kernel primitives, no framework abstraction. See
[docs/architecture-restructure.md](../docs/architecture-restructure.md).

```
pg/
  src/k8ostester_pg/
    slo.py        standard CNPG SLO checks (kernel SloChecks over Prometheus)
  experiments/    linear experiment scripts (the new model, replacing goals)
    kill-primary/run.py
  testbed/        the production-readiness golden path (see testbed/README.md)
  tests/
```

## Experiments — the linear model

`experiments/kill-primary/run.py` is the first experiment in the new model,
replacing the old fault-timeline + goals engine. It reads top to bottom — deploy
the ideal config, drive load, `chaos.kill_pod` the primary, verify failover — and
the **kernel `Run` helper** assembles the verdict from inline verify-steps
(correctness) + `slo.py` SLO range-queries over the run window. No generic
runner, no goals evaluator; just a script + the kernel primitives.

## What's here

- **`testbed/`** — the self-provisioning golden-path testbed (deploy → backup →
  rotate creds → upgrade → PITR → verify), with the SCADA console and the `--az`
  drill. Run it with `python pg/testbed/flow.py`; see
  [testbed/README.md](testbed/README.md).
- **`slo.py`** — the threshold *goals* from the old experiments (error rate,
  latency, availability) as kernel `SloCheck`s, evaluated over the run window as
  Prometheus range queries. Correctness goals (RPO, integrity, PITR) stay as
  inline verify-steps.

## Coming next (per the restructure)

- The `experiments/postgres-cnpg/` suite converts to **linear scripts** here
  (deploy → load → fault, with `slo.py` checks + verify-steps forming the
  verdict), replacing the generic runner/goals.
- The framework-coupled `postgres_cnpg` driver stays in `k8ostester-core` until
  the old engine is retired.
