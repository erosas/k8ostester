#!/usr/bin/env python3
"""Experiment: kill the primary — does CNPG fail over losing nothing?

The first experiment in the LINEAR model that replaces the old fault-timeline +
goals engine. It reads top to bottom; the verdict is assembled by the kernel from
inline verify-steps (correctness) + SLO range-queries (thresholds). It reuses the
kernel primitives (ClusterClient, chaos.kill_pod), the CNPG SLO checks
(k8ostester_pg.slo), and the ideal config manifests from pg/testbed.

    python pg/experiments/kill-primary/run.py --context <ctx> --prometheus <url>

Needs a cluster with the CNPG operator + the shared console (kernel/console) so
the app's experiment-labelled metrics are queryable. See
docs/architecture-restructure.md. This is authored/static-checked, not yet live.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from k8ostester_kernel import Run, chaos
from k8ostester_kernel.k8s import ClusterClient, wait_until
from k8ostester_kernel.verdict import prometheus_fetcher
from k8ostester_pg.slo import default_checks

EXPERIMENT = "kill-primary"
NS = "exp-kill-primary"
MANIFESTS = Path(__file__).parents[2] / "testbed" / "manifests"   # the ideal config
CNPG = ("postgresql.cnpg.io", "v1", "clusters")


def cluster_status(k8s: ClusterClient, field: str) -> str:
    obj = k8s.custom.get_namespaced_custom_object(*CNPG[:2], NS, CNPG[2], "pg")
    return str(obj.get("status", {}).get(field, ""))


def main() -> int:
    ap = argparse.ArgumentParser(description="kill-primary resilience experiment")
    ap.add_argument("--context", help="kube context")
    ap.add_argument("--prometheus", default="http://localhost:9090",
                    help="shared-console Prometheus (port-forward it)")
    args = ap.parse_args()

    k8s = ClusterClient(args.context)
    run = Run(EXPERIMENT)

    # 1. deploy the ideal config + app, labelled so the console scopes its metrics
    k8s.create_namespace(NS, labels={"k8ostester.io/experiment": EXPERIMENT})
    for m in sorted(MANIFESTS.glob("*.yaml")):
        k8s.apply_manifests(m, NS)
    run.event("deploy", "ideal config + app applied")
    wait_until(lambda: cluster_status(k8s, "readyInstances") == "3", timeout=600,
               desc="cluster healthy")
    run.event("ready", "cluster healthy (3/3)")

    # 2. steady baseline — the app drives continuous read/write load
    time.sleep(60)

    # 3. chaos: kill the primary (kernel primitive)
    primary = cluster_status(k8s, "currentPrimary")
    chaos.kill_pod(k8s, NS, primary)
    run.event("chaos", f"killed primary {primary}")

    # 4. recovery + correctness verifies (data comparisons, not thresholds)
    healthy = wait_until(lambda: cluster_status(k8s, "readyInstances") == "3",
                         timeout=300, desc="failover recovery") is not None
    run.verify("recovered", healthy)
    run.verify("primary_moved", cluster_status(k8s, "currentPrimary") != primary)
    time.sleep(60)   # let the SLO window capture the recovery

    # 5. verdict = verifies AND SLO range-queries over the run window (from the
    #    app's experiment-labelled metrics in the shared console's Prometheus)
    run.finish()
    verdict = run.verdict(prometheus_fetcher(args.prometheus), default_checks(EXPERIMENT))

    print(f"\n{EXPERIMENT}: {verdict['verdict'].upper()}")
    for name, r in verdict["slo"].items():
        print(f"  slo   {'✓' if r['pass'] else '✗'} {name}: {r['observed']:.4g} "
              f"({r['direction']} {r['threshold']})")
    for name, ok in verdict["verifies"].items():
        print(f"  check {'✓' if ok else '✗'} {name}")
    return 0 if verdict["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
