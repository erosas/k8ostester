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

CNPG_ACTIONS: list[Action] = [
    # --- ops: mutations you want -------------------------------------------
    Action("backup", "Take base backup", "ops",
           lambda s: s["ready"] and s["backup_configured"]),
    Action("restore", "Restore (PITR)", "ops",
           lambda s: s["backups_completed"] > 0 and s["pitr_window"],
           destructive=True),
    Action("rotate", "Rotate credentials", "ops",
           lambda s: s["ready"] and s["blue_green"]),
    Action("upgrade", "Minor upgrade", "ops",
           lambda s: s["ready"] and bool(s["target"])
           and s["version"] != s["target"] and not s["upgrading"]),
    # --- chaos: mutations that test it -------------------------------------
    Action("kill-primary", "Kill primary", "chaos",
           lambda s: bool(s["primary"]) and not s["fault_in_flight"],
           destructive=True),
    Action("partition-primary", "Partition primary", "chaos",
           lambda s: bool(s["primary"]) and not s["fault_in_flight"],
           destructive=True),
    Action("kill-replica", "Kill a replica", "chaos",
           lambda s: bool(s["replicas"]) and not s["fault_in_flight"],
           destructive=True),
    Action("drain-zone", "Drain a zone", "chaos",
           lambda s: len(s["zones"]) >= 2 and not s["fault_in_flight"],
           destructive=True),
]
