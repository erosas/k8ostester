"""SLO verdict — the repeatable pass/fail for a run, sourced from Prometheus.

This replaces the old goals-evaluator. An experiment is a linear step script that
records its window ``[start, end]`` and a set of correctness verify results
(RPO, integrity, PITR — data comparisons that no metric threshold can express).
The verdict combines those verifies with **SLO checks evaluated as Prometheus
range queries over the window**. Live alerting stays in Grafana; this is the
batch verdict a test / CI consumes.

The Prometheus HTTP call is isolated behind a small fetcher so the evaluation
logic is unit-testable without a cluster.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

Direction = Literal["max", "min"]
Aggregate = Literal["worst", "avg"]

# A fetcher returns the raw samples for a query over [start, end]. Injecting it
# keeps evaluate_slos() pure and testable; prometheus_fetcher() is the real one.
Fetcher = Callable[[str, float, float], list[float]]


@dataclass(frozen=True)
class SloCheck:
    """One SLO gate.

    ``direction`` says how ``threshold`` is applied — ``max``: stay at or below
    (error_rate, p99); ``min``: stay at or above (availability).

    ``aggregate`` says how the window's samples reduce to one observed value:
    - ``avg`` (default): the mean over the window — the right choice for
      resilience SLOs, where a sub-second blip is not an outage and only
      *sustained* impact should fail the run.
    - ``worst``: the single worst sample (max for ``max``, min for ``min``) —
      zero-tolerance, for gates where any breach at all matters.
    """

    name: str
    query: str
    threshold: float
    direction: Direction = "max"
    aggregate: Aggregate = "avg"

    def observed(self, samples: list[float]) -> float:
        if not samples:
            return 0.0
        if self.aggregate == "avg":
            return sum(samples) / len(samples)
        return max(samples) if self.direction == "max" else min(samples)

    def passed(self, observed: float) -> bool:
        return observed <= self.threshold if self.direction == "max" else observed >= self.threshold


def evaluate_slos(
    fetch: Fetcher, checks: list[SloCheck], start: float, end: float
) -> dict[str, dict]:
    """Evaluate each check over [start, end]; return per-check results."""
    results: dict[str, dict] = {}
    for c in checks:
        observed = c.observed(fetch(c.query, start, end))
        results[c.name] = {
            "observed": observed,
            "threshold": c.threshold,
            "direction": c.direction,
            "pass": c.passed(observed),
        }
    return results


def verdict(slo_results: dict[str, dict], verifies: dict[str, bool]) -> dict:
    """Combine SLO results and correctness verifies into one run verdict."""
    slo_ok = all(r["pass"] for r in slo_results.values())
    verifies_ok = all(verifies.values())
    return {
        "verdict": "pass" if (slo_ok and verifies_ok) else "fail",
        "verifies": verifies,
        "slo": slo_results,
    }


def prometheus_fetcher(base_url: str, step: float = 15.0) -> Fetcher:
    """A real fetcher hitting a Prometheus /query_range endpoint."""

    def fetch(query: str, start: float, end: float) -> list[float]:
        url = base_url.rstrip("/") + "/api/v1/query_range?" + urllib.parse.urlencode(
            {"query": query, "start": start, "end": end, "step": step}
        )
        with urllib.request.urlopen(url, timeout=15) as r:  # noqa: S310 (trusted in-cluster URL)
            data = json.load(r)
        return [
            float(v[1])
            for series in data.get("data", {}).get("result", [])
            for v in series.get("values", [])
        ]

    return fetch
