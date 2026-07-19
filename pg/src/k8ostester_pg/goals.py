"""Operational goals / SLOs, defined once.

Each goal turns into three things from a single definition: a **waterline** (a
threshold line on the matching Grafana panel), a **Prometheus alert rule**, and —
because it's the same "metric compared to a threshold" shape the kernel's
``SloCheck`` uses — an experiment pass/fail criterion. dashboard.py reads the
panel + threshold; builder.py reads the alert expr.
"""
from __future__ import annotations

# goal key -> (dashboard panel key, alert name, PromQL expr {pods}/{v}, summary {v})
GOALS: dict[str, tuple[str, str, str, str]] = {
    "repl_lag": (
        "replication-lag", "ReplicationLagHigh",
        'cnpg_pg_replication_lag{{{label}=~"{pods}"}} > {v}',
        "replication lag over {v}s",
    ),
    "connections": (
        "connections", "ConnectionsHigh",
        'sum(cnpg_backends_total{{{label}=~"{pods}"}}) > {v}',
        "connections over {v}",
    ),
    "archive_delay": (
        "archiving", "ArchiveDelayHigh",
        'time() - cnpg_pg_stat_archiver_last_archived_time{{{label}=~"{pods}"}} > {v}',
        "WAL archive delayed over {v}s",
    ),
}


def num(x: object) -> float | int | None:
    """Parse a goal value; None (skip) if blank/invalid. Ints stay ints."""
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return int(v) if v == int(v) else v
