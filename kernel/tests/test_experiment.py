"""Unit tests for the thin experiment Run helper — no cluster, fake clock."""
from k8ostester_kernel.experiment import Run
from k8ostester_kernel.verdict import SloCheck


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_run_records_window_and_verifies():
    clk = Clock()
    run = Run("kill-primary", now=clk)
    run.event("chaos", "killed primary")
    run.verify("integrity", True)
    clk.t += 300
    run.finish()
    assert run.started == 1000.0
    assert run.ended == 1300.0
    assert run.verifies == {"integrity": True}
    assert run.events[0]["step"] == "chaos"


def test_verdict_combines_verifies_and_slos_over_the_window():
    clk = Clock()
    run = Run("kill-primary", now=clk)
    run.verify("integrity", True)
    clk.t += 200
    run.finish()
    check = SloCheck("error_rate", "err", threshold=0.01, direction="max")
    seen = {}

    def fetch(query, start, end):
        seen["window"] = (start, end)   # verdict must query over the run window
        return [0.0, 0.003]

    v = run.verdict(fetch, [check])
    assert seen["window"] == (1000.0, 1200.0)
    assert v["verdict"] == "pass"
    assert v["experiment"] == "kill-primary"
    assert v["window"] == {"start": 1000.0, "end": 1200.0}


def test_a_failed_verify_fails_the_verdict_even_with_clean_slos():
    run = Run("x", now=Clock())
    run.verify("rpo", False)
    run.finish()
    v = run.verdict(lambda q, s, e: [0.0], [SloCheck("e", "e", 1, "max")])
    assert v["verdict"] == "fail"
