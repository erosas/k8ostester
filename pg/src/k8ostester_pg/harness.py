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


def _seaweedfs_ready(k8s: ClusterClient, namespace: str) -> str | None:
    """The seaweedfs pod name once it is READY (serving S3), not just Running."""
    for p in k8s.core.list_namespaced_pod(namespace, label_selector="app=seaweedfs").items:
        if any(c.type == "Ready" and c.status == "True"
               for c in (p.status.conditions or [])):
            return p.metadata.name
    return None


def cluster_field(k8s: ClusterClient, namespace: str, field: str, name: str = "pg") -> str:
    """Read a field from the CNPG cluster's status (e.g. currentPrimary)."""
    obj = k8s.custom.get_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, namespace, "clusters", name
    )
    return str(obj.get("status", {}).get(field, ""))


def replicas(k8s: ClusterClient, namespace: str, name: str = "pg") -> list[str]:
    """Names of the current replica instance pods (not the primary) for a cluster.
    Scoped to the cluster label so two clusters in one namespace don't mix."""
    pods = k8s.core.list_namespaced_pod(
        namespace,
        label_selector=f"cnpg.io/instanceRole=replica,cnpg.io/cluster={name}",
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
    # the ideal config archives WAL to seaweedfs — the bucket must exist before
    # the cluster comes up, or archiving fails. Wait for seaweedfs to be READY
    # (a Running pod isn't yet serving S3), then create AND verify the bucket
    # (weed shell no-ops silently if the master isn't up yet).
    sw = wait_until(lambda: _seaweedfs_ready(k8s, namespace), timeout=180,
                    desc="seaweedfs ready")

    def _bucket_ready() -> bool:
        k8s.exec_pod(namespace, sw, ["sh", "-c",
                     'echo "s3.bucket.create -name backups" | weed shell'])
        out = k8s.exec_pod(namespace, sw, ["sh", "-c",
                     'echo "s3.bucket.list" | weed shell'])
        return "backups" in out

    wait_until(_bucket_ready, timeout=120, desc="backups bucket created")
    wait_until(
        lambda: cluster_field(k8s, namespace, "readyInstances") == "3",
        timeout=600, desc="cluster healthy")
