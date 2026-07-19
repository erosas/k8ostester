# k8ostester-pg

The **PostgreSQL / CloudNativePG vertical** — direct, tech-specific logic on the
kernel primitives, no framework abstraction. See
[docs/architecture-restructure.md](../docs/architecture-restructure.md).

```
pg/
  src/k8ostester_pg/
    harness.py    shared provisioning (deploy ideal config, bucket, cluster helpers)
    slo.py        standard CNPG SLO checks (kernel SloChecks over Prometheus)
    server.py     the k8ost-console control plane — stdlib HTTP server + SSE
    console.html  the single-file SPA it serves
    discover.py   read a live cluster (pods + psql/df execs) → one snapshot
    control.py    the CNPG action set (precondition + available, on the kernel model)
    execute.py    gate → dispatch an action (chaos primitives / ops)
    ops.py        rotate credentials / minor upgrade / PITR restore
    builder.py    form options → CNPG manifest (resources/*.tmpl.yaml)
    dashboard.py  form options → adaptive Grafana dashboard JSON
    registry.py   image tag discovery + pull checks (for upgrades)
    resources/    manifest + dashboard templates (${VAR} substitution)
  experiments/    linear experiment scripts (deploy → chaos → verify → verdict)
    kill-primary/run.py       killing the primary → real disruption
    kill-replica/run.py       killing a replica   → non-event (contrast)
    partition-primary/run.py  network-isolate the primary → self-fence + failover
  testbed/        the production-readiness golden path (see testbed/README.md)
  deploy/         in-cluster manifests for the console (console.yaml, console-lab.yaml,
                  rbac-clusterwide.yaml) + console.Dockerfile
  tests/
```

## The control console (`k8ost-console`)

An interactive web control plane for a live CNPG cluster: it discovers what's
running and lets you **operate** (backup, rotate credentials), **break-glass**
(PITR restore, minor upgrade, inject a fault), and **build** (generate + deploy a
manifest, with observability). It runs laptop-side against a kubeconfig or
in-cluster as a control plane. The whole design — the capability model,
discovery, and the RBAC-as-blast-radius deployment — is in
[docs/remote-control.md](../docs/remote-control.md).

```bash
uv run --directory pg k8ost-console --context my-ctx --namespace demo --cluster orders
# → http://127.0.0.1:8700
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

The old generic engine (runner/goals/report + the CNPG driver) has been retired;
experiments live here as linear scripts. More fault scenarios are added the same
way — a thin `deploy → chaos → verify → verdict` script on the harness.
