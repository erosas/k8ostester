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
    # --- resources (cAdvisor / kubelet metrics; pod & PVC labels, not {label}) ---
    "cpu": (
        "cpu", "CpuHigh",
        'max(rate(container_cpu_usage_seconds_total{{container="postgres",pod=~"{pods}"}}[5m])) > {v}',
        "an instance using over {v} CPU cores",
    ),
    "memory": (
        "memory", "MemoryHigh",
        'max(container_memory_working_set_bytes{{container="postgres",pod=~"{pods}"}}) / 1073741824 > {v}',
        "an instance using over {v}Gi memory",
    ),
    "disk": (
        "disk", "DiskHigh",
        'max(kubelet_volume_stats_used_bytes{{persistentvolumeclaim=~"{pods}"}}'
        ' / kubelet_volume_stats_capacity_bytes{{persistentvolumeclaim=~"{pods}"}}) * 100 > {v}',
        "a volume over {v}% full",
    ),
    # --- operational health (CNPG metrics) ---
    "txid": (
        "txid-age", "TxidWraparound",
        'max(cnpg_pg_database_xid_age{{{label}=~"{pods}"}}) > {v}',
        "transaction ID age over {v} (wraparound risk)",
    ),
    "long_txn": (
        "long-txn", "LongTransaction",
        'max(cnpg_backends_max_tx_duration_seconds{{{label}=~"{pods}"}}) > {v}',
        "a transaction running over {v}s",
    ),
    "conn_age": (
        "conn-age", "ConnectionTooOld",
        'max(cnpg_k8ost_conn_oldest_seconds{{{label}=~"{pods}"}}) > {v}',
        "a client connection older than {v}s (recycle it)",
    ),
}


# goal key -> docs/runbooks.md anchor, so each alert can carry a runbook_url
RUNBOOK_ANCHOR = {
    "repl_lag": "repl-lag", "connections": "connsat", "archive_delay": "archive",
    "cpu": "cpu", "memory": "memory", "disk": "disk", "txid": "xid",
    "long_txn": "longtxn", "conn_age": "connage",
}


def num(x: object) -> float | int | None:
    """Parse a goal value; None (skip) if blank/invalid. Ints stay ints."""
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return int(v) if v == int(v) else v


def clamp(value: object, lo: int, hi: int, default: int) -> int:
    """Coerce ``value`` to an int in [lo, hi]; ``default`` if it isn't a number."""
    try:
        return max(lo, min(hi, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
