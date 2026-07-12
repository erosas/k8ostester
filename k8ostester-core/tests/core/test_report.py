import pytest
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from k8ostester.core.report import gather_run, find_all_runs, render

@pytest.fixture
def mock_run_dir(tmp_path):
    run_dir = tmp_path / "exp1" / "run1"
    run_dir.mkdir(parents=True)
    
    summary = {
        "experiment": "exp1",
        "run_id": "run1",
        "status": "passed",
        "group": "group1",
        "goals": [{"goal": "availability", "passed": True, "value": "100%", "threshold": "min 99%"}],
        "verifications": [{"check": "integrity", "passed": True, "detail": "ok"}]
    }
    (run_dir / "summary.json").write_text(json.dumps(summary))
    
    metrics = [
        {"kind": "op", "t": 100.0, "ok": True, "op": "write", "lat_ms": 10},
        {"kind": "op", "t": 101.0, "ok": True, "op": "write", "lat_ms": 11},
        {"kind": "op", "t": 101.5, "ok": False, "op": "write", "err": "OSError"},
    ]
    (run_dir / "metrics.jsonl").write_text("\n".join(json.dumps(m) for m in metrics))
    
    events = [
        {"type": "fault.injected", "ts": 100.5, "data": {"worker": "pod_kill"}}
    ]
    (run_dir / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    
    return run_dir

def test_gather_run(mock_run_dir):
    data = gather_run(mock_run_dir)
    assert data["name"] == "exp1"
    assert data["status"] == "passed"
    assert len(data["series"]) == 2 # 2 seconds of data
    assert len(data["faults"]) == 1
    assert data["faults"][0]["label"] == "pod_kill"
    assert data["stats"]["acked_writes"] == 2

def test_find_all_runs(tmp_path):
    run1 = tmp_path / "01-exp" / "run1"
    run1.mkdir(parents=True)
    (run1 / "summary.json").write_text(json.dumps({"group": "g1"}))
    
    run2 = tmp_path / "02-exp" / "run2"
    run2.mkdir(parents=True)
    (run2 / "summary.json").write_text(json.dumps({"group": "g1"}))
    
    runs = find_all_runs(tmp_path)
    assert len(runs) == 2
    assert runs[0] == run1
    assert runs[1] == run2

def test_find_latest_runs_group_reduces_to_latest_verdict(tmp_path):
    """--group auto-reduces to the latest verdict per experiment in the group,
    skipping older runs, sessions, errors, and other groups."""
    from k8ostester.core.report import find_latest_runs
    def run(exp, stamp, status, group):
        d = tmp_path / exp / stamp
        d.mkdir(parents=True)
        (d / "summary.json").write_text(json.dumps({"status": status, "group": group}))
        return d

    run("01-exp", "20260101", "passed", "g1")
    latest1 = run("01-exp", "20260102", "failed", "g1")   # newer verdict, same exp
    run("01-exp", "20260103", "session", "g1")            # session skipped
    run("02-exp", "20260101", "error", "g1")              # error-only excluded
    latest3 = run("03-exp", "20260101", "passed", "g1")
    run("04-exp", "20260101", "passed", "g2")             # other group excluded

    assert find_latest_runs(tmp_path, group="g1") == [latest1, latest3]

def test_render(tmp_path):
    runs = [
        {
            "label": "run1", "run_id": "r1", "status": "passed",
            "goals": [{"goal": "g1", "passed": True, "value": "10", "threshold": "max 20"}],
            "verifications": [{"check": "v1", "passed": True, "detail": "ok"}],
            "series": [], "faults": [], "stats": {}
        }
    ]
    out = tmp_path / "report.html"
    
    # Mock the template file as well
    with patch("k8ostester.core.report._TEMPLATE_PATH") as mock_tmpl:
        mock_tmpl.read_text.return_value = "Title: __TITLE__ Payload: __PAYLOAD__"
        render(runs, "Test Report", out)
        
        content = out.read_text()
        assert "Title: Test Report" in content
        assert '"run_id":"r1"' in content


def test_find_latest_runs_one_verdict_per_experiment(tmp_path):
    from k8ostester.core.report import find_latest_runs
    def run(exp, stamp, status):
        d = tmp_path / exp / stamp
        d.mkdir(parents=True)
        (d / "summary.json").write_text(json.dumps({"status": status}))
        return d

    run("01-a", "20260101-000000", "passed")
    latest_a = run("01-a", "20260102-000000", "failed")   # newer verdict wins
    run("01-a", "20260103-000000", "session")             # sessions ignored
    run("02-b", "20260101-000000", "error")               # error-only → excluded
    latest_c = run("03-c", "20260101-000000", "passed")

    result = find_latest_runs(tmp_path)
    assert result == [latest_a, latest_c]                 # experiment order, verdicts only
