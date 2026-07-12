
import pytest

from k8ostester.drivers import get_driver
from k8ostester.drivers.generic import GenericDriver


def test_get_driver_builtin():
    assert get_driver("generic") == GenericDriver

def test_get_driver_custom(tmp_path):
    # Create a custom driver.py
    driver_file = tmp_path / "driver.py"
    driver_file.write_text("""
from k8ostester.drivers.base import TechnologyDriver
class CustomDriver(TechnologyDriver):
    pass
DRIVER = CustomDriver
""")
    
    driver_cls = get_driver("anything", experiment_dir=tmp_path)
    assert driver_cls.__name__ == "CustomDriver"

def test_get_driver_fail():
    with pytest.raises(KeyError, match="no driver for"):
        get_driver("invalid")

def test_get_driver_custom_auto_detects_single_subclass(tmp_path):
    # no DRIVER attribute: the single TechnologyDriver subclass is picked up
    (tmp_path / "driver.py").write_text("""
from k8ostester.drivers.base import TechnologyDriver
class OnlyDriver(TechnologyDriver):
    pass
""")
    assert get_driver("anything", experiment_dir=tmp_path).__name__ == "OnlyDriver"

def test_get_driver_custom_ambiguous_subclasses(tmp_path):
    (tmp_path / "driver.py").write_text("""
from k8ostester.drivers.base import TechnologyDriver
class DriverA(TechnologyDriver):
    pass
class DriverB(TechnologyDriver):
    pass
""")
    with pytest.raises(RuntimeError, match="exactly one TechnologyDriver subclass"):
        get_driver("anything", experiment_dir=tmp_path)

def test_get_driver_custom_module_cached(tmp_path):
    (tmp_path / "driver.py").write_text("""
from k8ostester.drivers.base import TechnologyDriver
class CachedDriver(TechnologyDriver):
    pass
""")
    first = get_driver("anything", experiment_dir=tmp_path)
    second = get_driver("anything", experiment_dir=tmp_path)
    assert first is second  # same module instance on repeat resolution

def test_get_driver_builtin_beaten_by_custom(tmp_path):
    # a driver.py above the experiment dir wins over the builtin registry
    (tmp_path / "driver.py").write_text("""
from k8ostester.drivers.base import TechnologyDriver
class OverrideDriver(TechnologyDriver):
    pass
DRIVER = OverrideDriver
""")
    exp_dir = tmp_path / "experiments" / "exp1"
    exp_dir.mkdir(parents=True)
    assert get_driver("generic", experiment_dir=exp_dir).__name__ == "OverrideDriver"

def test_get_driver_builtin_when_no_custom_driver_found(tmp_path):
    # experiment dir given but no driver.py anywhere above → builtin resolution
    exp_dir = tmp_path / "exp"
    exp_dir.mkdir()
    assert get_driver("generic", experiment_dir=exp_dir) == GenericDriver
