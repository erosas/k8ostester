"""k8ostester PostgreSQL/CloudNativePG vertical.

Direct, tech-specific logic built on the kernel primitives — no framework
abstraction. Holds the production-readiness testbed (``pg/testbed``), the
standard CNPG SLO checks (``slo``), and, over time, the linear experiment
scripts that replace the old generic engine. See docs/architecture-restructure.md.
"""

from k8ostester_pg.slo import default_checks

__all__ = ["default_checks"]
