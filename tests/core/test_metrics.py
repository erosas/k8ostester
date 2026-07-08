import pytest
import json
from k8ostester.core.metrics import MetricStore, percentile
from k8ostester.core.exceptions import K8osConfigError

def test_metric_store(tmp_path):
    path = tmp_path / "metrics.jsonl"
    store = MetricStore(path)
    
    store.record("test", 123.456, foo="bar")
    store.record("op", 124.0, op="write", ok=True)
    store.flush()
    store.close()
    
    recs = list(MetricStore.read(path))
    assert len(recs) == 2
    assert recs[0]["kind"] == "test"
    assert recs[0]["ts"] == 123.456
    assert recs[0]["foo"] == "bar"
    
    # Test filtered read
    op_recs = list(MetricStore.read(path, kind="op"))
    assert len(op_recs) == 1
    assert op_recs[0]["op"] == "write"

def test_percentile():
    values = [10, 20, 30, 40, 50]
    # Nearest rank: round(p/100 * N)
    # p=50: round(0.5 * 5) = round(2.5) = 3 -> index 2 (value 30)
    # k8ostester uses round(p/100 * len) - 1
    # Actually, round(0.5 * 5) is 2 in some python versions (round to even), or 3 in others.
    # In Python 3, round(2.5) is 2.
    # len=5, p=50: 50/100 * 5 = 2.5. round(2.5) = 2. index = 2 - 1 = 1. values[1] = 20.
    assert percentile(values, 50) == 20
    assert percentile(values, 0) == 10
    assert percentile(values, 100) == 50
    
    # p=99 on 5 values: 0.99 * 5 = 4.95. round(4.95) = 5. index = 5 - 1 = 4 -> 50
    assert percentile(values, 99) == 50

def test_percentile_empty():
    with pytest.raises(K8osConfigError, match="no values"):
        percentile([], 50)
