"""Thin support for linear experiments — deliberately NOT a framework.

An experiment is plain Python: a script that deploys, drives load, injects faults
with delays, and records what it checked. This helper only standardizes the two
things every experiment needs identically — the **run window** (so SLO queries
know the interval) and the **verdict** (verify-steps + SLO range-queries). The
steps themselves are yours to write. See docs/architecture-restructure.md.
"""
from __future__ import annotations

import time
from collections.abc import Callable

from k8ostester_kernel.verdict import Fetcher, SloCheck, evaluate_slos
from k8ostester_kernel.verdict import verdict as _verdict


class Run:
    """Tracks one experiment run: its window, its verify results, and its events.

    ``now`` is injectable so the verdict logic is testable without real time.
    """

    def __init__(self, experiment: str, *, now: Callable[[], float] = time.time):
        self.experiment = experiment
        self._now = now
        self.started = now()
        self.ended: float | None = None
        self.verifies: dict[str, bool] = {}
        self.events: list[dict] = []

    def event(self, step: str, detail: str, **extra) -> None:
        self.events.append({"t": self._now(), "step": step, "detail": detail, **extra})

    def verify(self, name: str, ok: bool) -> bool:
        """Record a correctness check (RPO, integrity, PITR…) and return it."""
        self.verifies[name] = ok
        return ok

    def finish(self) -> None:
        self.ended = self._now()

    def verdict(self, fetch: Fetcher, slo_checks: list[SloCheck]) -> dict:
        """Assemble the run verdict: verify-steps AND SLO range-queries over the
        window. ``fetch`` is a kernel Fetcher (e.g. prometheus_fetcher(...))."""
        end = self.ended if self.ended is not None else self._now()
        slo = evaluate_slos(fetch, slo_checks, self.started, end)
        v = _verdict(slo, self.verifies)
        v["experiment"] = self.experiment
        v["window"] = {"start": self.started, "end": end}
        return v
