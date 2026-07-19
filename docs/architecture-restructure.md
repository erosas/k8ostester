# Architecture restructure — kernel + verticals

Status: **design, for review** (no code yet). Supersedes the general-framework
shape of `k8ostester-core` with a thinner, simpler architecture.

## Why

Today `k8ostester-core` is a **framework**: a `driver` / `worker` / `goal`
abstraction every technology must implement, plus a generic experiment runner,
goals-evaluator, and HTML report. That abstraction pays off only with several
technologies actively pulling on it. There is one deeply-built technology
(PostgreSQL/CNPG), so the abstraction is mostly *speculative generality* — an
indirection tax for Kafka/OpenSearch support that doesn't exist yet.

The evidence is `pg/testbed`: it deliberately does **not** use the framework —
it's a linear, PG-specific script — and it's been the clearest, most productive
part of the project. That's the signal.

Guiding principle: **share primitives, not abstractions.**
- A `kill_pod()` / `partition()` helper, the k8s client, the Grafana console —
  *primitives*. Reusable, no lock-in.
- A `TechnologyDriver` interface PG, Kafka, and OpenSearch must all satisfy —
  an *abstraction*. It forces premature commonality; every tech fights the shape.

Kernel = primitives. Verticals = direct, tech-specific logic.

## Two moves

1. **Structure** — a thin shared kernel + one vertical per technology (uv workspace).
2. **Model** — dissolve the generic experiment engine into: a linear step
   script + a shared Prometheus/Grafana console + inline correctness verifies +
   an end-of-run SLO-query verdict.

---

## 1. Module layout (uv workspace)

```
k8ostester/                    ← repo root, uv workspace
  kernel/                      ← THIN: primitives only, NO driver abstraction
    k8s client · chaos primitives (kill/partition/drain)
    console: shared Prometheus + Grafana (dashboards-as-code)
    SLO-verdict helper · discovery + capability model
  pg/                          ← everything PostgreSQL, direct and specific
    CNPG deploy · experiments (linear scripts) · the testbed
    PG ops (rotate/upgrade/PITR) · PG verify-steps · PG Grafana panels
  kafka/       (future vertical)
  opensearch/  (future vertical)
```

- uv workspace **members**; `pg` depends on `kernel`. IntelliJ/PyCharm reads
  this as real modules with dependency edges and one interpreter — no path hacks.
- **The rule that stops the kernel rotting back into today's core:** the kernel
  holds primitives + the console and must **never** grow an "every tech
  implements this" interface. Verticals write tech-specific logic *directly*
  against kernel primitives, the way the testbed already does. Kafka's ISR checks
  and PG's PITR have nothing in common — don't pretend they do.

### Kernel (thin, stable, shared)
- `ClusterClient`, helm wrapper, namespace lifecycle.
- Chaos primitives: `kill_pod`, `partition`, `drain_zone`.
- **The console** — a shared, *persistent* Prometheus + Grafana (see §3).
- **SLO-verdict helper** — run Prometheus range queries over a window vs.
  thresholds → pass/fail (see §2).
- Discovery + capability model for the interactive console (see
  [remote-control.md](remote-control.md)).

### Vertical (direct, tech-specific)
- The tech deploy (CNPG cluster, poolers).
- Linear step scripts — *the* experiments.
- Correctness verify-steps (RPO, integrity, PITR) — data comparisons.
- Tech-specific Grafana panels + chaos/ops actions.

---

## 2. The experiment model (the dissolved engine)

An experiment becomes a **linear sequence of steps with delays** (the `flow.py`
model): deploy → drive load → inject fault at T → … Everything else is sourced,
not reimplemented:

| Old generic-engine piece | Replaced by |
| --- | --- |
| goals-evaluator (thresholds) | Prometheus range queries at end-of-run |
| loadgen journal metrics | Prometheus (experiment-labeled) |
| custom HTML report | Grafana dashboards |
| comparison-report / groups | Grafana: labeled metrics + template vars + metric math (§3) |
| verify (RPO / integrity / PITR) | **stays** — inline verify-steps |
| experiment runner | the linear step script |

**The verdict** at end-of-run = **(all inline verifies pass) AND (no SLO
range-query breached).**

### Live alerts vs. the post-run verdict (the key distinction)
Don't source the verdict from Grafana *alerts*. Grafana alerts are for a human
watching live ("SLO violated right now") — the ops/SCADA use. A repeatable *test
verdict* is a batch question asked once at the end: "over `[start,end]`, did
error_rate ever exceed 1%? was p99 under 200ms in the steady window?" — a
**Prometheus range query with a threshold**, not a streaming alert. Both read
the same metrics; they serve different masters.

| | Source | Consumer |
| --- | --- | --- |
| Live SLO alerting | Grafana alerts | operator at the console |
| Repeatable verdict | Prometheus range queries at end-of-run | CI / test result |
| Correctness | inline verify-steps | test result |

### The SLO-verdict helper (kernel)
Small — the ~dozen lines that replace the whole goals-evaluator. Given a window
and a list of checks `{promql, threshold, direction}`, evaluate each over
`[start,end]` and combine with the verify results into a per-run verdict (JSON).

```jsonc
{ "run": "...", "experiment": "20-cnpg-reference", "verdict": "pass",
  "verifies": {"rpo": true, "integrity": true, "pitr": true},
  "slo": {"error_rate": {"observed": 0.002, "threshold": 0.01, "pass": true},
          "downtime_s":  {"observed": 0,     "threshold": 55,   "pass": true}} }
```

---

## 3. Metrics, labeling & cross-run comparison

Comparison stops being a bespoke report and becomes a **configurable Grafana
dashboard** — more powerful, because it's interactive (side-by-side, overlay,
diff) where a static report isn't.

### Enabler: label every series with experiment/run identity
Don't touch the exporters. Put the identity as a **Kubernetes label** on the
run's pods/namespace, and have Prometheus **relabel** it onto every scraped
series:

```yaml
relabel_configs:
  - source_labels: [__meta_kubernetes_pod_label_k8ostester_io_experiment]
    target_label: experiment
  - source_labels: [__meta_kubernetes_namespace]
    target_label: run
```

Now **all** metrics — app and CNPG DB — carry `experiment=…`, `run=…` with zero
exporter changes.

### Grafana comparison (native)
- **Template variable** `$exp = label_values(app_ops_total, experiment)`,
  multi-select → the configurable picker.
- **Overlay:** `sum by (experiment) (rate(app_ops_total{result="err",
  experiment=~"$exp"}[1m]))` → one line per experiment.
- **Side-by-side:** repeat a panel/row by `$exp` → one tile per experiment.
- **Diff:** PromQL math — `max(slo_p99{experiment="A"}) -
  max(slo_p99{experiment="B"})`, or a table joined on `experiment`; ratios for
  "A is 1.4× B."

### The one architectural consequence: shared, persistent Prometheus
To compare run A vs. run B, both runs' metrics must coexist — so the metrics
store **cannot** be spun up/torn down inside each run's namespace (as the testbed
does today). It moves to the **kernel/console layer**: installed once per
environment, in a shared namespace, outliving runs. Runs emit into it, labeled.
Retention (7–30 days) manages the `run`-label cardinality. This is a feature —
one console shows every run, live and historical.

**RBAC note (review):** because it scrapes pods across *every* run namespace, the
shared Prometheus needs a **ClusterRole** (cluster-wide pod/endpoint discovery),
not the namespaced `Role` the per-run testbed Prometheus uses today. The
`experiment`/`run` labels come from relabeling the discovered pods' namespace and
labels — so no per-workload scrape config.

---

## What survives, what dissolves

- **Survives:** the verdict concept, correctness verifies, repeatability, and
  cross-config comparison (now better).
- **Dissolves:** the driver/goal/report framework, the custom goals-evaluator,
  the loadgen-journal metric path, the custom HTML report, and per-run monitoring.

What's left: **linear step scripts (verticals) + a shared Prometheus/Grafana
console (kernel) + inline correctness verifies + a tiny SLO-query verdict.**

---

## Migration (incremental; suite stays green each step)

1. **Stand up the shared console** — Prometheus + Grafana in a kernel namespace,
   with the experiment/run relabeling. Purely additive.
2. **Add the SLO-verdict helper.** Port a couple of existing goals to Prometheus
   range queries; run alongside the current evaluator to confirm parity.
3. **Extract kernel primitives** (k8s client, chaos) from `k8ostester-core` into
   `kernel/` as a workspace member. To avoid breaking core mid-extraction, make
   **core depend on kernel** (import the moved primitives from it) during the
   transition — nothing breaks, and PG code migrates to the `pg` vertical
   gradually. (Done so far: the workspace + `kernel` module + the SLO-verdict
   helper — step 2's primitive — landed first, as it needs no live cluster.)
4. **Move** `postgres_cnpg` + experiments + the testbed into `pg/`; wire the uv
   workspace.
5. **Convert experiments**, one at a time, from the fault-timeline/goals format
   to linear step scripts + verify-steps + SLO checks.
6. **Retire** the generic runner/goals/report once all experiments are ported.
7. *(Optional)* keep a thin "collect per-run verdicts → summary table" layer if
   you want comparison outside Grafana; otherwise rely on Grafana.

---

## Open questions

- Keep a thin comparison-summary collector, or rely purely on Grafana? (Leaning:
  Grafana, plus a tiny optional collector.)
- Console lifecycle — does k8ost install the shared Prometheus/Grafana per
  environment, or assume it's present (attach-style)?
- How much of the current `experiments/` suite is worth porting vs. letting the
  testbed + a few reference experiments cover it?

## Related

- [productionization.md](productionization.md) — the testbed becomes the `pg`
  vertical's golden-path testbed.
- [remote-control.md](remote-control.md) — the interactive console; shares the
  kernel primitives + discovery/capability model.
