# Productionization testbed — design record

Status: **implemented**. Kept as the design rationale for `pg/testbed`; the
authoritative "how to run it" is [pg/testbed/README.md](../pg/testbed/README.md).

## Why a separate module

The testbed and the resilience **experiments** have different jobs and are not
forced to share a model:

| | the experiments (`pg/experiments`) | the testbed (`pg/testbed`) |
| --- | --- | --- |
| Purpose | find where a config breaks under chaos | prove **the one ideal config** is operable |
| Shape | linear fault scripts (kill / partition) | a **linear, pre-determined golden path** |
| Question it answers | "does this survive chaos?" | "can I run this in production, and automate it?" |
| Runs on | k8s | k8s (same substrate — no docker-compose) |

Code reuse is **explicitly not a goal**. `pg/testbed` re-implements what it needs
as a simple script rather than bending a shared abstraction. Simplicity over DRY.
(The earlier generic "core" engine this was contrasted against has since been
retired — see [architecture-restructure.md](architecture-restructure.md).)

## Principles

- **k8s-native.** The DB runs on your operator (as it must, to be real).
  Everything else — the dummy app, the OTEL collector, the console — deploys as
  ordinary k8s workloads. One substrate.
- **Linear.** The flow reads top-to-bottom. No engine, no plugin registry.
- **Grafana for the dashboards.** In-cluster, dashboards-as-code. (Local test
  tool, not a shipped dep — the AGPL policy doesn't bite here.)
- **Single config.** It tests `20-cnpg-reference` (the ideal), nothing else.

## Module layout

```
pg/testbed/
  manifests/
    cluster.yaml        # the ideal config (from 20-cnpg-reference)
    app.yaml            # the dummy application (reads + writes, exports OTEL)
    otel-collector.yaml # scrapes DB metrics, receives app metrics → console
  flow.py               # THE golden path, linear, top to bottom
  events.jsonl          # what flow.py emits (the annotation source)
  monitoring/           # in-cluster Prometheus + Grafana (dashboards-as-JSON)
  README.md
```

## The golden path (`flow.py`)

One linear script. Each step does a few kubectl/client calls, then appends an
event to `events.jsonl`. Readable as prose:

```
1. deploy         apply manifests/ → wait cluster + app healthy
2. steady         let the app drive reads+writes; capture a baseline
3. backup         take a base backup                       → event: backup
4. rotate-creds   rotate the app password                  → event: rotate
                  ASSERT the app's error rate stays under SLO and recovers
5. minor-upgrade  bump the PG minor image                  → event: upgrade
                  ASSERT rolling restart completes, app rides it, version moves
6. restore-pitr   restore a 2nd cluster to a chosen point   → event: restore
                  ASSERT it holds exactly the rows expected at that point
7. verify         integrity across the whole run → PASS/FAIL, write report
```

Result is a single verdict ("this config is operable") plus the event timeline
Grafana renders. Steps 1–7 are implemented in `flow.py`; the major PG upgrade
(step 5b) remains a **later** addition.

## Step mechanics (the parts that aren't obvious)

### Credential rotation (step 4)
CNPG keeps the app password in the `<cluster>-app` secret; the dummy app (and
the PgBouncer poolers, via `auth_query`) authenticate with it. Rotation:

1. Update the `password` in the app secret.
2. The operator reconciles it into Postgres (`ALTER ROLE`). With CNPG poolers'
   `auth_query`, the pooler picks up the new password automatically — no
   separate userlist to rotate (a point in favor of the pooler design).

**What the test proves** — the production trap: existing pooled connections keep
working (Postgres doesn't drop authenticated sessions on a password change), but
**new** connections must use the new password. If the app caches the old
credential, new connects start failing. Assertion: error rate stays under SLO
and returns to baseline within N seconds. This is the whole point of the step.

### Minor version upgrade (step 5)
Bump `.spec.imageName` to the new minor (e.g. `16.3 → 16.4`). The operator does
a rolling update — replicas first, then a switchover. Low-risk, always
available. Record the observed version (`SELECT version()` / pod image) so the
console's **PG-version-over-time** panel has a real transition to draw.
Assertion: rolling update completes, app stays under SLO through the switchover.

### Major version upgrade (step 5b — later)
CNPG ≥ 1.26 supports a **declarative offline major upgrade** via `pg_upgrade`
(e.g. `16 → 17`): a real outage window with pre/post validation. Gated on the
operator version and materially riskier, so it's a follow-up step, not in the
first golden path. Deferred by decision.

### Backup / restore (steps 3, 6)
Same concepts core already proves (Barman base backup + WAL + PITR restore into
a second cluster), re-implemented here as plain steps against the real cluster.
Nothing new to design; just linear calls + annotations.

## Event / annotation schema

`flow.py` appends one JSON object per line. The console overlays these as marker
lines on the metric timelines and drives the component-status view.

```jsonc
{"ts": "2026-07-18T14:03:22Z", "step": "backup",  "kind": "backup",  "status": "ok",   "detail": "base backup pg-20260718"}
{"ts": "2026-07-18T14:05:10Z", "step": "rotate",  "kind": "rotate",  "status": "ok",   "detail": "app password rotated"}
{"ts": "2026-07-18T14:07:41Z", "step": "upgrade", "kind": "version", "status": "ok",   "detail": "16.3 → 16.4", "from": "16.3", "to": "16.4"}
{"ts": "2026-07-18T14:09:55Z", "step": "restore", "kind": "restore", "status": "ok",   "detail": "PITR → 14:04:00Z"}
```

`kind` picks the marker style; `from`/`to` feed the version panel. Deliberately
flat and boring — the console needs nothing more.

## Observability + the console

**Stack: Prometheus → Grafana, all in-cluster** (k8s-native, same substrate).
This replaces the earlier "build our own web app" plan — one mature tool instead
of hand-rolled panels. Grafana's annotation API + state-timeline/node-graph
panels are exactly the pieces we lean on (Perses, the permissive alternative, is
weaker there — so Grafana wins on simplicity).

Metric sources (two, one substrate):

- **DB metrics** — the CNPG `:9187` Prometheus endpoint.
- **App metrics** — the dummy app exposes `/metrics` (ops/s, error %, p99, live
  connection count). App-perspective is the truth that matters.

Prometheus scrapes both; Grafana reads Prometheus. (If we want the metrics to
*also* land in your external OTEL endpoint, add an OTEL collector that scrapes
the same targets and exports OTLP — additive, not required for the console.)

The Grafana dashboard (provisioned as JSON, in-cluster) shows:

- **Metric panels** — app ops/error/latency + DB metrics, side by side.
- **Event-annotation timeline** — `flow.py` POSTs a Grafana **annotation** at
  each step (backup/rotate/upgrade/restore), so every panel gets vertical marker
  lines for free. You *see* "app dipped exactly when we rotated, recovered in 4s."
  (`events.jsonl` stays the local record; the annotation API is the render path.)
- **PG-version-over-time** — a native **state-timeline** panel fed by a
  `pg_version` gauge (or the `version` annotations).
- **Component status (SCADA)** — v1 uses Grafana's **Canvas / Node Graph** panel
  driven by per-instance `up`/role/connection metrics: client → poolers →
  primary → replicas with health color. If that isn't clear enough, v2 is a small
  bespoke page reusing the topology graph the core TUI already computes — but we
  try the built-in panel first (simplicity).

So `flow.py`'s only console responsibility is: expose app `/metrics`, and POST an
annotation per step. Everything else is Grafana config.

## Sequencing (as built)

1. **Skeleton + golden path** — `manifests/` + `flow.py` doing steps 1–7 with
   text/JSONL output. **Done.**
2. **Console** — Prometheus + Grafana in-cluster (`monitoring/`), dashboards-as-JSON,
   `flow.py` posting step annotations. **Done.**
3. **Major upgrade** (step 5b) — the `pg_upgrade` path, operator-version gated.
   Still deferred.

## Resolved design questions

- **Dummy app** — reuses the existing `k8os-loadgen` (app-perspective metrics),
  not a bespoke app.
- **Provisioning** — the testbed **self-provisions everything**: the CNPG
  operator, the cluster, and the in-cluster Prometheus/Grafana. See
  [pg/testbed/README.md](../pg/testbed/README.md).
