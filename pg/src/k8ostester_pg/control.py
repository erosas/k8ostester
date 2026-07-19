"""CNPG control-plane actions for the remote-control console.

The Ops + Chaos actions from docs/remote-control.md, each a kernel ``Action``
whose precondition is evaluated against a discovered CNPG state snapshot. The
console renders/gates them from this list; nothing tracks "used".

Snapshot shape (produced by discovery, not here) — the fields the preconditions
read::

    {
      "ready": bool,                 # cluster healthy
      "primary": str,                # current primary pod ("" if none)
      "replicas": [str, ...],
      "zones": [str, ...],           # distinct node zones in play
      "version": "16.4",             # current PG version
      "target": "16.6",              # a newer version available ("" if none)
      "upgrading": bool,
      "backup_configured": bool,
      "backups_completed": int,
      "pitr_window": bool,           # a WAL window exists
      "blue_green": bool,            # two-role credentials present
      "fault_in_flight": bool,       # a chaos fault is currently active
    }
"""
from __future__ import annotations

from k8ostester_kernel.control import Action

# Preconditions read the snapshot with .get() so a missing/partial field just
# disables the action (the safe default for a control plane) instead of crashing
# the whole capability map.
CNPG_ACTIONS: list[Action] = [
    # --- ops: routine on-call, low-risk ------------------------------------
    Action("backup", "Take base backup", "ops",
           lambda s: s.get("ready") and s.get("backup_configured")),
    Action("rotate", "Rotate credentials", "ops",
           lambda s: s.get("ready") and s.get("blue_green")),
    # --- chaos: destructive / high-risk / infrequent -----------------------
    # restore (creates a cluster) and minor upgrade (high-risk, rare) live here
    # with the faults — anything past a credential rotation is break-glass.
    Action("restore", "Restore (PITR)", "chaos",
           lambda s: s.get("backups_completed", 0) > 0 and s.get("pitr_window"),
           destructive=True),
    Action("upgrade", "Minor upgrade", "chaos",
           lambda s: s.get("ready") and bool(s.get("target"))
           and s.get("version") != s.get("target") and not s.get("upgrading"),
           destructive=True),
    Action("kill-primary", "Kill primary", "chaos",
           lambda s: bool(s.get("primary")) and not s.get("fault_in_flight"),
           destructive=True),
    Action("partition-primary", "Partition primary", "chaos",
           lambda s: bool(s.get("primary")) and not s.get("fault_in_flight"),
           destructive=True),
    Action("kill-replica", "Kill a replica", "chaos",
           lambda s: bool(s.get("replicas")) and not s.get("fault_in_flight"),
           destructive=True),
    Action("drain-zone", "Drain a zone", "chaos",
           lambda s: len(s.get("zones") or []) >= 2 and not s.get("fault_in_flight"),
           destructive=True),
]
