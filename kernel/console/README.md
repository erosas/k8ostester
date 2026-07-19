# Shared console (kernel)

A **shared, persistent** Prometheus + Grafana, installed **once per environment**
— not per run. Because it outlives individual runs, metrics from different runs
coexist and can be compared. This is the metrics/comparison half of the console
(the interactive control plane is a separate piece — see
[docs/remote-control.md](../../docs/remote-control.md)).

```bash
kubectl apply -f prometheus.yaml       # namespace k8os-console + shared Prometheus
kubectl apply -f grafana.yaml          # Grafana + the comparison dashboard
kubectl -n k8os-console port-forward svc/grafana 3000:3000   # → localhost:3000 (admin/admin)
```

## How runs feed it

Prometheus scrapes pods across **all** namespaces (a ClusterRole) and relabels
every series:

- `experiment` ← the pod label `k8ostester.io/experiment`
- `run`        ← the pod's namespace

So a run just needs to **label its workloads** with
`k8ostester.io/experiment: <name>` and deploy into its own namespace. No
per-run monitoring, no per-run scrape config.

## Comparing runs

The `compare` dashboard has an `$exp` template variable (multi-select over the
`experiment` label). Panels **overlay** experiments, **repeat** side-by-side, and
a **table** diffs their peak error rate. This replaces the old custom
comparison-report — configurable and interactive. See
[docs/architecture-restructure.md](../../docs/architecture-restructure.md).

## Not yet validated live

These manifests are authored and statically checked (YAML + dashboard JSON
parse). They still need a live apply to confirm the cross-namespace discovery,
relabeling, and dashboard render — next time a multi-run cluster is up. Retention
is 30d; tune for your cardinality.
