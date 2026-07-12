from unittest.mock import MagicMock

import pytest

from k8ostester.core.experiment import FaultSpec
from k8ostester.workers.base import Worker
from k8ostester.workers.node_drain import NodeDrainWorker
from k8ostester.workers.pod_kill import PodKillWorker
from k8ostester.workers.process_kill import ProcessKillWorker


def test_worker_resolve_pod_direct(mock_context):
    k8s, driver, events, ns = mock_context
    worker = Worker(k8s, driver, ns, events)
    assert worker.resolve_pod({"pod": "my-pod"}) == "my-pod"

def test_worker_resolve_pod_role_primary(mock_context):
    k8s, driver, events, ns = mock_context
    driver.topology.return_value = {"primary": "pod-1", "replicas": ["pod-2"]}
    worker = Worker(k8s, driver, ns, events)
    assert worker.resolve_pod({"role": "primary"}) == "pod-1"

def test_worker_resolve_pod_role_replica(mock_context):
    k8s, driver, events, ns = mock_context
    driver.topology.return_value = {"primary": "pod-1", "replicas": ["pod-2"]}
    worker = Worker(k8s, driver, ns, events)
    assert worker.resolve_pod({"role": "replica"}) == "pod-2"

def test_worker_resolve_pod_no_replicas(mock_context):
    k8s, driver, events, ns = mock_context
    driver.topology.return_value = {"primary": "pod-1", "replicas": []}
    worker = Worker(k8s, driver, ns, events)
    with pytest.raises(RuntimeError, match="no replica to target"):
        worker.resolve_pod({"role": "replica"})

def test_worker_resolve_node_direct(mock_context):
    k8s, driver, events, ns = mock_context
    worker = Worker(k8s, driver, ns, events)
    assert worker.resolve_node({"node": "node-1"}) == "node-1"

def test_worker_resolve_node_of(mock_context):
    k8s, driver, events, ns = mock_context
    driver.topology.return_value = {"primary": "pod-1"}
    pod_obj = MagicMock()
    pod_obj.spec.node_name = "node-1"
    k8s.core.read_namespaced_pod.return_value = pod_obj
    
    worker = Worker(k8s, driver, ns, events)
    assert worker.resolve_node({"node_of": "primary"}) == "node-1"
    k8s.core.read_namespaced_pod.assert_called_with("pod-1", ns)

def test_pod_kill_worker(mock_context):
    k8s, driver, events, ns = mock_context
    worker = PodKillWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="pod_kill", target={"pod": "kill-me"})
    
    worker.execute(fault)
    
    k8s.core.delete_namespaced_pod.assert_called_with("kill-me", ns, grace_period_seconds=0)
    events.emit.assert_called_once()

def test_node_drain_worker(mock_context):
    k8s, driver, events, ns = mock_context
    worker = NodeDrainWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="node_drain", target={"node": "node-1"})
    
    # Setup victim pods
    pod1 = MagicMock()
    pod1.metadata.name = "pod-1"
    pod1.spec.node_name = "node-1"
    pod2 = MagicMock()
    pod2.metadata.name = "pod-2"
    pod2.spec.node_name = "node-2" # Different node
    k8s.core.list_namespaced_pod.return_value.items = [pod1, pod2]
    
    cleanup = worker.execute(fault)
    
    # Check node was cordoned
    k8s.core.patch_node.assert_called_with("node-1", {"spec": {"unschedulable": True}})
    # Check victim pod was deleted
    k8s.core.delete_namespaced_pod.assert_called_with("pod-1", ns, grace_period_seconds=0)
    events.emit.assert_called_with("fault.node_drain", "cordoned node-1, evicted 1 pod(s): pod-1", node="node-1", pods=["pod-1"])
    
    # Check cleanup (uncordon)
    assert cleanup is not None
    cleanup()
    k8s.core.patch_node.assert_called_with("node-1", {"spec": {"unschedulable": False}})
    events.emit.assert_called_with("fault.cleanup", "uncordoned node-1")

def test_process_kill_worker(mock_context):
    k8s, driver, events, ns = mock_context
    worker = ProcessKillWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="process_kill", target={"pod": "my-pod"}, params={"container": "main"})
    
    worker.execute(fault)
    
    k8s.exec_pod.assert_called_with(ns, "my-pod", ["sh", "-c", "kill -9 1"], container="main")
    events.emit.assert_called_with("fault.process_kill", "kill -9 pid 1 in my-pod (main)", pod="my-pod", container="main")

def test_process_kill_worker_exec_fails(mock_context):
    k8s, driver, events, ns = mock_context
    worker = ProcessKillWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="process_kill", target={"pod": "my-pod"})
    
    k8s.exec_pod.side_effect = RuntimeError("stream reset")
    
    # Should not raise exception
    worker.execute(fault)
    
    k8s.exec_pod.assert_called_with(ns, "my-pod", ["sh", "-c", "kill -9 1"], container=None)
    events.emit.assert_called_with("fault.process_kill", "kill -9 pid 1 in my-pod", pod="my-pod", container=None)

def test_worker_execute_not_implemented(mock_context):
    k8s, driver, events, ns = mock_context
    with pytest.raises(NotImplementedError):
        Worker(k8s, driver, ns, events).execute(FaultSpec(at="0s", worker="x"))

def test_worker_resolve_pod_unknown_role(mock_context):
    k8s, driver, events, ns = mock_context
    driver.topology.return_value = {"primary": "pod-1", "replicas": []}
    with pytest.raises(ValueError, match="unknown role"):
        Worker(k8s, driver, ns, events).resolve_pod({"role": "leader"})

def test_worker_resolve_pod_invalid_target(mock_context):
    k8s, driver, events, ns = mock_context
    with pytest.raises(ValueError, match="target needs"):
        Worker(k8s, driver, ns, events).resolve_pod({"foo": "bar"})

def test_worker_resolve_node_invalid_target(mock_context):
    k8s, driver, events, ns = mock_context
    with pytest.raises(ValueError, match="target needs"):
        Worker(k8s, driver, ns, events).resolve_node({"foo": "bar"})
