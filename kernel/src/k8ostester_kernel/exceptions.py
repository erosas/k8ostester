"""Base exceptions shared by the kernel and the verticals.

``K8osError`` is the common base: catching it also catches the kernel's
``K8osInfraError``. Verticals may subclass ``K8osError`` when they want their
errors to participate in that shared catch (otherwise they raise plain
exceptions).
"""


class K8osError(Exception):
    """Base class for all k8ostester errors."""


class K8osInfraError(K8osError):
    """A problem with the Kubernetes cluster or an infra component."""
