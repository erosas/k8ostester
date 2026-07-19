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
# Mutating ops also require ``not busy`` — the exclusivity lock, so you can't
# stack e.g. a PITR restore and a minor upgrade. Chaos faults deliberately skip
# the lock: they stay available during an operation (the UI asks for an ack).
CNPG_ACTIONS: list[Action] = [
    # --- ops: routine on-call, low-risk ------------------------------------
    Action("backup", "Take base backup", "ops",
           lambda s: s.get("ready") and s.get("backup_configured") and not s.get("busy")),
    Action("rotate", "Rotate credentials", "ops",
           lambda s: s.get("ready") and s.get("blue_green") and not s.get("busy")),
    # --- chaos: destructive / high-risk / infrequent -----------------------
    # restore (creates a cluster) and minor upgrade (high-risk, rare) live here
    # with the faults — anything past a credential rotation is break-glass.
    Action("restore", "Restore (PITR)", "chaos",
           lambda s: s.get("backups_completed", 0) > 0 and s.get("pitr_window")
           and not s.get("busy"),
           destructive=True),
    # Minor upgrade: always available when healthy; the target image is chosen at
    # press time (no --target flag needed), so there's nothing to gate on beyond
    # "not already upgrading / busy".
    Action("upgrade", "Minor upgrade", "chaos",
           lambda s: s.get("ready") and not s.get("upgrading") and not s.get("busy"),
           destructive=True),
    # Generic per-pod faults: the fault picker passes the target pod, so one
    # action each covers the primary AND any replica (kill or partition either).
    Action("kill-pod", "Kill a pod", "chaos",
           lambda s: bool(s.get("primary") or s.get("replicas"))
           and not s.get("fault_in_flight"),
           destructive=True),
    Action("partition-pod", "Partition a pod", "chaos",
           lambda s: bool(s.get("primary") or s.get("replicas"))
           and not s.get("fault_in_flight"),
           destructive=True),
]
