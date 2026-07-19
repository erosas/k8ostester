"""Custom exceptions for K8osTester.

The base classes (``K8osError``, ``K8osInfraError``) live in the kernel so that
``except K8osError`` here also catches kernel-raised infra errors. The
config/driver errors are core-specific. See docs/architecture-restructure.md.
"""

from k8ostester_kernel.exceptions import K8osError, K8osInfraError


class K8osConfigError(K8osError):
    """Raised when an experiment or CLI configuration is invalid."""


class K8osDriverError(K8osError):
    """Raised when a technology driver encounters an error."""


__all__ = ["K8osError", "K8osInfraError", "K8osConfigError", "K8osDriverError"]
