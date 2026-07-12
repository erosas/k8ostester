import time

import pytest


class FakeClock:
    """Deterministic time for poll-loop tests: sleeps advance the clock
    instantly instead of blocking, so timeouts are exercised for real without
    counting clock reads (no fragile side_effect lists).

    `suspend()` models a host sleep: wall time advances, monotonic doesn't —
    exactly the divergence the runner's suspend detector looks for.

    Caveat for async tests: the asyncio event loop reads time.monotonic too,
    so under this fixture a real `await asyncio.sleep(x)` with x > 0 never
    completes (the loop clock is frozen). Only await things that resolve
    immediately, and advance time via the mocked callables instead.
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

@pytest.fixture(autouse=True)
def non_interactive_console(monkeypatch):
    """CLI tests must not depend on whether the test runner happens to have a
    TTY (k8ost run auto-launches the full-screen TUI on a terminal — inside
    pytest that hangs). Default every rich Console to non-terminal; tests
    that exercise terminal-only behavior patch is_terminal themselves."""
    from rich.console import Console

    monkeypatch.setattr(Console, "is_terminal", property(lambda self: False))
    # color support is decided at Console construction (import time) — reset
    # the CLI singleton so a PTY-launched test runner doesn't bake ANSI codes
    # into CliRunner output assertions
    from k8ostester.cli.app import console as app_console
    monkeypatch.setattr(app_console, "_color_system", None)
