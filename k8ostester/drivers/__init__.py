"""Driver resolution.

Built-in technologies ship inside the framework (D20) and are resolved by
name from the registry — a config repo needs nothing but experiment
directories. A `driver.py` found by walking up from the experiment directory
still takes precedence (D15's escape hatch for custom/forked drivers living
beside their experiments).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from k8ostester.drivers.base import TechnologyDriver
from k8ostester.drivers.generic import GenericDriver
from k8ostester.technologies.postgres_cnpg.driver import CnpgDriver

_BUILTINS: dict[str, type[TechnologyDriver]] = {
    "generic": GenericDriver,
    "postgres-cnpg": CnpgDriver,
}


def _load_tech_driver(experiment_dir: Path) -> type[TechnologyDriver] | None:
    for parent in [experiment_dir, *experiment_dir.parents]:
        candidate = parent / "driver.py"
        if not candidate.exists():
            continue
        module_name = f"k8ost_tech_{parent.name.replace('-', '_')}"
        if module_name in sys.modules:
            module = sys.modules[module_name]
        else:
            spec = importlib.util.spec_from_file_location(module_name, candidate)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        driver = getattr(module, "DRIVER", None)
        if driver is None:
            subclasses = [
                v
                for v in vars(module).values()
                if isinstance(v, type)
                and issubclass(v, TechnologyDriver)
                and v is not TechnologyDriver
            ]
            if len(subclasses) != 1:
                raise RuntimeError(
                    f"{candidate} must define DRIVER or exactly one TechnologyDriver subclass"
                )
            driver = subclasses[0]
        return driver
    return None


def get_driver(technology: str, experiment_dir: Path | None = None) -> type[TechnologyDriver]:
    if experiment_dir is not None:
        driver = _load_tech_driver(experiment_dir.resolve())
        if driver is not None:
            return driver
    if technology in _BUILTINS:
        return _BUILTINS[technology]
    raise KeyError(
        f"no driver for {technology!r}: no driver.py above the experiment and no built-in"
    )
