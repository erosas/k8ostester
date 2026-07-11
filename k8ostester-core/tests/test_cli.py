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
         patch("k8ostester.cli.env.available_contexts") as mock_contexts:
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
    with patch("k8ostester.cli.env.available_contexts") as mock_contexts:
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

def test_find_experiments_skips_hidden_and_results(tmp_path):
    from k8ostester.cli.run import find_experiments
    for d in ("experiments/pg/01-a", "experiments/pg/02-b", ".venv/x", "results/exp/r1"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "experiment.yaml").write_text("name: x\ntechnology: generic\n")

    found = find_experiments(tmp_path)
    assert [str(d.relative_to(tmp_path)) for d in found] == \
        ["experiments/pg/01-a", "experiments/pg/02-b"]

def test_run_command_interactive_picker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    exp_dir = make_exp_dir(tmp_path)

    with patch("k8ostester.core.runner.Runner") as mock_runner_cls:
        from k8ostester.core.runner import RunResult
        res = RunResult("r1", tmp_path / "run1")
        res.status = "passed"
        mock_runner_cls.return_value.run.return_value = res

        result = runner.invoke(app, ["run"], input="1\n")
        assert result.exit_code == 0
        assert "1 experiment(s)" in result.output
        assert "PASSED" in result.output
        # the picked directory is what actually ran
        spec = mock_runner_cls.call_args[0][0]
        assert spec.name == "dummy"

def test_run_command_no_experiments_found(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 1
    assert "no experiments found" in result.output

def test_picker_flags_invalid_experiment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    make_exp_dir(tmp_path)
    bad = tmp_path / "broken"
    bad.mkdir()
    (bad / "experiment.yaml").write_text("name: broken\ntechnology: generic\n")  # no manifests/

    with patch("k8ostester.core.runner.Runner") as mock_runner_cls:
        from k8ostester.core.runner import RunResult
        res = RunResult("r1", tmp_path / "run1")
        res.status = "passed"
        mock_runner_cls.return_value.run.return_value = res

        result = runner.invoke(app, ["run"], input="2\n")
        assert result.exit_code == 0
        assert "invalid" in result.output  # the broken row is marked, not fatal

def test_live_run_view_renders_header_and_events():
    from k8ostester.cli.live import LiveRunView
    from rich.console import Console

    view = LiveRunView("my-exp", "postgres-cnpg", None)
    view.on_event({"type": "deploy.start", "t_rel": 1.2, "msg": "applying manifests"})
    view.on_event({"type": "verify.fail", "t_rel": 60.0, "msg": "2 writes lost"})

    console_ = Console(record=True, width=120)
    console_.print(view)
    out = console_.export_text()
    assert "my-exp (postgres-cnpg)" in out
    assert "deploy.start" in out
    assert "2 writes lost" in out
    assert "verify.fail" in out  # current step in the header + alert row

def test_run_command_live_view(tmp_path):
    exp_dir = make_exp_dir(tmp_path)

    with patch("k8ostester.core.runner.Runner") as mock_runner_cls:
        from k8ostester.core.runner import RunResult
        res = RunResult("r1", tmp_path / "run1")
        res.status = "passed"
        mock_runner_cls.return_value.run.return_value = res

        result = runner.invoke(app, ["run", str(exp_dir), "--view", "live"])
        assert result.exit_code == 0
        # the live view was wired in as the event sink
        from k8ostester.cli.live import LiveRunView
        assert isinstance(mock_runner_cls.call_args[1]["on_event"].__self__, LiveRunView)

def test_run_command_terminal_defaults_to_tui(tmp_path):
    from unittest.mock import PropertyMock
    from rich.console import Console

    exp_dir = make_exp_dir(tmp_path)

    with patch("k8ostester.cli.tui.run_tui", return_value=0) as mock_tui, \
         patch.object(Console, "is_terminal", new_callable=PropertyMock, return_value=True):
        result = runner.invoke(app, ["run", str(exp_dir)])
        assert result.exit_code == 0
        mock_tui.assert_called_once()

def test_live_run_view_metrics_topology_and_progress():
    from k8ostester.cli.live import LiveRunView
    from rich.console import Console

    view = LiveRunView("exp", "postgres-cnpg", "docker-desktop")
    view.on_event({"type": "load.start", "t_rel": 20.0, "msg": "1 phase(s)",
                   "data": {"total_s": 150.0}})
    view.on_event({"type": "load.sample", "t_rel": 40.0, "msg": "19.8 ops/s",
                   "data": {"ops_s": 19.8, "err_s": 0.4, "total_ops": 812,
                            "acked_writes": 555, "failed": 4,
                            "goals": [{"goal": "uptime", "value": "99.10%",
                                       "threshold": "min 98", "passed": True,
                                       "detail": ""}]}})
    view.on_event({"type": "topology", "t_rel": 40.0, "msg": "primary pg-1",
                   "data": {"primary": "pg-1", "replicas": ["pg-2", "pg-3"]}})

    console_ = Console(record=True, width=120)
    console_.print(view)
    out = console_.export_text()
    assert "ops/s" in out and "19.8" in out
    assert "812 ops" in out and "555 acked writes" in out
    assert "uptime" in out and "99.10%" in out
    assert "pg-1" in out and "primary" in out and "pg-3" in out
    assert "/150s" in out  # load progress
    assert "load.sample" not in out  # samples feed panes, not the event tail

def test_topology_text_renders_connection_tree():
    from k8ostester.cli.live import topology_text

    graph = {
        "nodes": [
            {"id": "loadgen", "role": "client", "detail": "5 clients, persistent"},
            {"id": "pooler-rw", "role": "proxy", "detail": "pgbouncer (rw)"},
            {"id": "pg-1", "role": "primary", "detail": "healthy"},
            {"id": "pg-2", "role": "replica", "detail": "healthy"},
            {"id": "pg-3", "role": "replica", "detail": "failed"},
        ],
        "edges": [
            {"source": "loadgen", "target": "pooler-rw"},
            {"source": "pooler-rw", "target": "pg-1"},
            {"source": "pg-1", "target": "pg-2", "detail": "sync"},
            {"source": "pg-1", "target": "pg-3", "detail": "detached"},
        ],
    }
    out = topology_text(graph).plain
    lines = out.splitlines()
    assert "▷ loadgen" in lines[0]
    assert "◆ pooler-rw" in out and "pgbouncer" in out
    assert "● pg-1" in out
    assert "sync─▶ ○ pg-2" in out.replace("├─", "").replace("└─", "")
    assert "detached" in out and "failed" in out
    # tree shape: replicas indented under the primary
    assert lines[-1].startswith("      ")

def test_topology_text_legacy_flat_form():
    from k8ostester.cli.live import topology_text
    out = topology_text({"primary": "pg-1", "replicas": ["pg-2"]}).plain
    assert "pg-1" in out and "primary" in out and "pg-2" in out

def test_topology_text_renders_replication_lag():
    from k8ostester.cli.live import topology_text
    graph = {
        "nodes": [{"id": "pg-1", "role": "primary"}, {"id": "pg-2", "role": "replica"}],
        "edges": [{"source": "pg-1", "target": "pg-2", "detail": "async", "lag": "+2.1s"}],
    }
    out = topology_text(graph).plain
    assert "async +2.1s" in out
