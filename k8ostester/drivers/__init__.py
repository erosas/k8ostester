"""Technology drivers. Register each driver here by its `technology` value."""

from __future__ import annotations

from k8ostester.drivers.base import TechnologyDriver
from k8ostester.drivers.generic import GenericDriver

_REGISTRY: dict[str, type[TechnologyDriver]] = {
    "generic": GenericDriver,
}


def get_driver(technology: str) -> type[TechnologyDriver]:
    if technology not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"unknown technology {technology!r} (known: {known})")
    return _REGISTRY[technology]
