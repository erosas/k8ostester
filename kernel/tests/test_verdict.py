"""Unit tests for the SLO-verdict helper — pure, no cluster/Prometheus needed."""
from k8ostester_kernel.verdict import SloCheck, evaluate_slos, verdict


def fetcher(samples_by_query):
    """A fake Fetcher returning canned samples per query."""
    return lambda query, start, end: samples_by_query.get(query, [])


def test_max_check_passes_when_worst_case_under_threshold():
    c = SloCheck("error_rate", "err", threshold=0.01, direction="max")
    # worst-case (max) sample is 0.004 ≤ 0.01
    res = evaluate_slos(fetcher({"err": [0.001, 0.004, 0.002]}), [c], 0, 100)
    assert res["error_rate"]["observed"] == 0.004
    assert res["error_rate"]["pass"] is True


def test_max_check_fails_on_a_single_breach():
    c = SloCheck("p99_ms", "p99", threshold=200, direction="max")
    # one spike above threshold fails the whole window
    res = evaluate_slos(fetcher({"p99": [50, 80, 240, 60]}), [c], 0, 100)
    assert res["p99_ms"]["observed"] == 240
    assert res["p99_ms"]["pass"] is False


def test_min_check_uses_worst_case_minimum():
    c = SloCheck("uptime_pct", "up", threshold=95, direction="min")
    res_ok = evaluate_slos(fetcher({"up": [100, 99, 96]}), [c], 0, 100)
    assert res_ok["uptime_pct"]["observed"] == 96
    assert res_ok["uptime_pct"]["pass"] is True
    res_bad = evaluate_slos(fetcher({"up": [100, 92, 99]}), [c], 0, 100)
    assert res_bad["uptime_pct"]["pass"] is False


def test_empty_samples_treated_as_zero():
    # no data → observed 0.0; a min check with threshold>0 therefore fails
    assert SloCheck("x", "q", 1, "min").observed([]) == 0.0
    res = evaluate_slos(fetcher({}), [SloCheck("x", "q", 1, "min")], 0, 100)
    assert res["x"]["pass"] is False


def test_verdict_requires_both_slos_and_verifies():
    slo_pass = {"a": {"pass": True}}
    slo_fail = {"a": {"pass": False}}
    assert verdict(slo_pass, {"rpo": True, "integrity": True})["verdict"] == "pass"
    assert verdict(slo_pass, {"rpo": False})["verdict"] == "fail"          # verify fails
    assert verdict(slo_fail, {"rpo": True})["verdict"] == "fail"           # slo fails
    assert verdict({}, {})["verdict"] == "pass"                            # nothing to fail
