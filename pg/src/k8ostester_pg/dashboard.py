"""Generate a Grafana dashboard that matches what the Builder configured.

The panel set adapts to the manifest — replication-lag only for a multi-instance
cluster, WAL archiving only when backups are on — so the dashboard is a function
of the config, not a fixed template. Panels live as JSON fragments in
``resources/dashboard/`` (``__CLUSTER__`` is our placeholder; Grafana's own
``${datasource}`` vars are left intact); this module picks the relevant ones,
lays them out, and wraps them in the dashboard envelope. Targets query CNPG's
default Prometheus metrics, so it assumes the PodMonitor is enabled.
"""
from __future__ import annotations

import json
from importlib import resources

from k8ostester_pg.builder import _clamp
from k8ostester_pg.goals import GOALS, num

_PANEL_GOAL = {panel: key for key, (panel, *_rest) in GOALS.items()}   # panel key -> goal key


def _text(name: str) -> str:
    return resources.files("k8ostester_pg").joinpath("resources", name).read_text()


def _waterline(panel: dict, value: float) -> None:
    """Draw the goal as a red threshold line on a timeseries panel."""
    d = panel.setdefault("fieldConfig", {}).setdefault("defaults", {})
    d["thresholds"] = {"mode": "absolute",
                       "steps": [{"color": "green", "value": None}, {"color": "red", "value": value}]}
    d.setdefault("custom", {})["thresholdsStyle"] = {"mode": "line"}


def build_dashboard(opts: dict) -> str:
    name = (opts.get("name") or "pg").strip()
    instances = _clamp(opts.get("instances"), 1, 9, 3)
    goals = opts.get("goals") or {}

    # panels, in order — some only make sense for certain configs
    order = ["up", "connections", "db-size"]
    if instances > 1:
        order.append("replication-lag")
    if opts.get("backups"):
        order.append("archiving")

    panels = []
    for i, key in enumerate(order):
        panel = json.loads(_text(f"dashboard/{key}.json").replace("__CLUSTER__", name))
        panel["id"] = i + 1
        panel["gridPos"] = {"h": 8, "w": 12, "x": (i % 2) * 12, "y": (i // 2) * 8}
        goal_val = num(goals.get(_PANEL_GOAL.get(key)))   # a goal for this panel?
        if goal_val is not None:
            _waterline(panel, goal_val)
        panels.append(panel)

    dash = json.loads(_text("dashboard.tmpl.json").replace("__TITLE__", f"{name} — CloudNativePG"))
    dash["uid"] = f"k8ost-{name}"[:40]
    dash["panels"] = panels
    return json.dumps(dash, indent=2)
