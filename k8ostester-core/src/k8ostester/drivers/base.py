"""Driver contract.

A driver owns everything technology-specific about an experiment: installing
prerequisites, deploying the config under test, readiness, topology (who is
primary — fault targeting needs it), load generation, integrity verification,
and backup/restore verbs. The runner only ever talks to this interface.

Phase 1 implements deploy/wait_ready; load, topology, integrity and backup land
with the CNPG driver in phase 2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from k8ostester.core.events import EventLog
from k8ostester.core.exceptions import K8osConfigError, K8osDriverError
from k8ostester.core.experiment import ExperimentSpec
from k8ostester.core.k8s import ClusterClient


class TechnologyDriver:
    def __init__(
        self,
        k8s: ClusterClient,
        spec: ExperimentSpec,
        namespace: str,
        events: EventLog,
    ):
        self.k8s = k8s
        self.spec = spec
        self.namespace = namespace
        self.events = events

    def install_prereqs(self) -> None:
        """Install cluster-level prerequisites (operators, object store).
        Must be idempotent; shared across experiments, not torn down per run."""
        if self.spec.infra:
            raise K8osConfigError(f"{type(self).__name__} does not support infra")

    def deploy(self) -> None:
        """Apply the config under test into the run namespace."""
        out = self.k8s.apply_manifests(self.spec.manifests_dir, self.namespace)
        for line in out.splitlines():
            self.events.emit("manifest.applied", line)

    def wait_ready(self, timeout: float = 300) -> None:
        self.k8s.wait_workloads_ready(self.namespace, timeout)

    def topology(self) -> dict[str, Any]:
        """Role → pod mapping for fault targeting (e.g. {'primary': 'pg-1'})."""
        raise K8osDriverError(f"{type(self).__name__} has no topology resolver")

    def run_load(self, run_dir: Path) -> None:
        """Run the experiment's load plan to completion, writing metrics.jsonl
        and journal.jsonl into run_dir."""
        raise K8osDriverError(f"{type(self).__name__} has no load generator")

    def start_load(self, run_dir: Path) -> None:
        raise K8osDriverError(f"{type(self).__name__} has no load generator")

    def wait_load_started(self, timeout: float = 300) -> float:
        """Block until ops are flowing; returns the fault-timeline zero point."""
        raise K8osDriverError(f"{type(self).__name__} has no load generator")

    def wait_load_done(self) -> None:
        raise K8osDriverError(f"{type(self).__name__} has no load generator")

    def emit_live_telemetry(self) -> None:
        """Best-effort progress hook: emit load.sample/topology events for the
        live run view. Called by the runner while it waits out the fault
        timeline (and by drivers from their own wait loops); must never raise."""

    def topology_graph(self) -> dict[str, Any]:
        """Component graph for the live view: how traffic and replication flow.

        {"nodes": [{"id", "role", "detail"?}], "edges": [{"source", "target",
        "detail"?}]} — roles: client | proxy | primary | replica. Edge detail
        carries the relationship (service name, sync/async/quorum, ...).
        Drivers override this with their own discovery (CR status, database
        introspection); the default derives a flat primary→replicas graph
        from topology()."""
        try:
            topo = self.topology()
        except Exception:
            return {"nodes": [], "edges": []}
        primary = topo.get("primary")
        replicas = topo.get("replicas", [])
        nodes = [{"id": primary, "role": "primary"}] if primary else []
        nodes += [{"id": r, "role": "replica"} for r in replicas]
        edges = [{"source": primary, "target": r} for r in replicas if primary]
        return {**topo, "nodes": nodes, "edges": edges}

    @property
    def op_records(self) -> list[dict]:
        """Per-operation records from the completed load run (goal evidence)."""
        return []

    def start_load_session(self, run_dir: Path, rate: float, clients: int,
                           replicas: int) -> None:
        """Interactive load pool (`k8ost session`): every pod a self-contained
        unit of load; scaling replicas scales total load."""
        raise K8osDriverError(f"{type(self).__name__} has no interactive load support")

    def scale_load(self, replicas: int) -> None:
        raise K8osDriverError(f"{type(self).__name__} has no interactive load support")

    def set_load_rate(self, rate: float, clients: int) -> None:
        raise K8osDriverError(f"{type(self).__name__} has no interactive load support")

    def stop_load_session(self) -> str:
        raise K8osDriverError(f"{type(self).__name__} has no interactive load support")

    def session_actions(self) -> list[dict]:
        """Tech-specific ops for the interactive session UI. Each entry:
        {id, label, variant?, description?} — the framework renders them as
        controls and dispatches run_session_action(id); the plugin defines
        what they mean (a Postgres driver offers backup/PITR, a Kafka driver
        would offer partition reassignment, ...)."""
        return []

    def run_session_action(self, action_id: str) -> str:
        """Execute a session action; returns a one-line summary for the log."""
        raise K8osDriverError(f"{type(self).__name__} has no session actions")

    def ensure_backup(self) -> None:
        """Take a base backup now (before load, so PITR can replay forward)."""
        raise K8osDriverError(f"{type(self).__name__} has no backup support")

    def verify(self, check: str, config: dict) -> dict:
        """Run a verify step; returns {check, passed, detail}."""
        raise K8osDriverError(f"{type(self).__name__} has no '{check}' verification")
