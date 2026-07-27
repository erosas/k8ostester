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
    # rate-of-change / urgency: linear-extrapolate recent growth to a time-to-exhaustion.
    # A level ("80% full") says nothing about urgency; this says "full within {v}h".
    # (Disk only — its growth is monotonic; memory working-set sawtooths under GC, so a
    # linear projection there is unreliable and would false-alarm.)
    "disk_fill": (
        "disk-fill", "DiskFillingSoon",
        'predict_linear(kubelet_volume_stats_used_bytes{{persistentvolumeclaim=~"{pods}"}}[6h], {v}*3600)'
        ' > kubelet_volume_stats_capacity_bytes{{persistentvolumeclaim=~"{pods}"}}',
        "a volume is projected to fill within {v}h at its recent growth rate",
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
    "cpu": "cpu", "memory": "memory", "disk": "disk", "disk_fill": "disk",
    "txid": "xid", "long_txn": "longtxn", "conn_age": "connage",
}

# goals where a LOW value is the bad one — "hours until exhaustion", so the dashboard
# waterline colours red *below* the line (opposite of a level threshold).
INVERTED_GOALS = {"disk_fill"}


def num(x: object) -> float | int | None:
    """Parse a goal value; None (skip) if blank/invalid. Ints stay ints."""
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return int(v) if v == int(v) else v


def cpu_cores(s: object) -> float:
    """A k8s CPU quantity ('100m', '2') as cores."""
    t = str(s).strip()
    return float(t[:-1]) / 1000 if t.endswith("m") else float(t or 0)


_MEM_GI = {"Ki": 1 / 1048576, "Mi": 1 / 1024, "Gi": 1.0, "Ti": 1024.0}


def mem_gi(s: object) -> float:
    """A k8s memory quantity ('256Mi', '2Gi') as GiB."""
    t = str(s).strip()
    for unit, factor in _MEM_GI.items():
        if t.endswith(unit):
            return float(t[:-2] or 0) * factor
    return float(t or 0) / 1073741824   # bare bytes


def goal_threshold(key: str, value: object, opts: dict) -> float | int | None:
    """The absolute threshold an alert/waterline uses, from the UI value. CPU and
    memory are entered as a **percent of the configured limit**; transaction-ID age
    in **millions** of xids; everything else is already absolute (seconds / count / %)."""
    v = num(value)
    if v is None:
        return None
    if key == "cpu":
        return round(v / 100 * cpu_cores(opts.get("cpu") or "100m"), 3)
    if key == "memory":
        return round(v / 100 * mem_gi(opts.get("memory") or "256Mi"), 3)
    if key == "txid":
        return int(v * 1_000_000)
    return v


def clamp(value: object, lo: int, hi: int, default: int) -> int:
    """Coerce ``value`` to an int in [lo, hi]; ``default`` if it isn't a number."""
    try:
        return max(lo, min(hi, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
