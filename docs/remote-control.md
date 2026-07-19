# Remote-control console — design

Status: **walking skeleton working.** End to end: `kernel/control.py` (capability
model) + `pg/control.py` (8 CNPG actions) + `pg/discover.py` (state snapshot +
topology) + `pg/execute.py` (gated executor, chaos + backup wired) + `pg/server.py`
(stdlib SSE server) + `pg/console.html` (the SPA: live SCADA topology, Ops/Chaos
tabs, capability-gated buttons, destructive confirmation). Run it:

    uv run python -m k8ostester_pg.server --context <ctx> --namespace <ns> --target 16.6

All 8 actions are wired (`pg/ops.py`: rotate/upgrade/restore + the chaos
primitives), with live operation status and self-enabling controls. Tabs follow
the on-call model: **Ops** = routine (backup, rotate) · **Break-glass** =
destructive/high-risk (restore, upgrade, kill, partition, drain). Verified live:
rotate ran end to end from the button (blue/green, new cred authenticates).
Remaining polish: richer topology (pooler routing, replication lag), a PITR time
picker, and packaging the console entry point.

A web control plane for a live cluster: connect over a kubeconfig, **discover**
its state, and drive both **ops** (backups, PITR restore, credential rotation,
version upgrades) and **chaos** (kill, partition, AZ-drain) from a polished UI.
It is `k8ost session --attach` grown up — the TUI session's chaos control plane,
plus lifecycle operations, behind a real interface.

## Why / what it is not

- It **operates and attacks a real cluster** — it is not a dashboard. Grafana
  (read-only viz) is the wrong vehicle; this mutates state, so the UI is owned
  and interactive. The read-only telemetry can still live in Grafana beside it.
- It **discovers** — nothing about the target is hardcoded. The panel is a
  function of what's actually in the cluster right now.
- It is **remote-first** — laptop-side, holding your kubeconfig (like the
  `k8ost-docker` shim), pointed at any cluster.

## The core idea: capability = precondition over discovered state

The single design decision everything hangs on. A control is **not** tracked as
"used / unused." Every action declares a **precondition evaluated against live
discovered state**, and a control is enabled *iff* its precondition holds now.
"Disable after use" and "multi-use" then fall out of one rule — no special cases:

| Action | Tab | Precondition (over discovered state) | Resulting behavior |
| --- | --- | --- | --- |
| Take base backup | ops | `spec.backup` configured | multi-use |
| Restore (PITR) | ops | ≥1 completed backup **and** a WAL window exists | enabled once a backup exists |
| Rotate credentials | ops | cluster Ready **and** blue/green roles present | **multi-use** (precondition always holds) |
| Minor upgrade | ops | newer minor available **and** `current ≠ target` **and** phase ≠ Upgrading | **self-disables after use** (now `current == target`); re-enables when a newer minor appears |
| Major upgrade | ops | operator ≥ 1.26 **and** newer major available **and** no upgrade in flight | self-disables after use |
| Kill primary / replica | chaos | target pod exists **and** no conflicting fault in flight | per-target |
| Partition | chaos | target exists **and** a partition engine is available | per-target |
| AZ-drain | chaos | ≥2 zones present **and** a drainable zone | enabled only on multi-AZ |

Why this beats a used-flag:

- **Reload-safe** — the UI is a pure function of cluster state; refresh loses nothing.
- **Correct under concurrency** — if another operator (or an external process)
  upgrades, the button disables for you too, because the *cluster* changed.
- **Honest** — it mirrors the plant, exactly like a SCADA interlock. The button
  isn't "spent"; the goal is simply met.

The upgrade case is the proof: it disables not because we remember clicking it,
but because `current == target` is now true. Bump the target and it lights up
again. Nothing to reset.

## Discovered state (one snapshot, both tabs)

A poller reads the cluster on an interval and produces a snapshot the whole UI
renders from:

```jsonc
{
  "cluster": {"name": "pg", "phase": "Cluster in healthy state", "instances": 3, "ready": 3},
  "topology": [   // client → poolers → primary → replicas, with zone + health
    {"id": "pg-1", "role": "primary", "zone": "us-east-1a", "health": "ok"},
    {"id": "pg-2", "role": "replica", "zone": "us-east-1b", "health": "ok", "syncState": "sync", "lagBytes": 0},
    {"id": "pg-pooler", "role": "pooler-rw", "instances": 2}
  ],
  "version": {"current": "16.4", "target": "16.6", "upgrading": false},
  "backups": [{"name": "backup-...", "phase": "completed", "startedAt": "..."}],
  "pitrWindow": {"from": "12:01:33Z", "to": "now"},
  "inFlight": null,          // e.g. {"op": "upgrade", "progress": "2/3 rolled"}
  "app": {"up": true, "errorRate": 0.0}   // if the target app exposes metrics
}
```

`inFlight` and `phase` drive both the **progress display** and the
**interlocks** (a precondition includes "no conflicting op in flight"). Every
capability's `enabled` is computed **server-side** from this snapshot (so a stale
browser can't fire a disabled action) and also sent down for rendering.

## Two tabs, split by intent

- **Ops** — the mutations you *want*: the SCADA topology (client → poolers →
  primary → replicas, colored by health, grouped by zone), the backup/snapshot
  list + PITR window with a restore control, the version panel + upgrade control,
  replication lag + sync state. Every control gated by its precondition.
- **Chaos** — the mutations that *test* it: kill primary/replica, partition,
  AZ-drain, with a target picker and live blast-radius (the app's error rate from
  its metrics). This is the session's chaos, polished.

Same discovered snapshot underneath; the split is intent (operate vs. attack).

## Architecture

```
kubeconfig ──▶ backend (laptop-side, holds the config)
                 ├─ poller        : cluster → state snapshot  ──SSE──▶ browser
                 ├─ capability    : snapshot → {action: enabled?}      (SPA, 2 tabs,
                 └─ executor      : POST /api/action ─▶ kubectl/CNPG    SCADA topology)
```

- **Backend** — a small server that owns the kubeconfig, polls the cluster into
  the snapshot, streams it (SSE/websocket), and accepts `POST /api/action
  {id, params}` which it executes and reflects back through the next poll.
- **UI** — an owned SPA. The SCADA topology view *is* the topology graph core's
  session already computes (`topology_graph`), rendered for the web and made
  interactive. Controls render enabled/disabled from the capability map.
- **Safety** — it hits real clusters: destructive/irreversible actions (major
  upgrade, restore) require a typed confirmation; attach-mode teardown removes
  only k8ost's own artifacts, never the target.

## Reuse vs. new

Most of this already exists:

- **Reuse from core:** attach-mode discovery, `topology_graph`, session actions
  (`backup`, `restore`), fault workers (`network_partition`, `pod_kill`).
- **Reuse from the testbed:** rotate (blue/green), minor/major upgrade, AZ logic
  — but these currently live in `pg/testbed/flow.py` as script steps. To share
  them, they graduate into first-class **driver actions** (the same registry the
  session's `session_actions()` uses).
- **New:** the **capability/precondition layer** (the heart), the **backend
  poller + action API**, and the **web UI**.

## Where it lives (open decision)

It is the session evolved, so the natural home is **`k8ost console` inside
`k8ostester-core`** — reusing the session/driver/worker stack directly. The
alternative is a separate module importing core + testbed. Recommendation:
console in core, with the ops actions (rotate/upgrade) promoted from the testbed
into core driver actions so both the console and the testbed call the same code.

## Sequencing

1. **Design (this doc) → review.**
2. **Walking skeleton:** backend discovers → streams snapshot → SPA renders the
   SCADA topology; wire **one ops action (rotate)** and **one chaos action (kill
   primary)** end to end, with server-side capability gating. Proves the model.
3. **Ops tab:** backups list + PITR restore, version panel + upgrade, lag/sync.
4. **Chaos tab:** partition, AZ-drain, target picker, live blast-radius.
5. **Polish:** confirmations, in-flight progress, interlocks, reconnect.

## Open questions

- Console in core (`k8ost console`) vs. a separate module — leaning core.
- Backend web stack (stdlib + SSE vs. a small framework) — decided at the skeleton.
- Does the app-health/blast-radius panel assume the target app exposes metrics,
  or infer impact from CNPG/connection metrics when it doesn't?
- Multi-cluster (a picker across kubeconfig contexts) — later, or in from the start?
