"""Unit tests for the capability report — pure logic, no cluster."""
from k8ostester_kernel.capabilities import Capabilities, format_report


def caps(**over):
    base = dict(context="kind", server_version="v1.30.0", zones=["a", "b", "c"],
                worker_count=3, network_policy_enforced=True, snapshots=False,
                operators={"cloudnative-pg": True, "chaos-mesh": False})
    return Capabilities(**{**base, **over})


def test_multi_node_needs_two_workers():
    assert caps(worker_count=3).multi_node is True
    assert caps(worker_count=1).multi_node is False


def test_report_flags_each_capability():
    r = format_report(caps())
    assert "3 worker(s)" in r
    assert "a, b, c" in r                       # zones listed
    assert "CNI enforces NetworkPolicy" in r
    assert "cloudnative-pg" in r


def test_report_shows_missing_capabilities():
    r = format_report(caps(worker_count=1, zones=[], network_policy_enforced=False))
    assert "no zone labels" in r
    assert "does not enforce" in r
    assert "✘ node/AZ faults" in r
