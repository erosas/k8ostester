import time

import pytest


class FakeClock:
    """Deterministic time for poll-loop tests: sleeps advance the clock
    instantly instead of blocking, so timeouts are exercised for real without
    counting clock reads. `suspend()` advances wall time but not monotonic —
    the host-sleep divergence the wait_until deadline guards against.
    """

    def __init__(self, start: float = 1000.0):
        self.wall = start
        self.mono = start

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds

    def suspend(self, seconds: float) -> None:
        self.wall += seconds


@pytest.fixture
def fake_clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(time, "time", clock.time)
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    return clock
