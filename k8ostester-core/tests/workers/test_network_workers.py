from unittest.mock import ANY, patch

import pytest
from kubernetes import client

from k8ostester.core.experiment import FaultSpec
from k8ostester.workers.network import (
    PARTITION_LABEL,
    NetworkDelayWorker,
    NetworkLossWorker,
    NetworkPartitionWorker,
)

# -- native NetworkPolicy partition (the default engine) -------------------------

@patch("k8ostester.workers.network.load_resource")
def test_partition_native_networkpolicy(mock_load, mock_context):
    k8s, driver, events, ns = mock_context
    k8s.has_crd.return_value = True  # a policy-enforcing CNI is present
    mock_load.return_value = {"metadata": {"name": "np"}}
    worker = NetworkPartitionWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="network_partition", target={"pod": "my-pod"}, duration="30s")

    cleanup = worker.execute(fault)

    # a native NetworkPolicy is created; NO chaos-mesh CR
    k8s.networking.create_namespaced_network_policy.assert_called_once_with(ns, {"metadata": {"name": "np"}})
    k8s.custom.create_namespaced_custom_object.assert_not_called()
    # the target pod is marked so the policy selects exactly it
    label_patch = k8s.core.patch_namespaced_pod.call_args_list[0]
    assert label_patch.args[0] == "my-pod"
    marker = label_patch.args[2]["metadata"]["labels"][PARTITION_LABEL]
    assert mock_load.call_args[0][1]["MARKER"] == marker
    events.emit.assert_any_call(
        "fault.network_partition", ANY, pod="my-pod", duration="30s", policy=ANY)

    # heal: policy deleted and the marker label removed
    cleanup()
    k8s.networking.delete_namespaced_network_policy.assert_called_once()
    assert k8s.core.patch_namespaced_pod.call_args.args[2]["metadata"]["labels"] == {PARTITION_LABEL: None}


@patch("k8ostester.workers.network.load_resource", return_value={})
def test_partition_native_warns_when_cni_not_enforcing(mock_load, mock_context):
    k8s, driver, events, ns = mock_context
    k8s.has_crd.return_value = False  # kindnet-class CNI: no NetworkPolicy enforcement
    worker = NetworkPartitionWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="network_partition", target={"pod": "my-pod"}, duration="30s")

    worker.execute(fault).__call__()  # execute + immediate heal
    warns = [c for c in events.emit.call_args_list if c.args[0] == "capability.warn"]
    assert any("does not appear to enforce NetworkPolicy" in c.args[1] for c in warns)


def test_partition_native_requires_duration(mock_context):
    k8s, driver, events, ns = mock_context
    worker = NetworkPartitionWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="network_partition", target={"pod": "my-pod"})
    with pytest.raises(ValueError, match="needs a 'duration'"):
        worker.execute(fault)


def test_partition_rejects_unknown_engine(mock_context):
    k8s, driver, events, ns = mock_context
    worker = NetworkPartitionWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="network_partition", target={"pod": "my-pod"},
                      duration="30s", params={"engine": "iptables"})
    with pytest.raises(ValueError, match="engine must be"):
        worker.execute(fault)


# -- chaos-mesh partition (opt-in via engine) ------------------------------------

@patch("k8ostester.workers.network.load_resource")
def test_partition_chaos_mesh_engine(mock_load, mock_context):
    k8s, driver, events, ns = mock_context
    k8s.has_crd.return_value = True
    mock_load.return_value = {"metadata": {"name": "dummy"}}
    worker = NetworkPartitionWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="network_partition", target={"pod": "my-pod"},
                      duration="30s", params={"engine": "chaos-mesh"})

    cleanup = worker.execute(fault)

    args = k8s.custom.create_namespaced_custom_object.call_args[0]
    assert args[0] == "chaos-mesh.org" and args[2] == ns
    k8s.networking.create_namespaced_network_policy.assert_not_called()
    events.emit.assert_any_call(
        "fault.network_partition", ANY, pod="my-pod", duration="30s", chaos=ANY)
    cleanup()
    k8s.custom.delete_namespaced_custom_object.assert_called_once()


def test_partition_chaos_mesh_missing_crd(mock_context):
    k8s, driver, events, ns = mock_context
    k8s.has_crd.return_value = False
    worker = NetworkPartitionWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="network_partition", target={"pod": "my-pod"},
                      duration="30s", params={"engine": "chaos-mesh"})
    with pytest.raises(RuntimeError, match="needs Chaos Mesh"):
        worker.execute(fault)


@patch("k8ostester.workers.network.load_resource", return_value={"metadata": {"name": "x"}})
def test_partition_chaos_cleanup_other_error_raises(mock_load, mock_context):
    k8s, driver, events, ns = mock_context
    k8s.has_crd.return_value = True
    worker = NetworkPartitionWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="network_partition", target={"pod": "my-pod"},
                      duration="30s", params={"engine": "chaos-mesh"})
    cleanup = worker.execute(fault)

    k8s.custom.delete_namespaced_custom_object.side_effect = client.ApiException(status=500)
    with pytest.raises(client.ApiException):
        cleanup()


@patch("k8ostester.workers.network.load_resource", return_value={"metadata": {"name": "x"}})
def test_partition_chaos_cleanup_404_ok(mock_load, mock_context):
    k8s, driver, events, ns = mock_context
    k8s.has_crd.return_value = True
    worker = NetworkPartitionWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="network_partition", target={"pod": "my-pod"},
                      duration="30s", params={"engine": "chaos-mesh"})
    cleanup = worker.execute(fault)
    k8s.custom.delete_namespaced_custom_object.side_effect = client.ApiException(status=404)
    cleanup()  # namespace teardown may have raced us — must not raise


# -- loss / delay (always chaos-mesh; no native equivalent) ----------------------

@patch("k8ostester.workers.network.load_resource")
def test_network_loss_worker(mock_load, mock_context):
    k8s, driver, events, ns = mock_context
    worker = NetworkLossWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="network_loss", target={"pod": "my-pod"}, duration="30s", params={"loss": "20"})
    k8s.has_crd.return_value = True
    mock_load.return_value = {}
    worker.execute(fault)
    assert mock_load.call_args[0][1]["LOSS"] == "20"


@patch("k8ostester.workers.network.load_resource")
def test_network_delay_worker(mock_load, mock_context):
    k8s, driver, events, ns = mock_context
    worker = NetworkDelayWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="network_delay", target={"pod": "my-pod"}, duration="30s", params={"latency": "200ms", "jitter": "10ms"})
    k8s.has_crd.return_value = True
    mock_load.return_value = {}
    worker.execute(fault)
    variables = mock_load.call_args[0][1]
    assert variables["LATENCY"] == "200ms"
    assert variables["JITTER"] == "10ms"


def test_network_loss_requires_chaos_mesh(mock_context):
    k8s, driver, events, ns = mock_context
    k8s.has_crd.return_value = False
    worker = NetworkLossWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="network_loss", target={"pod": "my-pod"}, duration="30s")
    with pytest.raises(RuntimeError, match="needs Chaos Mesh"):
        worker.execute(fault)


@patch("k8ostester.workers.network.load_resource", return_value={})
def test_partition_auto_uses_chaos_mesh_when_cni_wont_enforce(mock_load, mock_context):
    """auto (the default) falls back to chaos-mesh IF present when the CNI does
    not enforce NetworkPolicy — but never installs it."""
    k8s, driver, events, ns = mock_context
    # no policy-enforcing CNI CRD, but chaos-mesh IS installed
    k8s.has_crd.side_effect = lambda crd: crd == "networkchaos.chaos-mesh.org"
    worker = NetworkPartitionWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="network_partition", target={"pod": "my-pod"}, duration="30s")

    worker.execute(fault)  # engine defaults to auto
    k8s.custom.create_namespaced_custom_object.assert_called_once()      # chaos path
    k8s.networking.create_namespaced_network_policy.assert_not_called()


@patch("k8ostester.workers.network.load_resource", return_value={})
def test_partition_auto_uses_netpol_when_cni_enforces(mock_load, mock_context):
    k8s, driver, events, ns = mock_context
    k8s.has_crd.side_effect = lambda crd: crd == "felixconfigurations.crd.projectcalico.org"
    worker = NetworkPartitionWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="network_partition", target={"pod": "my-pod"}, duration="30s")

    worker.execute(fault).__call__()
    k8s.networking.create_namespaced_network_policy.assert_called_once()  # native path
    k8s.custom.create_namespaced_custom_object.assert_not_called()
