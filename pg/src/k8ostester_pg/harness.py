"""Shared provisioning for CNPG experiments — keeps the linear scripts thin.

Plain functions on a kernel ``ClusterClient`` (not a framework): deploy the ideal
config, wire the shared-console labelling, make the WAL bucket, and expose small
cluster helpers. An experiment is then: ``deploy_ideal_config`` → chaos → verify
→ verdict. See docs/architecture-restructure.md.
"""
from __future__ import annotations

from pathlib import Path

from k8ostester_kernel.k8s import ClusterClient, wait_until

# the ideal config manifests (the pg vertical's canonical config)
IDEAL_MANIFESTS = Path(__file__).parents[2] / "testbed" / "manifests"
CNPG_GROUP, CNPG_VERSION = "postgresql.cnpg.io", "v1"


def cluster_field(k8s: ClusterClient, namespace: str, field: str, name: str = "pg") -> str:
    """Read a field from the CNPG cluster's status (e.g. currentPrimary)."""
    obj = k8s.custom.get_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, namespace, "clusters", name
    )
    return str(obj.get("status", {}).get(field, ""))


def replicas(k8s: ClusterClient, namespace: str) -> list[str]:
    """Names of the current replica instance pods (not the primary)."""
    pods = k8s.core.list_namespaced_pod(
        namespace, label_selector="cnpg.io/instanceRole=replica"
    )
    return [p.metadata.name for p in pods.items]


def print_verdict(verdict: dict) -> int:
    """Print a run verdict (SLOs + verifies) and return its exit code."""
    print(f"\n{verdict['experiment']}: {verdict['verdict'].upper()}")
    for name, r in verdict["slo"].items():
        print(f"  slo   {'✓' if r['pass'] else '✗'} {name}: {r['observed']:.4g} "
              f"({r['direction']} {r['threshold']})")
    for name, ok in verdict["verifies"].items():
        print(f"  check {'✓' if ok else '✗'} {name}")
    return 0 if verdict["verdict"] == "pass" else 1


def deploy_ideal_config(
    k8s: ClusterClient,
    namespace: str,
    experiment: str,
    manifests: Path = IDEAL_MANIFESTS,
) -> None:
    """Deploy the ideal config + app into a fresh namespace, ready for chaos.

    Labels the namespace and the app pods with the experiment (so the shared
    console scopes metrics), creates the WAL bucket the config archives to, and
    waits until the cluster is healthy (3/3).
    """
    k8s.create_namespace(namespace, labels={"k8ostester.io/experiment": experiment})
    for m in sorted(manifests.glob("*.yaml")):
        k8s.apply_manifests(m, namespace)
    # scope the app's metrics by experiment in the shared console
    k8s.apps.patch_namespaced_deployment(
        "app", namespace,
        {"spec": {"template": {"metadata": {
            "labels": {"k8ostester.io/experiment": experiment}}}}},
    )
    # the ideal config archives WAL to seaweedfs — make the bucket before the
    # cluster comes up, or archiving fails and it never goes healthy
    sw = wait_until(
        lambda: [p.metadata.name for p in
                 k8s.core.list_namespaced_pod(namespace, label_selector="app=seaweedfs").items
                 if p.status.phase == "Running"],
        timeout=180, desc="seaweedfs ready")[0]
    k8s.exec_pod(namespace, sw, ["sh", "-c", 'echo "s3.bucket.create -name backups" | weed shell'])
    wait_until(
        lambda: cluster_field(k8s, namespace, "readyInstances") == "3",
        timeout=600, desc="cluster healthy")
