"""Per-run metric store.

The authoritative record for goal evaluation: one JSON line per measurement
(load-gen operations, connection attempts, probe results). Kept deliberately
dumb — append during the run, load fully for evaluation afterwards; runs are
minutes long, not weeks.
"""

from k8ostester.core.exceptions import K8osConfigError

import json
from pathlib import Path
from typing import Any, Iterator


class MetricStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "a")

    def record(self, kind: str, ts: float, **fields: Any) -> None:
        self._file.write(json.dumps({"kind": kind, "ts": ts, **fields}) + "\n")

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    @staticmethod
    def read(path: Path, kind: str | None = None) -> Iterator[dict]:
        with open(path) as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                if kind is None or rec["kind"] == kind:
                    yield rec


def percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile; values must be pre-sorted."""
    if not sorted_values:
        raise K8osConfigError("no values to calculate percentile")
    k = max(0, min(len(sorted_values) - 1, round(p / 100 * len(sorted_values)) - 1))
    return sorted_values[k]
