"""Kubernetes access — the ClusterClient primitive moved to the kernel
(``k8ostester_kernel.k8s``). Re-exported here so existing ``k8ostester.core.k8s``
imports keep working during the restructure. See docs/architecture-restructure.md.
"""

from k8ostester_kernel.k8s import ClusterClient, available_contexts, wait_until

__all__ = ["ClusterClient", "wait_until", "available_contexts"]
