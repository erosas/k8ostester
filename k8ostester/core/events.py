"""Run event log.

Every notable moment of a run (namespace created, deploy started, pod ready,
fault injected, recovery observed…) is appended as one JSON line. Goal
evaluators later read fault/recovery timestamps from here, and reports render
it as the run timeline.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable


class EventLog:
    def __init__(self, path: Path, on_event: Callable[[dict], None] | None = None):
        self.path = path
        self._on_event = on_event
        self._start = time.time()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "a")

    def emit(self, type: str, msg: str = "", **data: Any) -> dict:
        event = {
            "ts": time.time(),
            "t_rel": round(time.time() - self._start, 3),
            "type": type,
            "msg": msg,
            **({"data": data} if data else {}),
        }
        self._file.write(json.dumps(event) + "\n")
        self._file.flush()
        if self._on_event:
            self._on_event(event)
        return event

    def close(self) -> None:
        self._file.close()

    @staticmethod
    def read(path: Path) -> list[dict]:
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]
