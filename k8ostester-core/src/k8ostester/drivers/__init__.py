"""Driver resolution.

Built-in technologies ship inside the framework (D20) and are resolved by
name from the registry — a config repo needs nothing but experiment
directories. A `driver.py` found by walking up from the experiment directory
still takes precedence (D15's escape hatch for custom/forked drivers living
beside their experiments).
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

from k8ostester.drivers.base import TechnologyDriver

# built-ins resolve lazily (import path strings): drivers import drivers.base,
# so importing their classes here would make this module circular with them
_BUILTINS: dict[str, str] = {
    "generic": "k8ostester.drivers.generic:GenericDriver",
    "postgres-cnpg": "k8ostester.technologies.postgres_cnpg.driver:CnpgDriver",
}


def _resolve_builtin(technology: str) -> type[TechnologyDriver]:
    module_path, _, attr = _BUILTINS[technology].partition(":")
    return getattr(importlib.import_module(module_path), attr)


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


def detect_technology(k8s, namespace: str) -> str | None:
    """Attach-mode discovery: the first built-in whose driver recognizes what
    is deployed in the namespace."""
    for technology in _BUILTINS:
        if _resolve_builtin(technology).detects(k8s, namespace):
            return technology
    return None


def get_driver(technology: str, experiment_dir: Path | None = None) -> type[TechnologyDriver]:
    if experiment_dir is not None:
        driver = _load_tech_driver(experiment_dir.resolve())
        if driver is not None:
            return driver
    if technology in _BUILTINS:
        return _resolve_builtin(technology)
    raise KeyError(
        f"no driver for {technology!r}: no driver.py above the experiment and no built-in"
    )
