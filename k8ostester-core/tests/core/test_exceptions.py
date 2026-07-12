from k8ostester.core.exceptions import K8osConfigError, K8osDriverError, K8osError, K8osInfraError


def test_exceptions():
    assert issubclass(K8osConfigError, K8osError)
    assert issubclass(K8osInfraError, K8osError)
    assert issubclass(K8osDriverError, K8osError)
