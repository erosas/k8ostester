import pytest
from k8ostester.core.goals import evaluate_goals, _threshold, _rto
from k8ostester.core.experiment import GoalSpec
from k8ostester.core.exceptions import K8osConfigError

def test_threshold_parsing():
    # Test rto seconds
    assert _threshold(GoalSpec(metric="rto", max="30s")) == (30.0, "max")
    assert _threshold(GoalSpec(metric="rto", min=10)) == (10.0, "min")
    
    # Test availability percentage
    assert _threshold(GoalSpec(metric="availability", min="99.9%")) == (99.9, "min")
    
    # Test latency ms
    assert _threshold(GoalSpec(metric="write_latency_p99", max="100ms")) == (100.0, "max")
    assert _threshold(GoalSpec(metric="read_latency_p95", max="0.5s")) == (500.0, "max")
    
    # Test tps
    assert _threshold(GoalSpec(metric="tps", min="500/s")) == (500.0, "min")

def test_rto_calculation():
    ops = [
        {"op": "write", "ok": True, "t": 10},
        {"op": "write", "ok": True, "t": 11},
        {"op": "write", "ok": True, "t": 20}, # 9s gap
        {"op": "write", "ok": True, "t": 21},
    ]
    fault_events = [{"ts": 15}]
    
    rto, detail = _rto(ops, fault_events)
    assert rto == 9.0
    assert "outage per fault: 9.0s" in detail

def test_rto_no_faults():
    rto, detail = _rto([], [])
    assert rto == 0.0
    assert "no faults" in detail

def test_rto_total_outage():
    ops = [
        {"op": "write", "ok": True, "t": 100},
        {"kind": "op", "op": "write", "ok": False, "t": 100} # last op for censoring
    ]
    fault_events = [{"ts": 10}] # No writes around T=10
    rto, detail = _rto(ops, fault_events)
    # ft=10. window starts at 10-30= -20.
    # ops has write at T=100.
    # starts will find T=100 as being in [-20, 190].
    # But wait, starts only looks at successful writes.
    # T=100 is a successful write.
    # So starts = [0]. gaps will be calculated.
    # since starts[-1] == 0 and len(ts) == 1, gaps will append (max(t)-ts[0])
    # max(t) is 100, ts[0] is 100. gap = 0.
    
    # If I want total outage, I need NO successful writes around the fault.
    rto, detail = _rto([], [{"ts": 10}])
    assert rto == float("inf")
    assert "total outage" in detail

def test_evaluate_goals_basic():
    goals = [
        GoalSpec(metric="availability", min="100%"),
        GoalSpec(check="integrity")
    ]
    ops = [
        {"op": "write", "ok": True, "t": 1},
        {"op": "read", "ok": True, "t": 2},
    ]
    verifications = [
        {"check": "integrity", "passed": True, "detail": "all good"}
    ]
    
    results = evaluate_goals(goals, ops, [], verifications)
    assert len(results) == 2
    assert results[0]["passed"] is True
    assert results[0]["value"] == "100.00%"
    assert results[1]["passed"] is True
    assert results[1]["detail"] == "all good"

def test_evaluate_goals_latency_steady_state():
    goals = [
        GoalSpec(metric="write_latency_p99", max="100ms", window="steady-state")
    ]
    ops = [
        {"op": "write", "ok": True, "t": 1, "lat_ms": 10}, # steady
        {"op": "write", "ok": True, "t": 2, "lat_ms": 11}, # steady
        {"op": "write", "ok": True, "t": 10, "lat_ms": 500}, # during/after fault
    ]
    fault_events = [{"ts": 10}]
    
    # steady() filters T < first_fault - 2, so T < 8
    results = evaluate_goals(goals, ops, fault_events, [])
    assert results[0]["passed"] is True
    assert results[0]["value"] == "11.0ms" # p99 of [10, 11]

def test_evaluate_goals_uptime_downtime():
    goals = [
        GoalSpec(metric="uptime", min="50%"),
        GoalSpec(metric="downtime_total", max="10s")
    ]
    # T=0 demanded, success
    # T=1 demanded, failure
    # T=2 no demand
    ops = [
        {"op": "write", "ok": True, "t": 10.0},
        {"op": "write", "ok": False, "t": 11.1},
    ]
    
    results = evaluate_goals(goals, ops, [], [])
    # demanded seconds: {0, 1}
    # up seconds: {0}
    # uptime: 1/2 = 50%
    # downtime: 1s
    assert results[0]["value"] == "50.00%"
    assert results[1]["value"] == "1s"

def test_evaluate_goals_rpo():
    goals = [GoalSpec(metric="rpo", max=0)]
    verifications = [{"check": "integrity", "passed": False, "missing": 5, "detail": "lost 5"}]
    
    results = evaluate_goals(goals, [], [], verifications)
    assert results[0]["value"] == "5 lost writes"
    assert results[0]["passed"] is False

def test_evaluate_goals_error_rates():
    goals = [
        GoalSpec(metric="error_rate", max="0%"),
        GoalSpec(metric="connect_error_rate", max="0%")
    ]
    ops = [
        {"op": "write", "ok": False, "t": 1},
        {"op": "connect", "ok": False, "t": 2},
    ]
    results = evaluate_goals(goals, ops, [], [])
    assert results[0]["value"] == "100.00%"
    assert results[1]["value"] == "100.00%"

def test_evaluate_goals_unknown_metric():
    goals = [GoalSpec(metric="invalid", max=1)]
    with pytest.raises(K8osConfigError, match="unknown goal metric"):
        evaluate_goals(goals, [], [], [])

def test_evaluate_goals_tps():
    goals = [GoalSpec(metric="tps", min=10)]
    ops = [
        {"op": "write", "ok": True, "t": 10},
        {"op": "write", "ok": True, "t": 20},
    ]
    results = evaluate_goals(goals, ops, [], [])
    # 2 ops / 10s = 0.2/s
    assert results[0]["value"] == "0/s"
    assert results[0]["passed"] is False

def test_evaluate_goals_rpo_missing_integrity():
    goals = [GoalSpec(metric="rpo", max=0)]
    with pytest.raises(K8osConfigError, match="requires 'integrity'"):
        evaluate_goals(goals, [], [], [])
