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
    series: list[dict] = []
    faults: list[dict] = []
    if ops:  # error runs / load-less experiments still belong in the goal matrix
        t0 = min(r["t"] for r in ops)
        buckets: dict[int, dict] = {}
        for r in ops:
            b = buckets.setdefault(
                int((r["t"] - t0) / BUCKET_S), {"ok": 0, "err": 0, "lat": []}
            )
            if not r["ok"]:
                b["err"] += 1  # any failed op — read, write or connect
            elif r["op"] == "write":
                b["ok"] += 1
                b["lat"].append(r["lat_ms"])

        # every second up to the last observed op gets a point: a second with no
        # attempted writes is an outage and must graph as 0, not as a gap the
        # line bridges over (hung clients stop *attempting*, see plan §9)
        last_sec = int((max(r["t"] for r in ops) - t0) / BUCKET_S)
        for sec in range(last_sec + 1):
            b = buckets.get(sec)
            series.append(
                {
                    "s": sec,
                    "wps": b["ok"] if b else 0,
                    "eps": b["err"] if b else 0,
                    "p99": round(percentile(sorted(b["lat"]), 99), 2)
                    if b and b["lat"]
                    else None,
                }
            )

        events_path = run_dir / "events.jsonl"
        if events_path.exists():
            for line in events_path.read_text().splitlines():
                ev = json.loads(line)
                if ev["type"] == "fault.injected":
                    faults.append(
                        {"s": round(ev["ts"] - t0, 1), "label": ev["data"]["worker"]}
                    )

    stats = None
    if ops:
        t0 = min(r["t"] for r in ops)
        demanded = {int(r["t"] - t0) for r in ops}
        up = {int(r["t"] - t0) for r in ops if r["ok"]}
        acked = sum(1 for r in ops if r["op"] == "write" and r["ok"])
        ok_ops = sum(1 for r in ops if r["ok"] and r["op"] in ("read", "write"))
        failed = sum(1 for r in ops if not r["ok"])
        stats = {
            "ops": len(ops),
            "acked_writes": acked,
            "failed": failed,
            "avg_tps": round(ok_ops / len(demanded), 1) if demanded else 0,
            "downtime_s": len(demanded - up),
            "span_s": int(max(r["t"] for r in ops) - t0) + 1,
        }

    return {
        "label": f"{summary['experiment']} ({summary['run_id']})",
        "name": summary["experiment"],
        "run_id": summary["run_id"],
        "status": summary["status"],
        "goals": summary.get("goals", []),
        "verifications": summary.get("verifications", []),
        "series": series,
        "faults": faults,
        "stats": stats,
    }


def find_all_runs(results_root: Path = Path("results")) -> list[Path]:
    """Every recorded run, in experiment order (numbered prefixes sort)."""
    return [p.parent for p in sorted(results_root.glob("*/*/summary.json"))]


def find_latest_runs(results_root: Path = Path("results"),
                     group: str | None = None) -> list[Path]:
    """The most recent *verdict* run (passed/failed) of each experiment — the
    one-row-per-experiment view. Skips sessions (no verdict) and error runs so
    a report reads as a clean comparison, not the raw run history. With
    `group`, restricted to experiments recorded under that group."""
    latest: dict[str, Path] = {}
    for summary_path in sorted(results_root.glob("*/*/summary.json")):
        try:
            summary = json.loads(summary_path.read_text())
        except json.JSONDecodeError:
            continue
        if summary.get("status") not in ("passed", "failed"):
            continue
        if group is not None and summary.get("group") != group:
            continue
        latest[summary_path.parent.parent.name] = summary_path.parent  # later run wins
    return [latest[name] for name in sorted(latest)]


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
