"""k8ostester kernel — shared primitives for the technology verticals.

Primitives, not a framework: verticals import what they need (k8s access, chaos
primitives, the metrics console, the SLO-query verdict) and write their own
tech-specific logic directly. There is deliberately no "every tech implements
this" driver interface here. See docs/architecture-restructure.md.
"""

from k8ostester_kernel.experiment import Run
from k8ostester_kernel.verdict import SloCheck, evaluate_slos, verdict

__all__ = ["Run", "SloCheck", "evaluate_slos", "verdict"]
