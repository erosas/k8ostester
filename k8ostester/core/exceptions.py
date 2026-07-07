"""Custom exceptions for K8osTester."""

class K8osError(Exception):
    """Base class for all k8ostester exceptions."""
    pass

class K8osConfigError(K8osError):
    """Raised when an experiment or CLI configuration is invalid."""
    pass

class K8osInfraError(K8osError):
    """Raised when there is an issue with the Kubernetes cluster or infra components."""
    pass

class K8osDriverError(K8osError):
    """Raised when a technology driver encounters an error."""
    pass
