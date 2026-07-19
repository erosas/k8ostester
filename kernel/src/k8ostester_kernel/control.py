"""Capability model for the control plane (see docs/remote-control.md).

The single idea the whole console hangs on: a control is **not** tracked as
used/unused. Each action declares a **precondition** evaluated against the
discovered cluster-state snapshot, and is enabled iff the precondition holds now.
"Disable after use" and "multi-use" then fall out of one rule — an upgrade
self-disables because ``version == target`` is now true, a rotate stays enabled
because its precondition always holds. Reload-safe, concurrency-correct, honest.

This layer is generic: verticals supply the actions (with tech-specific
preconditions) and the discovery that produces the snapshot. It computes the
enabled-map server-side so a stale browser can't fire a disabled action.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# a precondition maps the discovered state snapshot -> is this action enabled?
Precondition = Callable[[dict], bool]


@dataclass(frozen=True)
class Action:
    id: str
    label: str
    tab: str                       # "ops" (operate) or "chaos" (attack)
    precondition: Precondition
    destructive: bool = False      # requires a typed confirmation in the UI
    # Whether the action is *offered at all* for this deployment (vs merely
    # enabled/disabled). Use it to keep a control generic: one that needs extra
    # config — e.g. an upgrade needs a target image — is simply absent when that
    # config wasn't supplied, rather than showing as a permanently-disabled tile.
    available: Precondition = lambda _s: True


def capabilities(actions: list[Action], state: dict) -> list[dict]:
    """The enabled-map for the current discovered state — what the UI renders and
    the server enforces. Actions whose ``available`` is false for this deployment
    are omitted entirely; the rest report ``enabled`` as a pure function of state."""
    return [
        {
            "id": a.id,
            "label": a.label,
            "tab": a.tab,
            "enabled": bool(a.precondition(state)),
            "destructive": a.destructive,
        }
        for a in actions
        if a.available(state)
    ]


def is_enabled(actions: list[Action], action_id: str, state: dict) -> bool:
    """Server-side gate: may this action fire against the current state?"""
    for a in actions:
        if a.id == action_id:
            return bool(a.available(state) and a.precondition(state))
    return False
