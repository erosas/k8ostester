import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
from k8ostester.drivers.base import TechnologyDriver
from k8ostester.drivers.generic import GenericDriver
from k8ostester.drivers import get_driver, _load_tech_driver
from k8ostester.core.experiment import ExperimentSpec
from k8ostester.core.exceptions import K8osConfigError, K8osDriverError

@pytest.fixture
def mock_context():
    k8s = MagicMock()
    events = MagicMock()
    spec = ExperimentSpec(name="test", technology="generic", dir=Path("/tmp/test"))
    return k8s, events, spec, "test-ns"

def test_base_driver_deploy(mock_context):
    k8s, events, spec, ns = mock_context
    driver = TechnologyDriver(k8s, spec, ns, events)
    
    k8s.apply_manifests.return_value = "pod/my-pod created\nservice/my-svc configured"
    
    driver.deploy()
    
    k8s.apply_manifests.assert_called_with(spec.manifests_dir, ns)
    assert events.emit.call_count == 2
    events.emit.assert_any_call("manifest.applied", "pod/my-pod created")
    events.emit.assert_any_call("manifest.applied", "service/my-svc configured")

def test_base_driver_wait_ready(mock_context):
    k8s, events, spec, ns = mock_context
    driver = TechnologyDriver(k8s, spec, ns, events)
    
    driver.wait_ready(timeout=100)
    k8s.wait_workloads_ready.assert_called_with(ns, 100)

def test_base_driver_install_prereqs_error(mock_context):
    k8s, events, spec, ns = mock_context
    spec.infra = ["chaos-mesh"]
    driver = TechnologyDriver(k8s, spec, ns, events)
    
    with pytest.raises(K8osConfigError, match="does not support infra"):
        driver.install_prereqs()

def test_base_driver_abstract_methods(mock_context):
    k8s, events, spec, ns = mock_context
    driver = TechnologyDriver(k8s, spec, ns, events)
    
    with pytest.raises(K8osDriverError, match="has no topology"):
        driver.topology()
    with pytest.raises(K8osDriverError, match="has no load generator"):
        driver.run_load(Path("/tmp"))
    with pytest.raises(K8osDriverError, match="has no load generator"):
        driver.start_load(Path("/tmp"))
    with pytest.raises(K8osDriverError, match="has no load generator"):
        driver.wait_load_started()
    with pytest.raises(K8osDriverError, match="has no load generator"):
        driver.wait_load_done()
    with pytest.raises(K8osDriverError, match="has no backup support"):
        driver.ensure_backup()
    with pytest.raises(K8osDriverError, match="has no 'check' verification"):
        driver.verify("check", {})

def test_generic_driver_ignores_infra(mock_context):
    # unlike the base class, GenericDriver accepts (and ignores) infra entries
    k8s, events, spec, ns = mock_context
    spec.infra = ["chaos-mesh"]
    GenericDriver(k8s, spec, ns, events).install_prereqs()  # must not raise

def test_base_driver_op_records_default_empty(mock_context):
    k8s, events, spec, ns = mock_context
    assert TechnologyDriver(k8s, spec, ns, events).op_records == []
