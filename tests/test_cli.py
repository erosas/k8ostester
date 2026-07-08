import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from typer.testing import CliRunner
from k8ostester.cli import app

runner = CliRunner()

def test_validate_command(tmp_path):
    # Create a dummy experiment directory
    exp_dir = tmp_path / "my-exp"
    exp_dir.mkdir()
    (exp_dir / "experiment.yaml").write_text("""
name: dummy
technology: postgres
goals: []
""")
    (exp_dir / "manifests").mkdir()
    
    result = runner.invoke(app, ["validate", str(exp_dir)])
    assert result.exit_code == 0
    assert "dummy (postgres) is valid" in result.output

def test_runs_command(tmp_path):
    # Create dummy results
    exp_dir = tmp_path / "01-exp"
    run_dir = exp_dir / "20230101-120000"
    run_dir.mkdir(parents=True)
    import json
    (run_dir / "summary.json").write_text(json.dumps({
        "experiment": "01-exp", "run_id": "20230101-120000", "status": "passed"
    }))
    
    result = runner.invoke(app, ["runs", "--results", str(tmp_path)])
    assert result.exit_code == 0
    assert "01-exp" in result.output
    assert "passed" in result.output

def test_runs_no_data(tmp_path):
    # Test runs command with empty results dir
    result = runner.invoke(app, ["runs", "--results", str(tmp_path)])
    assert result.exit_code == 0
    assert "no runs recorded" in result.output

def test_report_command(tmp_path):
    # Setup dummy runs
    run1 = tmp_path / "exp1" / "r1"
    run1.mkdir(parents=True)
    import json
    (run1 / "summary.json").write_text(json.dumps({"experiment": "exp1", "run_id": "r1", "status": "passed"}))
    
    out_html = tmp_path / "report.html"
    
    with patch("k8ostester.core.report.gather_run") as mock_gather, \
         patch("k8ostester.core.report.render") as mock_render:
        mock_gather.return_value = {"run_id": "r1"}
        mock_render.return_value = out_html
        
        result = runner.invoke(app, ["report", str(run1), "-o", str(out_html)])
        assert result.exit_code == 0
        mock_render.assert_called_once()
        assert "report with 1 run(s)" in result.output

def test_env_check(tmp_path):
    with patch("k8ostester.core.capabilities.probe") as mock_probe, \
         patch("k8ostester.cli.available_contexts") as mock_contexts:
        mock_contexts.return_value = (["ctx1"], "ctx1")

        from k8ostester.core.capabilities import Capabilities
        caps = Capabilities(
            context="ctx1", server_version="v1.27",
            nodes=[], storage_classes=[],
            snapshot_crds=False, snapshot_classes=[],
            operators={"cloudnative-pg": True},
            helm_version="v3.12.0", kubectl_version="v1.27.1"
        )
        mock_probe.return_value = caps

        result = runner.invoke(app, ["env", "check", "-c", "ctx1"])
        assert result.exit_code == 0
        assert "Cluster: ctx1" in result.output
        assert "Experiment capabilities" in result.output

def test_report_command_all(tmp_path):
    # Setup dummy runs
    run1 = tmp_path / "exp1" / "r1"
    run1.mkdir(parents=True)
    import json
    (run1 / "summary.json").write_text(json.dumps({"experiment": "exp1", "run_id": "r1", "status": "passed"}))
    
    with patch("k8ostester.core.report.find_all_runs") as mock_find, \
         patch("k8ostester.core.report.gather_run") as mock_gather, \
         patch("k8ostester.core.report.render") as mock_render:
        mock_find.return_value = [run1]
        mock_gather.return_value = {"run_id": "r1"}
        mock_render.return_value = tmp_path / "out.html"
        
        result = runner.invoke(app, ["report", "--all"])
        assert result.exit_code == 0
        mock_find.assert_called_once()

def test_report_command_group(tmp_path):
    run1 = tmp_path / "exp1" / "r1"
    run1.mkdir(parents=True)
    import json
    (run1 / "summary.json").write_text(json.dumps({"experiment": "exp1", "run_id": "r1", "status": "passed", "group": "g1"}))
    
    with patch("k8ostester.core.report.find_group_runs") as mock_find, \
         patch("k8ostester.core.report.gather_run") as mock_gather, \
         patch("k8ostester.core.report.render") as mock_render:
        mock_find.return_value = [run1]
        mock_gather.return_value = {"run_id": "r1"}
        mock_render.return_value = tmp_path / "out.html"
        
        result = runner.invoke(app, ["report", "--group", "g1"])
        assert result.exit_code == 0
        mock_find.assert_called_with("g1")

def make_exp_dir(tmp_path):
    exp_dir = tmp_path / "my-exp"
    exp_dir.mkdir()
    (exp_dir / "experiment.yaml").write_text("name: dummy\ntechnology: generic\n")
    (exp_dir / "manifests").mkdir()
    return exp_dir

def test_run_command_success(tmp_path):
    exp_dir = make_exp_dir(tmp_path)

    with patch("k8ostester.core.runner.Runner") as mock_runner_cls:
        mock_runner = mock_runner_cls.return_value
        from k8ostester.core.runner import RunResult
        res = RunResult("r1", tmp_path / "run1")
        res.status = "passed"
        res.namespace = "exp-dummy-r1"
        mock_runner.run.return_value = res

        result = runner.invoke(app, ["run", str(exp_dir)])
        assert result.exit_code == 0
        assert "PASSED" in result.output
        assert "results: " in result.output

def test_run_command_failed_prints_verdict_and_exits_2(tmp_path):
    exp_dir = make_exp_dir(tmp_path)

    with patch("k8ostester.core.runner.Runner") as mock_runner_cls:
        from k8ostester.core.runner import RunResult
        res = RunResult("r1", tmp_path / "run1")
        res.status = "failed"
        res.namespace = "exp-dummy-r1"
        res.verifications = [{"check": "integrity", "passed": False, "detail": "2 lost"}]
        res.goals = [{"goal": "rto", "passed": True, "value": "3.0s",
                      "threshold": "max 30.0", "detail": "ok"}]
        mock_runner_cls.return_value.run.return_value = res

        result = runner.invoke(app, ["run", str(exp_dir)])
        assert result.exit_code == 2
        assert "FAILED" in result.output
        assert "integrity" in result.output  # verdict table rendered
        assert "rto" in result.output

def test_run_command_error_exits_1(tmp_path):
    exp_dir = make_exp_dir(tmp_path)

    with patch("k8ostester.core.runner.Runner") as mock_runner_cls:
        mock_runner_cls.return_value.run.side_effect = RuntimeError("cluster gone")
        result = runner.invoke(app, ["run", str(exp_dir)])
        assert result.exit_code == 1
        assert "run error:" in result.output

def test_env_contexts(tmp_path):
    with patch("k8ostester.cli.available_contexts") as mock_contexts:
        mock_contexts.return_value = (["ctx1", "ctx2"], "ctx1")
        result = runner.invoke(app, ["env", "contexts"])
        assert result.exit_code == 0
        assert "* ctx1" in result.output
        assert "  ctx2" in result.output

def test_report_command_no_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # empty cwd: no results/ to discover
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 1
    assert "no runs selected" in result.output

def test_report_command_open_browser(tmp_path):
    run1 = tmp_path / "exp1" / "r1"
    run1.mkdir(parents=True)

    with patch("k8ostester.core.report.gather_run") as mock_gather, \
         patch("k8ostester.core.report.render") as mock_render, \
         patch("webbrowser.open") as mock_open:
        mock_gather.return_value = {"run_id": "r1"}
        mock_render.return_value = tmp_path / "out.html"

        result = runner.invoke(app, ["report", str(run1), "--open"])
        assert result.exit_code == 0
        mock_open.assert_called_once()

def test_runs_command_skips_corrupt_summary(tmp_path):
    import json
    good = tmp_path / "01-exp" / "r1"
    good.mkdir(parents=True)
    (good / "summary.json").write_text(json.dumps(
        {"experiment": "01-exp", "run_id": "r1", "status": "passed"}))
    bad = tmp_path / "02-exp" / "r2"
    bad.mkdir(parents=True)
    (bad / "summary.json").write_text("{not json")

    result = runner.invoke(app, ["runs", "--results", str(tmp_path)])
    assert result.exit_code == 0
    assert "01-exp" in result.output
    assert "02-exp" not in result.output

def test_env_check_renders_nodes_and_storage(tmp_path):
    with patch("k8ostester.core.capabilities.probe") as mock_probe:
        from k8ostester.core.capabilities import Capabilities, NodeInfo, StorageClassInfo
        mock_probe.return_value = Capabilities(
            context="ctx1", server_version="v1.31",
            nodes=[NodeInfo(name="n1", roles=["worker"], ready=True,
                            arch="arm64", kubelet_version="v1.31")],
            storage_classes=[StorageClassInfo(name="standard",
                                              provisioner="rancher.io/local-path",
                                              is_default=True)],
            snapshot_crds=True, snapshot_classes=["snap1"],
            operators={"cloudnative-pg": False},
            helm_version=None, kubectl_version=None,
        )
        result = runner.invoke(app, ["env", "check"])
        assert result.exit_code == 0
        assert "n1" in result.output
        assert "standard" in result.output
        assert "not found on PATH" in result.output
