"""Unit tests for the SLO-verdict helper — pure, no cluster/Prometheus needed."""
from k8ostester_kernel.verdict import SloCheck, evaluate_slos, verdict


def fetcher(samples_by_query):
    """A fake Fetcher returning canned samples per query."""
    return lambda query, start, end: samples_by_query.get(query, [])


def test_avg_is_the_default_a_brief_blip_does_not_fail():
    # the resilience default: a single spike is averaged away, not fatal
    c = SloCheck("error_rate", "err", threshold=0.01, direction="max")
    res = evaluate_slos(fetcher({"err": [0.0, 0.0, 0.03, 0.0]}), [c], 0, 100)
    assert round(res["error_rate"]["observed"], 4) == 0.0075   # mean, not the 0.03 spike
    assert res["error_rate"]["pass"] is True


def test_avg_fails_on_sustained_impact():
    c = SloCheck("error_rate", "err", threshold=0.01, direction="max")
    res = evaluate_slos(fetcher({"err": [0.02, 0.03, 0.02]}), [c], 0, 100)  # mean 0.0233
    assert res["error_rate"]["pass"] is False


def test_min_direction_uses_average_availability():
    c = SloCheck("availability", "up", threshold=0.95, direction="min")
    # up 95% of the window on average → pass, even though it dipped to 0 once
    res = evaluate_slos(fetcher({"up": [1, 1, 1, 1, 0, 1, 1, 1, 1, 1]}), [c], 0, 100)
    assert res["availability"]["observed"] == 0.9      # dipped too much → below 0.95
    assert res["availability"]["pass"] is False


def test_worst_aggregate_is_zero_tolerance():
    # aggregate="worst" restores the strict "any breach fails" behavior
    c = SloCheck("p99_ms", "p99", threshold=200, direction="max", aggregate="worst")
    res = evaluate_slos(fetcher({"p99": [50, 80, 240, 60]}), [c], 0, 100)
    assert res["p99_ms"]["observed"] == 240
    assert res["p99_ms"]["pass"] is False


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
