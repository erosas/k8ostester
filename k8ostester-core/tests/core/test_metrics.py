
import pytest

from k8ostester.core.exceptions import K8osConfigError
from k8ostester.core.metrics import MetricStore, percentile


def test_metric_store(tmp_path):
    path = tmp_path / "metrics.jsonl"
    store = MetricStore(path)
    
    store.record("test", 123.456, foo="bar")
    store._file.write("\n")  # blank line: reader must skip it
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
    # nearest-rank on sorted values: index = round(p/100 * len) - 1,
    # using Python's banker's rounding (round(2.5) == 2)
    values = [10, 20, 30, 40, 50]
    assert percentile(values, 50) == 20
    assert percentile(values, 0) == 10
    assert percentile(values, 100) == 50
    assert percentile(values, 99) == 50

def test_percentile_empty():
    with pytest.raises(K8osConfigError, match="no values"):
        percentile([], 50)
