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
| rotate-credentials | rotating the app password (CNPG-managed role) recovers — the app rides it |
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
flow.py               the linear golden path
events.jsonl          written per run; the console's annotation source (phase 2)
```

## Notes / next phases

- **Phase 2 — console:** Prometheus scrapes the app `/metrics` + the CNPG
  `:9187` DB metrics; Grafana renders it, with `flow.py`'s step events as
  annotation lines and a PG-version-over-time panel. (`flow.py` will POST a
  Grafana annotation per step.)
- **Phase 3 — major upgrade:** the `pg_upgrade` path (CNPG ≥ 1.26), added as a
  step after the minor upgrade.
- **PG image tags** (`PG_IMAGE_FROM`/`PG_IMAGE_TO` in `flow.py`) — adjust to a
  minor pair available for your operator.
- **Private registry:** override the app image in `03-app.yaml`; the loadgen
  image is pulled by the cluster, so give the nodes registry creds.
