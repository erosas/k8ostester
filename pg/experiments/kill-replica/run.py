#!/usr/bin/env python3
"""Experiment: kill a replica — is it a non-event? (target architecture)

The contrast to kill-primary: killing an async replica should NOT trigger a
failover and should NOT disturb the app — the primary keeps serving. So this
experiment expects to PASS (no SLO breach, primary unchanged), where kill-primary
breaches the strict SLOs. Same linear model, same harness. See
docs/architecture-restructure.md.

    python pg/experiments/kill-replica/run.py --context <ctx> --prometheus <url>
"""
from __future__ import annotations

import argparse
import time

from k8ostester_kernel import Run, chaos
from k8ostester_kernel.k8s import ClusterClient, wait_until
from k8ostester_kernel.verdict import prometheus_fetcher
from k8ostester_pg import harness
from k8ostester_pg.slo import default_checks

EXPERIMENT = "kill-replica"
NS = "exp-kill-replica"


def main() -> int:
    ap = argparse.ArgumentParser(description="kill-replica non-event experiment")
    ap.add_argument("--context", help="kube context")
    ap.add_argument("--prometheus", default="http://localhost:9090",
                    help="shared-console Prometheus (port-forward it)")
    args = ap.parse_args()

    k8s = ClusterClient(args.context)
    run = Run(EXPERIMENT)

    # 1. deploy the ideal config + app
    harness.deploy_ideal_config(k8s, NS, EXPERIMENT)
    run.event("ready", "ideal config healthy (3/3)")

    # 2. steady baseline
    time.sleep(60)

    # 3. chaos: kill a replica (NOT the primary)
    primary = harness.cluster_field(k8s, NS, "currentPrimary")
    replica = harness.replicas(k8s, NS)[0]
    chaos.kill_pod(k8s, NS, replica)
    run.event("chaos", f"killed replica {replica} (primary {primary} untouched)")

    # 4. recovery + verifies: the primary must NOT change (no failover), and all
    #    instances come back
    recovered = wait_until(
        lambda: harness.cluster_field(k8s, NS, "readyInstances") == "3",
        timeout=300, desc="replica rejoined") is not None
    run.verify("recovered", recovered)
    run.verify("primary_unchanged", harness.cluster_field(k8s, NS, "currentPrimary") == primary)
    time.sleep(60)   # let the SLO window capture the (non-)event

    # 5. verdict — expect PASS: a replica loss is a non-event for the app
    run.finish()
    verdict = run.verdict(prometheus_fetcher(args.prometheus), default_checks(EXPERIMENT))
    return harness.print_verdict(verdict)


if __name__ == "__main__":
    raise SystemExit(main())
