"""Run lifecycle orchestration.

provision → wait-ready → load+faults (phase 2/3) → verify (phase 2) →
goal evaluation (phase 3) → teardown (unless --keep)

Each run gets its own namespace (`<base>-<run-id>`) and its own results
directory. The runner is deliberately technology-blind: everything specific
goes through the driver.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from k8ostester.core.capabilities import probe
from k8ostester.core.events import EventLog
from k8ostester.core.experiment import ExperimentSpec
from k8ostester.core.k8s import ClusterClient
from k8ostester.drivers import get_driver

RUN_LABEL = "k8ostester.io/run"


class RunResult:
    def __init__(self, run_id: str, run_dir: Path):
        self.run_id = run_id
        self.run_dir = run_dir
        self.status = "unknown"
        self.namespace: str | None = None
        self.error: str | None = None
        self.verifications: list[dict] = []


class Runner:
    def __init__(
        self,
        spec: ExperimentSpec,
        results_root: Path = Path("results"),
        keep: bool = False,
        context_override: str | None = None,
        on_event=None,
    ):
        self.spec = spec
        self.keep = keep
        self.context = context_override or spec.cluster.context
        run_stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.run_id = run_stamp
        self.run_dir = results_root / spec.name / run_stamp
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events = EventLog(self.run_dir / "events.jsonl", on_event=on_event)
        self.namespace = f"{self.spec.namespace_base}-{run_stamp.replace('-', '')[-6:]}"

    def run(self) -> RunResult:
        result = RunResult(self.run_id, self.run_dir)
        result.namespace = self.namespace
        k8s = ClusterClient(self.context)
        started = time.time()
        try:
            self._snapshot_spec()
            self._check_capabilities(k8s)

            driver_cls = get_driver(self.spec.technology)
            driver = driver_cls(k8s, self.spec, self.namespace, self.events)

            self.events.emit("run.start", f"experiment {self.spec.name}",
                             namespace=self.namespace, context=self.context or "(current)")

            self.events.emit("prereqs.install", "installing prerequisites")
            driver.install_prereqs()

            self.events.emit("namespace.create", self.namespace)
            k8s.create_namespace(self.namespace, labels={RUN_LABEL: self.run_id})

            self.events.emit("deploy.start", str(self.spec.manifests_dir))
            driver.deploy()

            self.events.emit("ready.wait", "waiting for workloads")
            driver.wait_ready()
            self.events.emit("ready.ok", f"workloads ready after {time.time() - started:.1f}s")

            verify_names = [
                s if isinstance(s, str) else next(iter(s)) for s in self.spec.verify
            ]
            if {"backup", "pitr"} & set(verify_names):
                self.events.emit("backup.start", "taking base backup before load")
                driver.ensure_backup()

            if self.spec.load and self.spec.load.phases:
                driver.run_load(self.run_dir)

            for step in self.spec.verify:
                name = step if isinstance(step, str) else next(iter(step))
                config = {} if isinstance(step, str) else (step[name] or {})
                self.events.emit("verify.start", name)
                outcome = driver.verify(name, config)
                result.verifications.append(outcome)
                self.events.emit(
                    "verify.pass" if outcome["passed"] else "verify.fail",
                    f"{name}: {outcome['detail']}",
                )

            if self.spec.faults:
                self.events.emit("faults.skip", "fault injection lands in phase 3")
            if self.spec.goals:
                self.events.emit("goals.skip", "goal evaluation lands in phase 3")

            failed = [v for v in result.verifications if not v["passed"]]
            result.status = "failed" if failed else "passed"
        except Exception as e:
            result.status = "error"
            result.error = str(e)
            self.events.emit("run.error", str(e))
            raise
        finally:
            self._teardown(k8s, result)
            self._write_summary(result, started)
            self.events.close()
        return result

    def _snapshot_spec(self) -> None:
        """Copy the resolved spec into the run dir so results are self-describing."""
        (self.run_dir / "experiment.json").write_text(
            self.spec.model_dump_json(indent=2)
        )

    def _check_capabilities(self, k8s: ClusterClient) -> None:
        caps = probe(self.context)
        needs_nodes = any(f.worker == "node_fail" for f in self.spec.faults)
        if needs_nodes and not caps.multi_node:
            self.events.emit(
                "capability.warn",
                f"experiment uses node_fail but cluster has {caps.worker_count} worker(s); "
                "those faults will be skipped",
            )

    def _teardown(self, k8s: ClusterClient, result: RunResult) -> None:
        if self.keep:
            self.events.emit("teardown.skip", f"--keep: namespace {self.namespace} left running")
            return
        try:
            self.events.emit("teardown.start", f"deleting namespace {self.namespace}")
            k8s.delete_namespace(self.namespace)
            self.events.emit("teardown.ok", "namespace deleted")
        except Exception as e:  # teardown failure must not mask the run error
            self.events.emit("teardown.error", str(e))
            if result.status == "passed":
                result.status = "error"
                result.error = f"teardown failed: {e}"

    def _write_summary(self, result: RunResult, started: float) -> None:
        summary = {
            "run_id": self.run_id,
            "experiment": self.spec.name,
            "technology": self.spec.technology,
            "context": self.context or "(current)",
            "namespace": self.namespace,
            "status": result.status,
            "error": result.error,
            "verifications": result.verifications,
            "duration_s": round(time.time() - started, 1),
            "kept": self.keep,
        }
        (self.run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
