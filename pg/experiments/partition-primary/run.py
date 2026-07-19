#!/usr/bin/env python3
"""Experiment: network-partition the primary — does it self-fence and fail over?

Where kill-primary is a hard crash, this isolates the primary on the network
(NetworkPolicy deny-all). CNPG's liveness isolation check should self-fence the
partitioned primary so the operator promotes a replica — no split-brain, no lost
writes. Same linear model + harness. See docs/architecture-restructure.md.

    python pg/experiments/partition-primary/run.py --context <ctx> --prometheus <url>

Requires a CNI that ENFORCES NetworkPolicy (Calico/Cilium). On a non-enforcing
CNI (kindnet/docker-desktop) the partition applies but has no effect.
"""
from __future__ import annotations

import argparse
import time

from k8ostester_kernel import Run, chaos
from k8ostester_kernel.k8s import ClusterClient, wait_until
from k8ostester_kernel.verdict import prometheus_fetcher
from k8ostester_pg import harness
from k8ostester_pg.slo import default_checks

EXPERIMENT = "partition-primary"
NS = "exp-partition-primary"


def main() -> int:
    ap = argparse.ArgumentParser(description="partition-primary resilience experiment")
    ap.add_argument("--context", help="kube context")
    ap.add_argument("--prometheus", default="http://localhost:9090",
                    help="shared-console Prometheus (port-forward it)")
    ap.add_argument("--partition-seconds", type=int, default=60,
                    help="how long to hold the partition")
    args = ap.parse_args()

    k8s = ClusterClient(args.context)
    run = Run(EXPERIMENT)

    # 1. deploy the ideal config + app
    harness.deploy_ideal_config(k8s, NS, EXPERIMENT)
    run.event("ready", "ideal config healthy (3/3)")

    # 2. steady baseline
    time.sleep(60)

    # 3. chaos: partition the primary, hold, then heal
    primary = harness.cluster_field(k8s, NS, "currentPrimary")
    chaos.partition_pod(k8s, NS, primary)
    run.event("chaos", f"partitioned primary {primary}")
    # the partitioned primary should self-fence and a replica be promoted
    failed_over = wait_until(
        lambda: harness.cluster_field(k8s, NS, "currentPrimary") not in ("", primary),
        timeout=300, desc="failover away from the partitioned primary") is not None
    run.verify("primary_moved", failed_over)
    time.sleep(args.partition_seconds)
    chaos.heal_partition(k8s, NS, primary)
    run.event("heal", f"healed partition of {primary}")

    # 4. recovery: the old primary rejoins, all instances back
    recovered = wait_until(
        lambda: harness.cluster_field(k8s, NS, "readyInstances") == "3",
        timeout=300, desc="all instances back") is not None
    run.verify("recovered", recovered)
    time.sleep(60)   # let the SLO window capture the recovery

    # 5. verdict = verifies AND SLO range-queries over the run window
    run.finish()
    verdict = run.verdict(prometheus_fetcher(args.prometheus), default_checks(EXPERIMENT))
    return harness.print_verdict(verdict)


if __name__ == "__main__":
    raise SystemExit(main())
