"""Comparison report: one self-contained HTML for a set of runs.

Data prep happens here (per-second buckets from metrics.jsonl, fault offsets,
goal matrix); rendering is inline SVG + JS in the emitted file — no external
assets, so the report can be archived or shared as a single artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

from k8ostester.core.metrics import percentile

BUCKET_S = 1.0


def gather_run(run_dir: Path) -> dict:
    summary = json.loads((run_dir / "summary.json").read_text())
    ops = []
    metrics_path = run_dir / "metrics.jsonl"
    if metrics_path.exists():
        for line in metrics_path.read_text().splitlines():
            rec = json.loads(line)
            if rec.get("kind") == "op":
                ops.append(rec)
    if not ops:
        raise ValueError(f"{run_dir} has no op records to graph")
    t0 = min(r["t"] for r in ops)

    buckets: dict[int, dict] = {}
    for r in ops:
        if r["op"] != "write":
            continue
        b = buckets.setdefault(int((r["t"] - t0) / BUCKET_S), {"ok": 0, "lat": []})
        if r["ok"]:
            b["ok"] += 1
            b["lat"].append(r["lat_ms"])

    series = [
        {
            "s": sec,
            "wps": b["ok"],
            "p99": round(percentile(sorted(b["lat"]), 99), 2) if b["lat"] else None,
        }
        for sec, b in sorted(buckets.items())
    ]

    faults = []
    events_path = run_dir / "events.jsonl"
    if events_path.exists():
        for line in events_path.read_text().splitlines():
            ev = json.loads(line)
            if ev["type"] == "fault.injected":
                faults.append(
                    {"s": round(ev["ts"] - t0, 1), "label": ev["data"]["worker"]}
                )

    return {
        "label": f"{summary['experiment']} ({summary['run_id']})",
        "name": summary["experiment"],
        "run_id": summary["run_id"],
        "status": summary["status"],
        "goals": summary.get("goals", []),
        "verifications": summary.get("verifications", []),
        "series": series,
        "faults": faults,
    }


def find_group_runs(group: str, results_root: Path = Path("results")) -> list[Path]:
    dirs = []
    for summary_path in sorted(results_root.glob("*/*/summary.json")):
        try:
            if json.loads(summary_path.read_text()).get("group") == group:
                dirs.append(summary_path.parent)
        except json.JSONDecodeError:
            continue
    return dirs


def render(runs: list[dict], title: str, out: Path) -> Path:
    goal_names: list[str] = []
    for run in runs:
        for v in run["verifications"]:
            if f"verify:{v['check']}" not in goal_names:
                goal_names.append(f"verify:{v['check']}")
        for g in run["goals"]:
            if g["goal"] not in goal_names:
                goal_names.append(g["goal"])

    matrix = []
    for name in goal_names:
        row = {"goal": name, "cells": []}
        for run in runs:
            if name.startswith("verify:"):
                item = next(
                    (v for v in run["verifications"] if v["check"] == name[7:]), None
                )
                cell = (
                    {"passed": item["passed"], "value": "pass" if item["passed"] else "fail"}
                    if item
                    else None
                )
            else:
                item = next((g for g in run["goals"] if g["goal"] == name), None)
                cell = (
                    {"passed": item["passed"], "value": str(item["value"]),
                     "threshold": str(item["threshold"])}
                    if item
                    else None
                )
            row["cells"].append(cell)
        matrix.append(row)

    payload = json.dumps({"runs": runs, "matrix": matrix}, separators=(",", ":"))
    html = (
        _TEMPLATE_PATH.read_text().replace("__TITLE__", title).replace("__PAYLOAD__", payload)
    )
    out.write_text(html)
    return out


_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "resources" / "report.html"
