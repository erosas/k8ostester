import pytest
from k8ostester.core.experiment import parse_duration, parse_rate, GoalSpec
from k8ostester.core.goals import _threshold, evaluate_goals
from k8ostester.core.exceptions import K8osConfigError

def test_parse_duration():
    assert parse_duration("30s") == 30.0
    assert parse_duration("2m") == 120.0
    assert parse_duration("100ms") == 0.1
    assert parse_duration("1h") == 3600.0
    with pytest.raises(ValueError):
        parse_duration("invalid")

def test_parse_rate():
    assert parse_rate("500/s") == 500.0
    assert parse_rate("100") == 100.0
    with pytest.raises(ValueError):
        parse_rate("invalid")

def test_goal_threshold():
    goal = GoalSpec(metric="rto", max="30s")
    limit, kind = _threshold(goal)
    assert limit == 30.0
    assert kind == "max"

    goal = GoalSpec(metric="tps", min="100/s")
    limit, kind = _threshold(goal)
    assert limit == 100.0
    assert kind == "min"

    goal = GoalSpec(metric="write_latency_p95", max="100ms")
    limit, kind = _threshold(goal)
    assert limit == 100.0  # ms
    assert kind == "max"

def test_evaluate_goals_basic():
    goals = [GoalSpec(metric="availability", min="99.9%")]
    ops = [
        {"t": 1.0, "op": "write", "ok": True, "lat_ms": 10},
        {"t": 2.0, "op": "read", "ok": True, "lat_ms": 5},
    ]
    results = evaluate_goals(goals, ops, [], [])
    assert len(results) == 1
    assert results[0]["passed"] is True
    assert results[0]["value"] == "100.00%"

def test_evaluate_goals_failure():
    goals = [GoalSpec(metric="availability", min="100%")]
    ops = [
        {"t": 1.0, "op": "write", "ok": True, "lat_ms": 10},
        {"t": 2.0, "op": "read", "ok": False, "lat_ms": 5},
    ]
    results = evaluate_goals(goals, ops, [], [])
    assert results[0]["passed"] is False
    assert results[0]["value"] == "50.00%"
