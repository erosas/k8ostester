# k8ostester-pg

The **PostgreSQL / CloudNativePG vertical** — direct, tech-specific logic on the
kernel primitives, no framework abstraction. See
[docs/architecture-restructure.md](../docs/architecture-restructure.md).

```
pg/
  src/k8ostester_pg/
    slo.py        standard CNPG SLO checks (kernel SloChecks over Prometheus)
    harness.py    shared provisioning (deploy ideal config, bucket, cluster helpers)
  experiments/    linear experiment scripts (the new model, replacing goals)
    kill-primary/run.py    killing the primary breaches strict SLOs → FAIL
    kill-replica/run.py    killing a replica is a non-event          → PASS
  testbed/        the production-readiness golden path (see testbed/README.md)
  tests/
```

## Experiments — the linear model

Each experiment reads top to bottom — deploy the ideal config, drive load, inject
chaos, verify — and the **kernel `Run` helper** assembles the verdict from inline
verify-steps (correctness) + `slo.py` SLO range-queries over the run window. No
generic runner, no goals evaluator. `harness.py` holds the shared provisioning so
each script stays thin:

```python
harness.deploy_ideal_config(k8s, NS, EXPERIMENT)   # config + app + bucket + healthy
chaos.kill_pod(k8s, NS, primary)                    # kernel primitive
run.verify("primary_moved", ...)                    # correctness
verdict = run.verdict(fetch, default_checks(EXPERIMENT))   # + SLO queries
```

`kill-primary` and `kill-replica` are a matched pair: the verdict **discriminates**
— a primary kill breaches the strict SLOs (FAIL), a replica kill is a non-event
(PASS). Validated on a real cluster (kill-primary ran end to end on docker-desktop).

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
