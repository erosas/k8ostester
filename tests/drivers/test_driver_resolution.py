import pytest
from pathlib import Path
from k8ostester.drivers import get_driver, _BUILTINS
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
