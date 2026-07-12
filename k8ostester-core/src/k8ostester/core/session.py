"""Interactive session orchestration (`k8ost session`).

The interactive counterpart to Runner: deploy the experiment's config, start
load as a scalable pod pool, then hand control to the human — scale the load
up and down, inject faults on demand, watch the same live telemetry. There is
no timeline and no verdict; the human is the experiment plan.

Control commands arrive on a thread-safe queue (the UI calls scale()/
inject()/stop() from its own thread) and are executed by the session loop,
which also ticks driver telemetry every few seconds. Teardown mirrors the
runner: fault cleanups, load-pool logs into the session directory, namespace
delete unless --keep.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from k8ostester.core.events import EventLog
from k8ostester.core.exceptions import K8osInfraError
from k8ostester.core.experiment import ExperimentSpec, FaultSpec
from k8ostester.core.k8s import ClusterClient
from k8ostester.core.runner import RUN_LABEL
from k8ostester.drivers import detect_technology, get_driver
from k8ostester.workers import get_worker

TELEMETRY_INTERVAL_S = 3


class Session:
    def __init__(
        self,
        spec: ExperimentSpec,
        results_root: Path = Path("results"),
        keep: bool = False,
        context_override: str | None = None,
        on_event=None,
        allow_concurrent: bool = False,
        pods: int = 1,
        rate: float = 20.0,
        clients: int = 5,
        attach_namespace: str | None = None,
    ):
        self.spec = spec
        self.keep = keep
        self.allow_concurrent = allow_concurrent
        self.context = context_override or spec.cluster.context
        self.pods = pods
        self.rate = rate
        self.clients = clients
        self.attach = attach_namespace is not None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.session_id = f"{stamp}-session"
        self.run_dir = results_root / spec.name / self.session_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events = EventLog(self.run_dir / "events.jsonl", on_event=on_event)
        self.namespace = attach_namespace or f"{spec.namespace_base}-{stamp.replace('-', '')[-6:]}"
        self.error: str | None = None
        self._commands: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._cleanups: list = []

    # -- controls (thread-safe, called from the UI) -----------------------------

    def scale(self, delta: int) -> None:
        self._commands.put(("scale", delta))

    def set_rate(self, delta: float) -> None:
        self._commands.put(("rate", delta))

    def inject(self, worker: str, target: dict, duration: str | None = None) -> None:
        self._commands.put(("fault", worker, target, duration))

    def run_action(self, action_id: str, label: str, params: dict | None = None) -> None:
        self._commands.put(("tech", action_id, label, params))

    def stop(self) -> None:
        self._stop.set()

    # -- the session loop (runs on a worker thread) ------------------------------

    def start(self) -> None:
        """Blocks until stop(): setup → command/telemetry loop → teardown."""
        k8s = ClusterClient(self.context)
        driver = None
        started = time.time()
        try:
            if self.attach:
                # attach mode: the cluster under test already exists and is
                # not ours — discover it, never deploy, never delete
                if self.spec.technology == "auto":
                    detected = detect_technology(k8s, self.namespace)
                    if not detected:
                        raise K8osInfraError(
                            f"no supported technology detected in namespace {self.namespace!r} "
                            "— pass --technology explicitly"
                        )
                    self.spec.technology = detected
                    self.events.emit("session.detect", f"detected {detected} in {self.namespace}")
                driver_cls = get_driver(self.spec.technology, self.spec.dir)
                driver = driver_cls(k8s, self.spec, self.namespace, self.events)
                self.events.emit(
                    "session.attach",
                    f"attached to existing namespace {self.namespace} ({self.spec.technology}) "
                    "— teardown removes only k8ost artifacts",
                    namespace=self.namespace, context=self.context or "(current)",
                )
                driver.topology()  # fail fast if there is nothing to drive
            else:
                self._check_no_concurrent_run(k8s)
                driver_cls = get_driver(self.spec.technology, self.spec.dir)
                driver = driver_cls(k8s, self.spec, self.namespace, self.events)

                self.events.emit("session.start", f"interactive session on {self.spec.name}",
                                 namespace=self.namespace, context=self.context or "(current)")
                self.events.emit("prereqs.install", "installing prerequisites")
                driver.install_prereqs()
                self.events.emit("namespace.create", self.namespace)
                k8s.create_namespace(self.namespace, labels={RUN_LABEL: self.session_id})
                self.events.emit("deploy.start", str(self.spec.manifests_dir))
                driver.deploy()
                self.events.emit("ready.wait", "waiting for workloads")
                driver.wait_ready()

            driver.start_load_session(self.run_dir, self.rate, self.clients, self.pods)
            try:  # action metadata is optional decoration, never load-bearing
                actions = [dict(a) for a in driver.session_actions()]
            except Exception:
                actions = []
            self.events.emit(
                "session.ready",
                "controls live — scale the load, inject faults; q tears down",
                actions=actions,
            )

            while True:
                self._drain_commands(k8s, driver)
                driver.emit_live_telemetry()
                if self._stop.wait(TELEMETRY_INTERVAL_S):
                    break
        except Exception as e:
            self.error = str(e)
            self.events.emit("session.error", str(e))
            raise
        finally:
            self._teardown(k8s, driver, started)

    def _drain_commands(self, k8s: ClusterClient, driver) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                if command[0] == "scale":
                    self.pods = max(0, self.pods + command[1])
                    driver.scale_load(self.pods)
                    self.events.emit(
                        "load.scale",
                        f"{self.pods} load pod(s) ≈ {self.pods * self.rate:g} ops/s",
                        pods=self.pods,
                    )
                elif command[0] == "rate":
                    self.rate = max(1.0, self.rate + command[1])
                    driver.set_load_rate(self.rate, self.clients)
                    self.events.emit(
                        "load.rate",
                        f"{self.rate:g} ops/s per pod — pool re-rolling "
                        f"(≈ {self.pods * self.rate:g} ops/s total)",
                        rate=self.rate, pods=self.pods,
                    )
                elif command[0] == "tech":
                    _, action_id, label, params = command
                    self.events.emit("session.action", f"{label} — running…")
                    summary = driver.run_session_action(action_id, params)
                    self.events.emit("session.action", f"{label}: {summary}")
                elif command[0] == "fault":
                    _, worker_name, target, duration = command
                    worker = get_worker(worker_name)(k8s, driver, self.namespace, self.events)
                    cleanup = worker.execute(
                        FaultSpec(at="0s", worker=worker_name, target=target, duration=duration)
                    )
                    if cleanup:
                        self._cleanups.append(cleanup)
                    self.events.emit("fault.injected", f"{worker_name} (manual)",
                                     worker=worker_name, target=target)
            except Exception as e:
                # a failed control action is reported, never fatal
                self.events.emit("session.command.error", f"{command[0]}: {e}")

    def _check_no_concurrent_run(self, k8s: ClusterClient) -> None:
        others = [
            ns.metadata.name
            for ns in k8s.core.list_namespace(label_selector=RUN_LABEL).items
        ]
        if others and not self.allow_concurrent:
            raise K8osInfraError(
                f"another experiment already occupies this cluster: {', '.join(others)} "
                "— wait for it, or pass --allow-concurrent"
            )

    def _teardown(self, k8s: ClusterClient, driver, started: float) -> None:
        for cleanup in self._cleanups:
            try:
                cleanup()
            except Exception as e:
                self.events.emit("teardown.error", f"fault cleanup failed: {e}")
        if driver is not None:
            try:
                logs = driver.stop_load_session()
                (self.run_dir / "loadgen.log").write_text(logs)
            except Exception:
                pass  # sessions that never got the pool up have nothing to keep
        if self.attach:
            self.events.emit(
                "teardown.skip",
                f"attached namespace {self.namespace} left untouched (k8ost artifacts removed)",
            )
        elif self.keep:
            self.events.emit("teardown.skip", f"--keep: namespace {self.namespace} left running")
        else:
            try:
                self.events.emit("teardown.start", f"deleting namespace {self.namespace}")
                k8s.delete_namespace(self.namespace)
                self.events.emit("teardown.ok", "namespace deleted")
            except Exception as e:
                self.events.emit("teardown.error", str(e))
        (self.run_dir / "summary.json").write_text(json.dumps({
            "run_id": self.session_id,
            "experiment": self.spec.name,
            "group": self.spec.group,
            "technology": self.spec.technology,
            "context": self.context or "(current)",
            "namespace": self.namespace,
            "status": "error" if self.error else "session",
            "error": self.error,
            "verifications": [],
            "goals": [],
            "duration_s": round(time.time() - started, 1),
            "kept": self.keep,
        }, indent=2))
        self.events.close()