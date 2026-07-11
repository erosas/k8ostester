"""Goal evaluation: declarative SLOs judged against the run's evidence.

Evidence, per D5, is the loadgen's per-op records (single clock — the loadgen
pod's) plus the framework's fault events and verification outcomes. Fault
timestamps come from the framework clock, so they are only used to *locate*
the outage window; the outage itself is measured as a gap between loadgen
timestamps, which makes RTO immune to host↔pod clock skew.
"""

from __future__ import annotations

import re

from k8ostester.core.exceptions import K8osConfigError
from k8ostester.core.experiment import GoalSpec, parse_duration, parse_rate
from k8ostester.core.metrics import percentile

# how far around a fault we look for its outage gap
# (RTO calculation: we find the largest gap between successful writes
# that starts within this window of the fault injection time)
RTO_SCAN_WINDOW_BEFORE_S = 30
RTO_SCAN_WINDOW_AFTER_S = 180

_LATENCY_RE = re.compile(r"^(write|read|connect)_latency_p(\d{2})$")


def _threshold(goal: GoalSpec) -> tuple[float, str]:
    """Returns (limit, kind) where kind is 'max' or 'min', in the metric's
    canonical unit: rto → s, rpo → count, availability/error rates → %,
    latency → ms."""
    raw = goal.max if goal.max is not None else goal.min
    kind = "max" if goal.max is not None else "min"
    if raw is None:
        raise K8osConfigError(f"goal {goal.metric!r} needs 'max' or 'min'")
    if isinstance(raw, (int, float)):
        return float(raw), kind
    s = str(raw).strip()
    if s.endswith("%"):
        return float(s[:-1]), kind
    if goal.metric == "tps":
        return parse_rate(s), kind  # canonical ops/s ("500/s" or bare number)
    if goal.metric and _LATENCY_RE.match(goal.metric):
        return parse_duration(s) * 1000, kind  # canonical ms
    return parse_duration(s), kind  # canonical seconds (rto)


def _ok_write_ts(ops: list[dict]) -> list[float]:
    return sorted(r["t"] for r in ops if r["op"] == "write" and r["ok"])


def _rto(ops: list[dict], fault_events: list[dict]) -> tuple[float, str]:
    """Max outage across faults; outage = largest gap between consecutive
    successful writes *starting* in a window around the fault. The gap's far
    end is unbounded: an outage longer than the window must be reported at
    full length, not silently truncated to the pre-fault noise."""
    if not fault_events:
        return 0.0, "no faults injected"
    ts = _ok_write_ts(ops)
    worst, details = 0.0, []
    for fault in fault_events:
        ft = fault["ts"]
        starts = [
            i for i, t in enumerate(ts)
            if ft - RTO_SCAN_WINDOW_BEFORE_S <= t <= ft + RTO_SCAN_WINDOW_AFTER_S
        ]
        if not starts:
            return float("inf"), "no successful writes around the fault — total outage"
        gaps = [ts[i + 1] - ts[i] for i in starts if i + 1 < len(ts)]
        if starts[-1] == len(ts) - 1:
            # no success after this one — the outage is censored by the end of
            # the data; count time up to the last op of any kind (lower bound)
            gaps.append(max(r["t"] for r in ops) - ts[starts[-1]])
        gap = max(gaps)
        worst = max(worst, gap)
        details.append(f"{gap:.1f}s")
    return worst, f"outage per fault: {', '.join(details)}"


def evaluate_goals(
    goals: list[GoalSpec],
    ops: list[dict],
    fault_events: list[dict],
    verifications: list[dict],
) -> list[dict]:
    results = []
    first_fault_ts = min((f["ts"] for f in fault_events), default=None)

    def steady(records: list[dict]) -> list[dict]:
        if first_fault_ts is None:
            return records
        return [r for r in records if r["t"] < first_fault_ts - 2]

    for goal in goals:
        if goal.check:
            outcome = next((v for v in verifications if v["check"] == goal.check), None)
            passed = bool(outcome and outcome["passed"])
            detail = outcome["detail"] if outcome else f"verify step '{goal.check}' did not run"
            results.append(
                {"goal": goal.check, "value": "pass" if passed else "fail",
                 "threshold": "must pass", "passed": passed, "detail": detail}
            )
            continue

        metric = goal.metric or ""
        limit, kind = _threshold(goal)

        if metric == "rto":
            value, detail = _rto(ops, fault_events)
            display = f"{value:.1f}s"
        elif metric == "rpo":
            integrity = next((v for v in verifications if v["check"] == "integrity"), None)
            if integrity is None:
                raise K8osConfigError("rpo goal requires 'integrity' in verify steps")
            value = float(integrity.get("missing", 0 if integrity["passed"] else float("inf")))
            detail = integrity["detail"]
            display = f"{int(value)} lost writes"
        elif metric == "availability":
            considered = [r for r in ops if r["op"] in ("read", "write")]
            value = 100.0 * sum(r["ok"] for r in considered) / len(considered) if considered else 0.0
            detail = f"{sum(r['ok'] for r in considered)}/{len(considered)} ops succeeded"
            display = f"{value:.2f}%"
        elif metric == "uptime":
            # time-bucketed availability: of the seconds where clients demanded
            # service (≥1 op attempted — failures count as demand), the fraction
            # that saw ≥1 success. Demand-aware so a deliberate pause phase
            # (PITR anchor, D13) isn't scored as downtime; safe because the
            # loadgen is bounded — during an outage clients keep attempting and
            # journaling failures, so outage seconds always land in the
            # denominator (unlike op-count availability, plan §9)
            if not ops:
                value, detail = 0.0, "no ops recorded"
            else:
                t0 = min(r["t"] for r in ops)
                demanded = {int(r["t"] - t0) for r in ops}
                up = {int(r["t"] - t0) for r in ops if r["ok"]}
                value = 100.0 * len(up & demanded) / len(demanded)
                detail = f"{len(up & demanded)}/{len(demanded)} demanded seconds had ≥1 successful op"
            display = f"{value:.2f}%"
        elif m := _LATENCY_RE.match(metric):
            op_kind, pct = m.group(1), int(m.group(2))
            pool = [r for r in ops if r["op"] == op_kind and r["ok"]]
            if goal.window == "steady-state":
                pool = steady(pool)
            if not pool:
                results.append(
                    {"goal": metric, "value": "n/a", "threshold": f"{kind} {limit}",
                     "passed": False, "detail": f"no successful {op_kind} ops in window"}
                )
                continue
            value = percentile(sorted(r["lat_ms"] for r in pool), pct)
            detail = f"{len(pool)} {op_kind} ops in window '{goal.window}'"
            display = f"{value:.1f}ms"
        elif metric == "tps":
            ok_ops = sorted(r["t"] for r in ops if r["ok"])
            span = ok_ops[-1] - ok_ops[0] if len(ok_ops) > 1 else 0.0
            value = len(ok_ops) / span if span else 0.0
            detail = f"{len(ok_ops)} successful ops over {span:.0f}s"
            display = f"{value:.0f}/s"
        elif metric == "connect_error_rate":
            pool = [r for r in ops if r["op"] == "connect"]
            value = 100.0 * sum(not r["ok"] for r in pool) / len(pool) if pool else 0.0
            detail = f"{sum(not r['ok'] for r in pool)}/{len(pool)} connects failed"
            display = f"{value:.2f}%"
        elif metric == "downtime_total":
            # sum of demanded seconds with no successful op — complements uptime
            # (a ratio) with an absolute number, and distinguishes one long
            # outage from many short ones when a run has multiple faults
            if not ops:
                value, detail = float("inf"), "no ops recorded"
            else:
                t0 = min(r["t"] for r in ops)
                demanded = {int(r["t"] - t0) for r in ops}
                up = {int(r["t"] - t0) for r in ops if r["ok"]}
                value = float(len(demanded - up))
                detail = f"{int(value)} of {len(demanded)} demanded seconds had no successful op"
            display = f"{value:.0f}s"
        elif metric == "error_rate":
            # op-level twin of connect_error_rate: failed reads+writes over
            # attempts — the "how many requests errored" number an app owner asks for
            pool = [r for r in ops if r["op"] in ("read", "write")]
            value = 100.0 * sum(not r["ok"] for r in pool) / len(pool) if pool else 0.0
            detail = f"{sum(not r['ok'] for r in pool)}/{len(pool)} ops failed"
            display = f"{value:.2f}%"
        else:
            raise K8osConfigError(f"unknown goal metric {metric!r}")

        passed = value <= limit if kind == "max" else value >= limit
        unit = {"rto": "s", "rpo": "", "tps": "/s", "downtime_total": "s"}.get(
            metric, "ms" if "latency" in metric else "%"
        )
        results.append(
            {"goal": metric, "value": display, "threshold": f"{kind} {limit:g}{unit}",
             "passed": passed, "detail": detail}
        )
    return results
