# Productionization testbed — design

Status: **design, for review** (no code yet). This describes a second module,
`k8os-testbed`, separate from `k8ostester-core`.

## Why a second module

The two have different jobs and should not be forced to share a model:

| | `k8ostester-core` | `k8os-testbed` (new) |
| --- | --- | --- |
| Purpose | explore fast, find better configs | prove **the one ideal config** is operable |
| Shape | generic chaos engine (drivers/workers/goals) | a **linear, pre-determined golden path** |
| Question it answers | "which config survives chaos?" | "can I run this in production, and automate it?" |
| Runs on | k8s | k8s (same substrate — no docker-compose) |

Code reuse is **explicitly not a goal**. `k8os-testbed` copies the ideal config
and re-implements what it needs as a simple script, rather than bending core's
abstractions. Simplicity over DRY.

## Principles

- **k8s-native.** The DB runs on your operator (as it must, to be real).
  Everything else — the dummy app, the OTEL collector, the console — deploys as
  ordinary k8s workloads. One substrate.
- **Linear.** The flow reads top-to-bottom. No engine, no plugin registry.
- **Owned, self-contained console.** No Grafana (AGPL — see the license policy).
  A small web view we own, reading OTEL metrics + our own event stream.
- **Single config.** It tests `20-cnpg-reference` (the ideal), nothing else.

## Module layout

```
k8os-testbed/
  manifests/
    cluster.yaml        # the ideal config (from 20-cnpg-reference)
    app.yaml            # the dummy application (reads + writes, exports OTEL)
    otel-collector.yaml # scrapes DB metrics, receives app metrics → console
  flow.py               # THE golden path, linear, top to bottom
  events.jsonl          # what flow.py emits (the console's annotation source)
  console/              # self-contained web SCADA view (later phase)
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
the console renders. Major PG upgrade (step 5b) is a **later** addition.

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

## Observability + the console (later phase)

Two metric sources, one substrate:

- **DB metrics** — the CNPG `:9187` Prometheus endpoint, scraped by the in-cluster
  OTEL collector (exactly like experiment 19).
- **App metrics** — the dummy app exports OTLP directly (ops/s, error %, p99,
  live connection count). App-perspective is the truth that matters.

The **console** is a self-contained web view (owned, no Grafana) showing:

- **SCADA-style live component status** — the topology (client → poolers →
  primary → replicas) with per-node health color. This is the same topology graph
  the core TUI already computes, rendered for the web.
- **Metric panels** — app ops/error/latency + DB metrics, side by side.
- **Event-annotation timeline** — vertical markers for backup/rotate/upgrade/
  restore from `events.jsonl`, so you *see* "app dipped exactly when we rotated,
  recovered in 4s."
- **PG-version-over-time** — a step line from the `version` events.

**Open decision (defer to the console phase):** the web stack. Options range from
a static SPA polling the k8s API + Prometheus, to a tiny Python server (SSE for
live events + a metrics proxy) serving the SPA. Chosen when we build it; the
event schema above is stable regardless.

## Sequencing

1. **Skeleton + golden path** (this design → code): `manifests/` + `flow.py`
   doing steps 1–7 with text/JSONL output. Proves the operations run end to end.
2. **Console**: the SCADA web view over `events.jsonl` + OTEL.
3. **Major upgrade** (step 5b): the `pg_upgrade` path, operator-version gated.

## Open questions

- Dummy app: extend the existing loadgen (add OTLP export) or a purpose-built
  tiny app? Leaning reuse-the-concept, simplest export.
- Console web stack (above) — decided at phase 2.
- Does the testbed self-provision the operator + OTEL collector, or assume the
  cluster already has them (like the attach scenario)?
