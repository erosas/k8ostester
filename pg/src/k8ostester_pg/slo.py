"""Standard SLO checks for CNPG experiments.

These are the *threshold* goals from the old experiments (error rate, latency,
availability), expressed as kernel ``SloCheck``s evaluated over the run window
as Prometheus range queries. The *correctness* goals — RPO == 0, integrity, PITR
restored exactly — are NOT here: they're data comparisons, so they stay as
inline verify-steps. See docs/architecture-restructure.md.

Metrics come from the app-perspective series the dummy app exposes (labelled
``experiment`` by the shared console's relabeling).
"""
from __future__ import annotations

from k8ostester_kernel.verdict import SloCheck

# defaults mirror 20-cnpg-reference's goals (error_rate 1%, p99 200ms)
DEFAULT_ERROR_RATE = 0.01
DEFAULT_WRITE_P99_S = 0.2


def default_checks(
    experiment: str,
    error_rate: float = DEFAULT_ERROR_RATE,
    write_p99_s: float = DEFAULT_WRITE_P99_S,
) -> list[SloCheck]:
    """The standard SLO gate for a CNPG experiment, scoped to one experiment's
    metrics. Pair with the run's verify-steps to form the verdict."""
    exp = f'experiment="{experiment}"'
    return [
        SloCheck(
            "error_rate",
            f'sum(rate(app_ops_total{{result="err",{exp}}}[1m]))'
            f' / clamp_min(sum(rate(app_ops_total{{{exp}}}[1m])), 1)',
            threshold=error_rate,
            direction="max",
        ),
        SloCheck(
            "write_latency_p99_s",
            f'max(app_last_latency_seconds{{op="write",{exp}}})',
            threshold=write_p99_s,
            direction="max",
        ),
        SloCheck(
            "app_availability",
            f"min(app_up{{{exp}}})",
            threshold=1,
            direction="min",
        ),
    ]
