# k8os-testbed

A **k8s-native, single-purpose production-readiness testbed** for the one ideal
CNPG config. Where `k8ostester-core` explores fast to *find* good configs, this
module proves a chosen config is **operable**: it walks the real operations you
must trust before production and gives a single PASS/FAIL.

It is deliberately its own thing — a linear script, not the core engine. See
[../docs/productionization.md](../docs/productionization.md) for the design.

## The golden path (`flow.py`)

```
provision → deploy cluster + app → steady → base backup
  → rotate credentials → minor PG upgrade → PITR restore → verify
```

Each step runs real k8s/CNPG operations and appends a line to `events.jsonl`
(the annotation source for the phase-2 Grafana console). The run ends with a
PASS/FAIL per step.

What each step proves:

| Step | Proves |
| --- | --- |
| provision | operator + object store + the ideal cluster + a real app come up clean |
| backup | a Barman base backup completes to the object store |
| rotate-credentials | **blue/green** switch between two valid roles — near-zero downtime, no failed auth |
| minor-upgrade | an `imageName` bump rolls the cluster and the PG version actually moves |
| restore-pitr | a second cluster restored to a chosen point holds rows up to (not past) it |
| verify | cluster healthy + app serving at the end |

## Run it

Needs `kubectl` + `helm` on PATH and a kube context that can install an operator
(kind, docker-desktop, or a remote cluster where you may install CNPG). The
testbed **self-provisions everything** — operator, object store, cluster, app.

```bash
cd k8os-testbed
python flow.py                      # run against the current context
python flow.py --context my-remote  # a specific context
python flow.py --keep               # leave it running to inspect (else auto-cleanup)
python flow.py cleanup              # delete the testbed namespace (operator left installed)
```

Everything lands in the `k8os-testbed` namespace (the operator in `cnpg-system`).

## What's here

```
manifests/
  01-seaweedfs.yaml   object store for Barman backups/WAL (self-contained)
  02-cluster.yaml     THE ideal config — HA + quorum sync, backup/WAL/PITR,
                      periodic base backups, rw + ro poolers; app password
                      managed from a secret so rotation is a one-line update
  03-app.yaml         the dummy app — reuses the k8os-loadgen image, pooled
                      read/write with split routing, exposes /metrics on :8000
monitoring/
  prometheus.yaml     scrapes the app + CNPG DB metrics (in-pod k8s SD)
  grafana.yaml        the console — datasource + dashboard as code, step
                      annotations, a PG-version-over-time panel
flow.py               the linear golden path
events.jsonl          written per run; local record of the step annotations
```

## The console (phase 2)

Provisioned automatically. `flow.py` POSTs a Grafana annotation per step, so the
dashboard overlays the backup/rotate/upgrade/restore markers as vertical lines
over the app + DB metrics — you *see* the app dip the instant creds rotate, and
recover.

```bash
kubectl -n k8os-testbed port-forward svc/grafana 3000:3000
# → http://localhost:3000  (admin/admin), dashboard "k8os-testbed — production readiness"
```

Panels: app throughput/latency, app up, DB instances up, PG-version-over-time.
(The version panel reads `cnpg_collector_postgres_version` — adjust the metric
name in the dashboard if your operator differs.)

## Credential rotation (blue/green)

Two login roles `app_a` / `app_b`, each with its own secret, both members of the
object-owning `app` role (so they share the tables). An `app-active` ConfigMap
selects which role the app authenticates as. Rotation:

1. refresh the **idle** role's password (safe — nothing uses it)
2. flip the `app-active` selector to it
3. rolling-restart the app (2 replicas → one pod at a time)

Because **both roles stay valid the whole time**, no connection is ever rejected
— the switch is near-zero downtime (a single role can never do this: one role
has one password, so it always has a hard cutover). **Rollback** is flipping the
selector back — the previous role's password was never touched. The dashboard's
"Active app role" panel shows the flip at the `rotate` annotation.

## AZ spread (`--az`, multi-node)

Proves the config survives an availability-zone failure — mirrors a self-managed
cluster in AWS, where nodes carry `topology.kubernetes.io/zone` labels.

```bash
kind create cluster --config kind/kind-az.yaml     # 3 workers, zones k8os-az-a/b/c
python flow.py --context kind-k8os-testbed --az --keep
kind delete cluster --name k8os-testbed
```

**Making the sync replica always cross-AZ — the simple trick.** Instead of tuning
the synchronous config to "prefer" a different-zone replica, `--az` enforces
**one instance per zone** (hard `topologySpreadConstraints`, `manifests/az/spread.yaml`).
The primary is then *alone* in its AZ, so **every** replica is in a different AZ —
which means the existing `any/1` quorum sync is *automatically* satisfied by a
cross-AZ replica, with no sync-config knob at all. Placement makes the guarantee
structural. `maxSkew 1 + DoNotSchedule` keeps it true through an AZ outage (the
lost instance stays Pending rather than doubling up).

`verify_sync_az` then *proves* it: it maps each streaming standby → node → zone
and asserts they all differ from the primary's zone. (In production you'd run the
same check as a CronJob guardrail — an external process that continuously asserts
the invariant.) The object store is regional, so backups/WAL survive the AZ loss.

(The single-node default run skips all this — spread needs real zones.)

## Notes / next phases

- **AZ-failure drill:** cordon+drain a whole zone and assert failover with RPO 0
  (the natural next fault, on top of `--az` spread).
- **Phase 3 — major upgrade:** the `pg_upgrade` path (CNPG ≥ 1.26), added as a
  step after the minor upgrade.
- **PG image tags** (`PG_IMAGE_FROM`/`PG_IMAGE_TO` in `flow.py`) — adjust to a
  minor pair available for your operator.
- **Private registry:** override the app image in `03-app.yaml`; the loadgen
  image is pulled by the cluster, so give the nodes registry creds.
