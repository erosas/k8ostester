#!/usr/bin/env python3
"""Experiment: kill the primary — does CNPG fail over losing nothing?

A linear experiment in the new model: deploy (via the pg harness) → load → chaos
→ verify → verdict. The verdict is assembled by the kernel from inline verify-
steps (correctness) + SLO range-queries over the run window. See
docs/architecture-restructure.md.

    python pg/experiments/kill-primary/run.py --context <ctx> --prometheus <url>

Needs the CNPG operator + the shared console (kernel/console).
"""
from __future__ import annotations

import argparse
import time

from k8ostester_kernel import Run, chaos
from k8ostester_kernel.k8s import ClusterClient, wait_until
from k8ostester_kernel.verdict import prometheus_fetcher
from k8ostester_pg import harness
from k8ostester_pg.slo import default_checks

EXPERIMENT = "kill-primary"
NS = "exp-kill-primary"


def main() -> int:
    ap = argparse.ArgumentParser(description="kill-primary resilience experiment")
    ap.add_argument("--context", help="kube context")
    ap.add_argument("--prometheus", default="http://localhost:9090",
                    help="shared-console Prometheus (port-forward it)")
    args = ap.parse_args()

    k8s = ClusterClient(args.context)
    run = Run(EXPERIMENT)

    # 1. deploy the ideal config + app (harness handles labelling, bucket, health)
    harness.deploy_ideal_config(k8s, NS, EXPERIMENT)
    run.event("ready", "ideal config healthy (3/3)")

    # 2. steady baseline — the app drives continuous read/write load
    time.sleep(60)

    # 3. chaos: kill the primary
    primary = harness.cluster_field(k8s, NS, "currentPrimary")
    chaos.kill_pod(k8s, NS, primary)
    run.event("chaos", f"killed primary {primary}")

    # 4. recovery + correctness verifies. Wait for the FAILOVER first (a new
    #    primary) — readyInstances can read 3 from stale status before the
    #    promotion lands — then all instances back.
    failed_over = wait_until(
        lambda: harness.cluster_field(k8s, NS, "currentPrimary") not in ("", primary),
        timeout=300, desc="failover to a new primary") is not None
    run.verify("primary_moved", failed_over)
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
