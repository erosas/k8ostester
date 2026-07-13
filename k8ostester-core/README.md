# k8ostester

`k8ost` — validate Kubernetes configurations of stateful technologies (Postgres
first) against explicit resilience and performance goals: run a config under
load, inject faults (kill the primary, partition a node…), verify data integrity
and backup/PITR, and get a pass/fail verdict per goal.

This is the installable framework. The experiment suite, docs, and container
build/publish flow live in the [project repository](https://github.com/erosas/k8ostester).

```bash
uv tool install --editable ./k8ostester-core   # installs the `k8ost` CLI
k8ost env check                                 # what can this cluster do?
k8ost run                                        # pick an experiment, watch it in the TUI
```

MIT licensed.
