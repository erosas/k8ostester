# k8ostester-pg

The **PostgreSQL / CloudNativePG vertical** — direct, tech-specific logic on the
kernel primitives, no framework abstraction. See
[docs/architecture-restructure.md](../docs/architecture-restructure.md).

```
pg/
  src/k8ostester_pg/
    slo.py        standard CNPG SLO checks (kernel SloChecks over Prometheus)
  testbed/        the production-readiness golden path (see testbed/README.md)
  tests/
```

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
