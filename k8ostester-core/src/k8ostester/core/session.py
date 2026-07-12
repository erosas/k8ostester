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
import shutil
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

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
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        self.session_id = f"{stamp}-session"
        self.run_dir = results_root / spec.name / self.session_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events = EventLog(self.run_dir / "events.jsonl", on_event=on_event)
        self.namespace = attach_namespace or f"{spec.namespace_base}-{stamp.replace('-', '')[-6:]}"
        self.error: str | None = None
        self._commands: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._cleanups: list = []
        # the recorder: successful controls, timestamped — exported as a
        # replayable experiment.yaml at teardown
        self._recorded: list[tuple[float, tuple]] = []
        self._ready_at: float | None = None
        self._ready_state: tuple[int, float, int] = (pods, rate, clients)
        # tech ops (backup/PITR) can block for minutes — they run on their own
        # thread so telemetry and controls keep flowing; the lock serializes
        # them (one long-running op at a time)
        self._tech_lock = threading.Lock()
        self._labeled_attached = False

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
            # the concurrent-run guard protects a scored run in progress in
            # both modes; --allow-concurrent overrides it either way
            self._check_no_concurrent_run(k8s)
            if self.attach:
                # attach mode: the cluster under test already exists and is
                # not ours — discover it, never deploy, never delete. Only
                # built-in drivers resolve (no experiment dir), so a stray
                # driver.py in the cwd can never run against a live cluster.
                if self.spec.technology == "auto":
                    detected = detect_technology(k8s, self.namespace)
                    if not detected:
                        raise K8osInfraError(
                            f"no supported technology detected in namespace {self.namespace!r} "
                            "— pass --technology explicitly"
                        )
                    self.spec.technology = detected
                    self.events.emit("session.detect", f"detected {detected} in {self.namespace}")
                driver_cls = get_driver(self.spec.technology)
                driver = driver_cls(k8s, self.spec, self.namespace, self.events)
                # label the attached namespace so a later scored run sees us
                # (reverted at teardown) — the only mutation attach makes
                self._label_attached(k8s)
                self.events.emit(
                    "session.attach",
                    f"attached to existing namespace {self.namespace} ({self.spec.technology}) "
                    "— teardown removes only k8ost artifacts",
                    namespace=self.namespace, context=self.context or "(current)",
                )
                driver.topology()  # fail fast if there is nothing to drive
            else:
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
            self.events.emit(
                "session.ready",
                "controls live — scale the load, inject faults; q tears down",
                actions=self._driver_actions(driver),
            )
            self._ready_at = time.time()
            self._ready_state = (self.pods, self.rate, self.clients)

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
                    self._recorded.append((time.time(), ("pods", self.pods)))
                elif command[0] == "rate":
                    self.rate = max(1.0, self.rate + command[1])
                    driver.set_load_rate(self.rate, self.clients)
                    self.events.emit(
                        "load.rate",
                        f"{self.rate:g} ops/s per pod — pool re-rolling "
                        f"(≈ {self.pods * self.rate:g} ops/s total)",
                        rate=self.rate, pods=self.pods,
                    )
                    self._recorded.append((time.time(), ("rate", self.rate)))
                elif command[0] == "tech":
                    _, action_id, label, params = command
                    self._start_tech_action(driver, action_id, label, params)
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
                    self._recorded.append(
                        (time.time(), ("fault", worker_name, target, duration)))
            except Exception as e:
                # a failed control action is reported, never fatal
                self.events.emit("session.command.error", f"{command[0]}: {e}")

    def _driver_actions(self, driver) -> list[dict]:
        """The driver's session-action metadata (optional decoration, never
        load-bearing) — copied to plain dicts, failures swallowed."""
        try:
            return [dict(a) for a in driver.session_actions()]
        except Exception:
            return []

    def _start_tech_action(self, driver, action_id, label, params) -> None:
        """Run a (possibly minutes-long) tech op off the session loop so
        telemetry and controls stay live; one at a time."""
        if not self._tech_lock.acquire(blocking=False):
            self.events.emit("session.command.error",
                             f"{label}: another tech op is still running")
            return

        def worker() -> None:
            try:
                self.events.emit("session.action", f"{label} — running…")
                summary = driver.run_session_action(action_id, params)
                if action_id == "backup":
                    self._recorded.append((time.time(), ("backup",)))
                self.events.emit("session.action", f"{label}: {summary}")
                # tech ops change what is possible next (a backup opens the
                # restore window) — refresh the action metadata
                self.events.emit("session.actions", "tech ops refreshed",
                                 actions=self._driver_actions(driver))
            except Exception as e:
                self.events.emit("session.command.error", f"{label}: {e}")
            finally:
                self._tech_lock.release()

        threading.Thread(target=worker, daemon=True).start()

    def _label_attached(self, k8s: ClusterClient) -> None:
        try:
            k8s.set_namespace_labels(self.namespace, {RUN_LABEL: self.session_id})
            self._labeled_attached = True
        except Exception as e:
            # discoverability is a nicety, not load-bearing — a cluster that
            # denies the patch still gets driven, just invisibly
            self.events.emit("session.attach", f"could not label namespace for discovery: {e}")

    def recorded_spec(self, end: float) -> dict:
        """The session's control timeline as a declarative experiment: scale
        and rate changes become load phases (total rate = pods × per-pod
        rate), manual faults keep their offsets from session-ready, a backup
        adds the backup verification. Replayable with `k8ost run`."""
        assert self._ready_at is not None
        t0 = self._ready_at
        pods, rate, clients = self._ready_state

        phases: list[dict] = []
        faults: list[dict] = []
        took_backup = False
        segment_start = t0

        def close_segment(until: float) -> None:
            duration = round(until - segment_start)
            if duration < 1:
                return
            if pods == 0 or rate == 0:
                phases.append({"duration": f"{duration}s", "rate": "0/s"})
            else:
                phases.append({
                    "duration": f"{duration}s",
                    "rate": f"{pods * rate:g}/s",
                    "clients": {"count": pods * clients, "mode": "persistent"},
                })

        for at, command in self._recorded:
            if command[0] in ("pods", "rate"):
                close_segment(at)
                segment_start = at
                if command[0] == "pods":
                    pods = command[1]
                else:
                    rate = command[1]
            elif command[0] == "fault":
                _, worker, target, duration = command
                faults.append({
                    "at": f"{round(at - t0)}s", "worker": worker, "target": target,
                    **({"duration": duration} if duration else {}),
                })
            elif command[0] == "backup":
                took_backup = True
        close_segment(end)

        return {
            "name": f"{self.spec.name}-recorded",
            "technology": self.spec.technology,
            **({"group": self.spec.group} if self.spec.group else {}),
            **({"cluster": {"context": self.context}} if self.context else {}),
            **({"infra": self.spec.infra} if self.spec.infra else {}),
            "load": {
                "endpoint": self.spec.load.endpoint if self.spec.load else "auto",
                "phases": phases,
            },
            "faults": faults,
            "verify": ["integrity"] + (["backup"] if took_backup else []),
            "goals": [g.model_dump(exclude_none=True, exclude_defaults=True)
                      for g in self.spec.goals],
        }

    def _write_recording(self) -> None:
        if not self._recorded or self._ready_at is None:
            return
        try:
            recorded_dir = self.run_dir / "recorded"
            recorded_dir.mkdir(exist_ok=True)
            doc = self.recorded_spec(end=time.time())
            # attach sessions have no manifests of their own; a managed session
            # copies its (config.manifests-honoring) manifests dir if present
            has_manifests = not self.attach and self.spec.manifests_dir.is_dir()
            header = (
                f"# Recorded from session {self.session_id} on {self.spec.name}.\n"
                "# Replay with: k8ost run <this directory>\n"
            )
            if not has_manifests:
                header += "# NOTE: recorded from an attached cluster — supply manifests/ before replaying.\n"
            (recorded_dir / "experiment.yaml").write_text(
                header + yaml.safe_dump(doc, sort_keys=False))
            if has_manifests:
                shutil.copytree(self.spec.manifests_dir, recorded_dir / "manifests",
                                dirs_exist_ok=True)
            self.events.emit("session.recorded",
                             f"replayable experiment written: {recorded_dir}")
        except Exception as e:
            self.events.emit("session.record.error", str(e))

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
        self._write_recording()
        for cleanup in self._cleanups:
            try:
                cleanup()
            except Exception as e:
                self.events.emit("teardown.error", f"fault cleanup failed: {e}")
        cleanup_failures: list[str] = []
        if driver is not None:
            try:
                logs = driver.stop_load_session()
                (self.run_dir / "loadgen.log").write_text(logs)
            except Exception:
                pass  # sessions that never got the pool up have nothing to keep
            cleanup_failures = list(getattr(driver, "cleanup_failures", []))
        for failure in cleanup_failures:
            self.events.emit("teardown.error", f"could not remove {failure}")
        if self._labeled_attached:  # revert the discovery label we added
            try:
                k8s.set_namespace_labels(self.namespace, {RUN_LABEL: None})
            except Exception as e:
                self.events.emit("teardown.error", f"could not remove discovery label: {e}")
        if self.attach:
            if cleanup_failures:
                self.events.emit(
                    "teardown.error",
                    f"attached namespace {self.namespace} left untouched, but "
                    f"{len(cleanup_failures)} k8ost artifact(s) could NOT be removed — "
                    f"remove manually: {', '.join(cleanup_failures)}",
                )
            else:
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