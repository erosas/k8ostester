"""k8ostester PostgreSQL/CloudNativePG vertical.

Direct, tech-specific logic built on the kernel primitives — no framework
abstraction. Holds the k8ost-console control plane (``server``), the linear
experiment scripts (``pg/experiments``), the production-readiness testbed
(``pg/testbed``), and the standard CNPG SLO checks (``slo``). See
docs/architecture-restructure.md.
"""

from k8ostester_pg.slo import default_checks

__all__ = ["default_checks"]
