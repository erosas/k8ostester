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
    kill-primary/run.py       killing the primary → real disruption
    kill-replica/run.py       killing a replica   → non-event (contrast)
    partition-primary/run.py  network-isolate the primary → self-fence + failover
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

The old generic engine (runner/goals/report + the CNPG driver) has been retired;
experiments live here as linear scripts. More fault scenarios are added the same
way — a thin `deploy → chaos → verify → verdict` script on the harness.
