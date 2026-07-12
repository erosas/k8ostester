import pytest

from k8ostester.workers import get_worker
from k8ostester.workers.pod_kill import PodKillWorker


def test_get_worker_success():
    assert get_worker("pod_kill") == PodKillWorker

def test_get_worker_fail():
    with pytest.raises(KeyError, match="unknown worker"):
        get_worker("invalid")
