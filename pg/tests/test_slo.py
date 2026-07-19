"""Unit tests for the CNPG SLO checks — pure, no cluster."""
from k8ostester_kernel.verdict import evaluate_slos, verdict
from k8ostester_pg.slo import default_checks


def test_default_checks_are_scoped_to_the_experiment():
    checks = default_checks("20-cnpg-reference")
    names = {c.name for c in checks}
    assert names == {"error_rate", "write_latency_p99_s", "app_availability"}
    # every query is scoped to the experiment label so runs don't cross-contaminate
    assert all('experiment="20-cnpg-reference"' in c.query for c in checks)


def test_thresholds_and_directions_match_the_goals():
    by = {c.name: c for c in default_checks("x")}
    assert by["error_rate"].threshold == 0.01 and by["error_rate"].direction == "max"
    assert by["write_latency_p99_s"].threshold == 0.2 and by["write_latency_p99_s"].direction == "max"
    assert by["app_availability"].threshold == 1 and by["app_availability"].direction == "min"


def test_checks_feed_the_kernel_verdict():
    checks = default_checks("x")
    # a clean run: low error rate, fast writes, always up
    samples = {
        checks[0].query: [0.0, 0.002],
        checks[1].query: [0.05, 0.11],
        checks[2].query: [1, 1, 1],
    }
    fetch = lambda q, s, e: samples.get(q, [])  # noqa: E731
    slo = evaluate_slos(fetch, checks, 0, 100)
    assert verdict(slo, {"rpo": True, "integrity": True})["verdict"] == "pass"
    # a write-latency breach flips the verdict
    samples[checks[1].query] = [0.05, 0.9]
    slo = evaluate_slos(fetch, checks, 0, 100)
    assert verdict(slo, {"rpo": True})["verdict"] == "fail"
