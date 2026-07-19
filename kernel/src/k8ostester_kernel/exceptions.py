"""Base exceptions shared by the kernel and the verticals.

Verticals subclass ``K8osError`` for their own error types; because the kernel
raises ``K8osInfraError`` from these same classes, a vertical's
``except K8osError`` catches kernel-raised infra errors too.
"""


class K8osError(Exception):
    """Base class for all k8ostester errors."""


class K8osInfraError(K8osError):
    """A problem with the Kubernetes cluster or an infra component."""
