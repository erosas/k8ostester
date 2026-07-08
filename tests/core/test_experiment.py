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
