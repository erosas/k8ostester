"""Generic driver: deploy arbitrary manifests and wait for workloads.

Used for framework smoke tests, and it is the seed of the generic-app driver
(test-your-own-application mode): faults target `cluster` + namespace + label
selector, goals come from the app's own metrics.
"""

from __future__ import annotations

from k8ostester.drivers.base import TechnologyDriver


class GenericDriver(TechnologyDriver):
    def install_prereqs(self) -> None:
        pass  # a generic app has no operator or shared infra
