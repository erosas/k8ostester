import pytest
from k8ostester.core.experiment import parse_duration, parse_rate, GoalSpec, ExperimentSpec, LoadPhase, FaultSpec

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

def test_experiment_spec_logic():
    spec = ExperimentSpec(name="test", technology="postgres")
    assert spec.namespace_base == "exp-test"
    
    phase = LoadPhase(duration="1m", rate="100/s")
    assert phase.duration_s == 60.0
    
    fault = FaultSpec(at="30s", worker="pod_kill")
    assert fault.at_s == 30.0

def test_experiment_spec_manifests_dir(tmp_path):
    d = tmp_path / "exp"
    d.mkdir()
    (d / "manifests").mkdir()
    spec = ExperimentSpec(name="test", technology="pg", dir=d)
    assert spec.manifests_dir == (d / "manifests").resolve()

def test_parse_rate_none_and_numbers():
    assert parse_rate(None) == 0.0
    assert parse_rate(5) == 5.0

def test_parse_duration_bare_number():
    assert parse_duration(30) == 30.0

def test_goal_needs_metric_or_check():
    from k8ostester.core.exceptions import K8osConfigError
    with pytest.raises(K8osConfigError, match="either 'metric' or 'check'"):
        GoalSpec(check=None)

def test_load_experiment(tmp_path):
    from k8ostester.core.experiment import load_experiment
    d = tmp_path / "exp"
    d.mkdir()

    with pytest.raises(FileNotFoundError, match="no experiment.yaml"):
        load_experiment(d)

    (d / "experiment.yaml").write_text("name: t\ntechnology: generic\n")
    with pytest.raises(FileNotFoundError, match="manifests directory not found"):
        load_experiment(d)

    (d / "manifests").mkdir()
    spec = load_experiment(d)
    assert spec.name == "t"
    assert spec.dir == d.resolve()
