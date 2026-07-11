"""CloudNativePG driver.

Owns everything Postgres-specific: operator/object-store prerequisites, the
Cluster CR under test, readiness and topology from the CR status, the
in-cluster load generator Job, and the three verifications — integrity
(journal vs database), backup (Barman base backup completed), and PITR
(restore a second cluster to the mid-run pause and compare row sets exactly).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from k8ostester.core.exceptions import K8osConfigError
from k8ostester.core.experiment import parse_rate
from k8ostester.core.goals import _LATENCY_RE, evaluate_goals
from k8ostester.core.helm import Helm, HelmError
from k8ostester.core.infra import InfraManager
from k8ostester.core.k8s import wait_until
from k8ostester.core.resources import load_resource
from k8ostester.drivers.base import TechnologyDriver

CNPG_GROUP = "postgresql.cnpg.io"
CNPG_VERSION = "v1"
# this technology's own prerequisite pins (D15) — core only pins common infra
CNPG_CHART_VERSION = "0.28.3"  # CloudNativePG operator 1.29.1
CNPG_REPO = "https://cloudnative-pg.github.io/charts"
LOADGEN_SCRIPT = Path(__file__).parent / "loadgen.py"
RESOURCES = Path(__file__).parent / "resources"
LOADGEN_IMAGE = "python:3.12-slim"
PSYCOPG_PIN = "psycopg[binary]==3.2.*"


class VerifyResult(dict):
    @classmethod
    def make(cls, check: str, passed: bool, detail: str) -> "VerifyResult":
        return cls(check=check, passed=passed, detail=detail)


class CnpgDriver(TechnologyDriver):
    _journal: list[dict]  # acked-write records from the loadgen run
    _records: list[dict]  # every op record
    _backup_name: str | None = None
    _last_topology: dict | None = None  # last topology emitted as telemetry

    # -- lifecycle -----------------------------------------------------------

    def install_prereqs(self) -> None:
        """Tech-owned prerequisites here; common entries delegate to core.
        Omitting `- operator: cnpg` from infra means "the cluster already has
        the operator, don't touch it" (e.g. a private cluster where ops owns
        the install) — but the CRD must actually be there."""
        common = InfraManager(self.k8s, self.events)
        common.ensure([e for e in self.spec.infra if common.handles(e)])
        declares_operator = False
        for entry in self.spec.infra:
            if common.handles(entry):
                continue
            if isinstance(entry, dict) and entry.get("operator") == "cnpg":
                declares_operator = True
                self._ensure_operator()
            else:
                raise ValueError(f"unknown infra entry for postgres-cnpg: {entry!r}")
        if not declares_operator and not self.k8s.has_crd("clusters.postgresql.cnpg.io"):
            raise RuntimeError(
                "CloudNativePG is not installed on this cluster and the experiment "
                "doesn't declare it — add '- operator: cnpg' to infra, or install "
                "the operator out-of-band"
            )

    def _ensure_operator(self) -> None:
        helm = Helm(self.k8s.context)
        self.events.emit(
            "infra.cnpg", f"ensuring CloudNativePG operator (chart {CNPG_CHART_VERSION})"
        )
        try:
            helm.repo_add("cnpg", CNPG_REPO)
            helm.upgrade_install(
                "cnpg", "cnpg/cloudnative-pg", "cnpg-system", version=CNPG_CHART_VERSION
            )
        except HelmError:
            # tolerate chart-repo blips when the operator is already installed
            if not helm.release_exists("cnpg", "cnpg-system"):
                raise
            self.events.emit("infra.cnpg", "chart repo unreachable — using the installed release")

    def _resolve_resource(self, name: str) -> Path:
        """Experiment manifests/ dir takes precedence over packaged templates."""
        override = self.spec.manifests_dir / name
        if override.exists():
            return override
        return RESOURCES / name

    def deploy(self) -> None:
        out = self.k8s.apply_manifests(
            self.spec.manifests_dir,
            self.namespace,
            variables={"K8OST_NAMESPACE": self.namespace},
        )
        for line in out.splitlines():
            self.events.emit("manifest.applied", line)

    def wait_ready(self, timeout: float = 600) -> None:
        self._wait_cluster_healthy(self.cluster_name, timeout)
        # anything else in the manifests (e.g. a PgBouncer Pooler deployment)
        self.k8s.wait_workloads_ready(self.namespace, timeout=300)

    # -- cluster introspection -------------------------------------------------

    @property
    def cluster_name(self) -> str:
        clusters = self._clusters()["items"]
        if len(clusters) != 1:
            raise RuntimeError(
                f"expected exactly 1 CNPG Cluster in {self.namespace}, found {len(clusters)}"
            )
        return clusters[0]["metadata"]["name"]

    def _clusters(self) -> dict:
        return self.k8s.custom.list_namespaced_custom_object(
            CNPG_GROUP, CNPG_VERSION, self.namespace, "clusters"
        )

    def _cluster(self, name: str) -> dict:
        return self.k8s.custom.get_namespaced_custom_object(
            CNPG_GROUP, CNPG_VERSION, self.namespace, "clusters", name
        )

    def _wait_cluster_healthy(self, name: str, timeout: float) -> None:
        last = ""

        def healthy() -> bool:
            nonlocal last
            status = self._cluster(name).get("status", {})
            phase = status.get("phase", "(no status)")
            ready = status.get("readyInstances", 0)
            instances = status.get("instances", "?")
            if phase != last:
                self.events.emit("cnpg.phase", f"{name}: {phase} ({ready}/{instances} ready)")
                last = phase
            return phase == "Cluster in healthy state" and ready == instances

        wait_until(
            healthy, timeout, interval=3,
            desc=lambda: f"cluster {name} not healthy (last: {last})",
        )

    def topology(self) -> dict[str, Any]:
        status = self._cluster(self.cluster_name).get("status", {})
        primary = status.get("currentPrimary")
        instances = status.get("instanceNames", [])
        return {
            "primary": primary,
            "replicas": [i for i in instances if i != primary],
        }

    def _psql(self, sql: str, db: str = "app", pod: str | None = None) -> str:
        pod = pod or self.topology()["primary"]
        return self.k8s.exec_pod(
            self.namespace, pod, ["psql", "-d", db, "-qtAc", sql], container="postgres"
        )

    # -- load ------------------------------------------------------------------

    def run_load(self, run_dir: Path) -> None:
        self.start_load(run_dir)
        self.wait_load_started()
        self.wait_load_done()

    def start_load(self, run_dir: Path) -> None:
        """The pgbench runner has no acked-write journal and its clients abort
        on connection loss — reject experiments that need either, up front."""
        if self.spec.load and self.spec.load.runner == "pgbench":
            if self.spec.faults:
                raise K8osConfigError(
                    "runner 'pgbench' cannot run fault timelines (pgbench clients abort "
                    "on connection loss) — use the journal runner for fault experiments"
                )
            if len(self.spec.load.phases) != 1:
                raise K8osConfigError("runner 'pgbench' takes exactly one load phase")
            if self.spec.load.workers != 1:
                raise K8osConfigError(
                    "runner 'pgbench' supports workers: 1 (one pod drives thousands of clients)"
                )
            verify_names = {
                v if isinstance(v, str) else next(iter(v)) for v in self.spec.verify
            }
            if bad := verify_names & {"integrity", "pitr"}:
                raise K8osConfigError(
                    f"runner 'pgbench' has no acked-write journal — cannot verify: {sorted(bad)}"
                )
            for g in self.spec.goals:
                metric_ok = (
                    g.metric is None
                    or g.metric == "tps"
                    or g.metric.startswith("write_latency_")
                )
                check_ok = g.check in (None, "backup")
                if not (metric_ok and check_ok):
                    raise K8osConfigError(
                        f"goal {g.metric or g.check!r} needs the journal runner — "
                        "pgbench supports tps, write_latency_*, and the backup check"
                    )

        spec = self.spec.load
        assert spec is not None
        if spec.runner == "pgbench":
            self._start_pgbench(run_dir)
            return
        phases = []
        for p in spec.phases:
            clients = p.clients or spec.clients
            phases.append(
                {
                    "duration_s": p.duration_s,
                    "rate": parse_rate(p.rate),
                    "mix": p.mix or {"read": 0.5, "write": 0.5},
                    "clients": clients.count,
                    "mode": clients.mode,
                }
            )
        total_s = sum(p["duration_s"] for p in phases)

        secret = self.k8s.core.read_namespaced_secret(
            f"{self.cluster_name}-app", self.namespace
        )
        import base64

        decode = lambda k: base64.b64decode(secret.data[k]).decode()
        host = (
            f"{self.cluster_name}-rw"
            if spec.endpoint == "auto"
            else spec.endpoint
        )
        dsn = (
            f"host={host} port=5432 dbname={decode('dbname')} "
            f"user={decode('username')} password={decode('password')}"
        )

        self.k8s.core.create_namespaced_config_map(
            self.namespace,
            {
                "metadata": {"name": "k8ost-loadgen"},
                "data": {"loadgen.py": LOADGEN_SCRIPT.read_text()},
            },
        )
        job = load_resource(
            self._resolve_resource("loadgen-job.yaml"),
            {
                "IMAGE": spec.image or LOADGEN_IMAGE,
                "PSYCOPG_PIN": PSYCOPG_PIN,
                "DSN": dsn,
                "PHASES_JSON": json.dumps(phases),
                "WORKERS": str(spec.workers),
                "PULL_SECRETS": json.dumps(
                    [{"name": spec.pull_secret}] if spec.pull_secret else []
                ),
            },
        )
        self.k8s.batch.create_namespaced_job(self.namespace, job)
        self._load_total_s = total_s
        self._workers = spec.workers
        self._run_dir = run_dir
        self.events.emit(
            "load.start",
            f"{len(phases)} phase(s), ~{total_s:.0f}s"
            + (f", {spec.workers} worker pods" if spec.workers > 1 else ""),
            total_s=total_s,
        )

    def _start_pgbench(self, run_dir: Path) -> None:
        """The pgbench runner (spec-validated: one phase, no faults, journal-free
        goals only). Same image as the database pods — pgbench ships in it."""
        spec = self.spec.load
        assert spec is not None
        phase = spec.phases[0]
        rate = parse_rate(phase.rate)
        clients = (phase.clients or spec.clients).count

        secret = self.k8s.core.read_namespaced_secret(
            f"{self.cluster_name}-app", self.namespace
        )
        import base64

        decode = lambda k: base64.b64decode(secret.data[k]).decode()
        host = f"{self.cluster_name}-rw" if spec.endpoint == "auto" else spec.endpoint
        dsn = (
            f"host={host} port=5432 dbname={decode('dbname')} "
            f"user={decode('username')} password={decode('password')}"
        )
        cluster = self._cluster(self.cluster_name)
        # same image the database pods run (pgbench ships in it, already pulled)
        image = cluster["spec"].get("imageName") or cluster.get("status", {}).get("image")
        if not image:
            raise RuntimeError("cannot determine cluster image for the pgbench runner")

        job = load_resource(
            self._resolve_resource("pgbench-job.yaml"),
            {
                "IMAGE": image,
                "DSN": dsn,
                "SCALE": str(spec.params.get("scale", 10)),
                "CLIENTS": str(clients),
                "JOBS": str(min(clients, 8)),
                "DURATION": str(int(phase.duration_s)),
                "RATE": str(int(rate)) if rate else "",
                "PULL_SECRETS": json.dumps(
                    [{"name": spec.pull_secret}] if spec.pull_secret else []
                ),
            },
        )
        self.k8s.batch.create_namespaced_job(self.namespace, job)
        self._load_total_s = phase.duration_s
        self._workers = 1
        self._run_dir = run_dir
        self.events.emit(
            "load.start",
            f"pgbench: scale {spec.params.get('scale', 10)}, {clients} clients, "
            f"{int(phase.duration_s)}s" + (f", {int(rate)}/s" if rate else " (unthrottled)"),
            total_s=phase.duration_s,
        )

    def wait_load_started(self, timeout: float = 300) -> float:
        """Block until the loadgen emits its 'start' record (schema created,
        ops flowing). Returns the framework-clock timestamp — the zero point
        for the fault timeline."""
        def started() -> bool:
            status = self.k8s.batch.read_namespaced_job("k8ost-loadgen", self.namespace).status
            if status.failed:
                raise RuntimeError("loadgen job failed: " + self._loadgen_logs()[-2000:])
            if not (status.active or status.succeeded):
                return False
            try:
                # every worker pod must be flowing before the fault clock starts
                return self._loadgen_logs().count('"kind": "start"') >= self._workers
            except Exception:
                return False  # pod still ContainerCreating / not listed yet

        wait_until(started, timeout, interval=3, desc="loadgen did not start emitting ops")
        self.events.emit("load.started", "loadgen is emitting ops")
        return time.time()

    def emit_live_telemetry(self) -> None:
        """Live telemetry for the run view, piggybacked on whatever loop is
        currently waiting. Best-effort by contract: it must never fail a run."""
        try:
            sample = self._live_sample(self._loadgen_logs())
            if sample:
                self.events.emit(
                    "load.sample",
                    f"{sample['ops_s']} ops/s, {sample['failed']} failed",
                    **sample,
                )
            topology = self.topology()
            if topology != self._last_topology:
                self._last_topology = topology
                self.events.emit(
                    "topology", f"primary {topology['primary']}", **topology
                )
        except Exception:
            pass

    def wait_load_done(self) -> None:
        def finished() -> bool:
            status = self.k8s.batch.read_namespaced_job("k8ost-loadgen", self.namespace).status
            if status.failed:
                raise RuntimeError("loadgen job failed: " + self._loadgen_logs()[-2000:])
            done = (status.succeeded or 0) >= self._workers
            if not done:
                self.emit_live_telemetry()
            return done

        # pull + pip headroom on top of the phase plan
        wait_until(finished, self._load_total_s + 300, interval=5,
                   desc="loadgen job did not finish")

        logs = self._loadgen_logs()
        (self._run_dir / "loadgen.log").write_text(logs)  # raw dump survives any parse failure
        if self.spec.load and self.spec.load.runner == "pgbench":
            self._parse_pgbench_output(logs, self._run_dir)
        else:
            self._parse_loadgen_output(logs, self._run_dir)

    @property
    def op_records(self) -> list[dict]:
        return [r for r in self._records if r.get("kind") == "op"]

    # goal metrics computable mid-run — everything except the fault-anchored
    # (rto) and reconciliation-based (rpo, checks) ones
    LIVE_GOAL_METRICS = {
        "uptime", "downtime_total", "availability",
        "error_rate", "connect_error_rate", "tps",
    }
    LIVE_WINDOW_S = 10  # instantaneous ops/s and err/s window

    def _live_sample(self, logs: str) -> dict | None:
        """Aggregate the journal-so-far into one progress sample: instantaneous
        ops/s + err/s, cumulative totals, and live values for every goal the
        final evaluator can already score without fault/reconciliation data."""
        ops = []
        for line in logs.splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # pip noise etc.
            if rec.get("kind") == "op":
                ops.append(rec)
        if not ops:
            return None

        latest = max(r["t"] for r in ops)
        window = [r for r in ops if r["t"] >= latest - self.LIVE_WINDOW_S]
        live_goals = [
            g for g in self.spec.goals
            if g.metric and (g.metric in self.LIVE_GOAL_METRICS or _LATENCY_RE.match(g.metric))
        ]
        return {
            "ops_s": round(len(window) / self.LIVE_WINDOW_S, 1),
            "err_s": round(sum(not r["ok"] for r in window) / self.LIVE_WINDOW_S, 1),
            "total_ops": len(ops),
            "failed": sum(not r["ok"] for r in ops),
            "acked_writes": sum(1 for r in ops if r["op"] == "write" and r["ok"]),
            "goals": evaluate_goals(live_goals, ops, [], []) if live_goals else [],
        }

    def _loadgen_logs(self) -> str:
        """Concatenated logs of every worker pod; records interleave safely
        because each carries its own timestamp."""
        pods = sorted(
            self.k8s.core.list_namespaced_pod(
                self.namespace, label_selector="app=k8ost-loadgen"
            ).items,
            key=lambda p: p.metadata.name,
        )
        chunks = []
        for pod in pods:
            try:
                chunks.append(self.k8s.pod_logs(self.namespace, pod.metadata.name))
            except Exception:
                continue  # pod still ContainerCreating; caller retries
        return "\n".join(chunks)

    def _parse_loadgen_output(self, logs: str, run_dir: Path) -> None:
        self._records, self._journal = [], []
        with open(run_dir / "metrics.jsonl", "w") as metrics_file:
            for line in logs.splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # pip noise etc.
                self._records.append(rec)
                metrics_file.write(line + "\n")
                if rec.get("kind") == "op" and rec.get("op") == "write" and rec.get("ok"):
                    self._journal.append(rec)
        with open(run_dir / "journal.jsonl", "w") as journal_file:
            journal_file.writelines(json.dumps(r) + "\n" for r in self._journal)
        ops = [r for r in self._records if r.get("kind") == "op"]
        failed = sum(1 for r in ops if not r["ok"])
        self.events.emit(
            "load.done",
            f"{len(ops)} ops ({len(self._journal)} acked writes, {failed} failed)",
        )
        if not self._journal:
            raise RuntimeError("loadgen produced no acked writes — check its logs")

    def _parse_pgbench_output(self, logs: str, run_dir: Path) -> None:
        """Per-transaction log lines (between the Job's markers) into op
        records: `client txn_no latency_us script_no epoch_s epoch_us [lag]`.
        Every logged transaction is a completed TPC-B write; there is no
        journal (spec validation already excluded journal-dependent goals)."""
        self._records, self._journal = [], []
        in_log = False
        with open(run_dir / "metrics.jsonl", "w") as metrics_file:
            for line in logs.splitlines():
                if line.startswith("K8OST_TXNLOG_BEGIN"):
                    in_log = True
                    continue
                if line.startswith("K8OST_TXNLOG_END"):
                    in_log = False
                    continue
                if not in_log:
                    continue
                parts = line.split()
                if len(parts) < 6:
                    continue
                lat_ms = int(parts[2]) / 1000.0
                completed = int(parts[4]) + int(parts[5]) / 1e6
                rec = {
                    "kind": "op", "op": "write", "phase": 0,
                    "t": round(completed - lat_ms / 1000.0, 6),
                    "lat_ms": round(lat_ms, 3), "ok": True,
                }
                self._records.append(rec)
                metrics_file.write(json.dumps(rec) + "\n")
        (run_dir / "journal.jsonl").write_text("")  # no acked-write journal by design
        if not self._records:
            raise RuntimeError("pgbench produced no transaction log — check loadgen.log")
        span = max(r["t"] for r in self._records) - min(r["t"] for r in self._records)
        self.events.emit(
            "load.done",
            f"{len(self._records)} pgbench transactions"
            + (f" (~{len(self._records) / span:.0f} tps)" if span else ""),
        )

    # -- verification ----------------------------------------------------------

    def verify(self, check: str, config: dict) -> VerifyResult:
        if check == "integrity":
            return self.verify_integrity()
        if check == "backup":
            return self.verify_backup()
        if check == "pitr":
            return self.verify_pitr()
        raise ValueError(f"unknown verify step {check!r}")

    def verify_integrity(self) -> VerifyResult:
        db_rows = {
            int(id_): checksum
            for id_, checksum in (
                line.split("|")
                for line in self._psql(
                    "select id, checksum from k8ost_ops order by id"
                ).splitlines()
                if line
            )
        }
        missing = [r["id"] for r in self._journal if r["id"] not in db_rows]
        corrupt = [
            r["id"]
            for r in self._journal
            if r["id"] in db_rows and db_rows[r["id"]] != r["checksum"]
        ]
        passed = not missing and not corrupt
        detail = (
            f"{len(self._journal)} acked writes all present with matching checksums"
            if passed
            else f"{len(missing)} acked writes LOST, {len(corrupt)} corrupted"
        )
        result = VerifyResult.make("integrity", passed, detail)
        result["missing"] = len(missing)  # rpo goal reads this (lost acked writes)
        result["corrupt"] = len(corrupt)
        return result

    def ensure_backup(self) -> None:
        """Take a Barman base backup now (called before load so PITR can
        replay WAL forward through the whole run)."""
        self._backup_name = f"k8ost-{datetime.now(timezone.utc).strftime('%H%M%S')}"
        self.k8s.custom.create_namespaced_custom_object(
            CNPG_GROUP, CNPG_VERSION, self.namespace, "backups",
            load_resource(
                self._resolve_resource("backup.yaml"),
                {"BACKUP_NAME": self._backup_name, "CLUSTER_NAME": self.cluster_name},
            ),
        )
        def completed() -> bool:
            backup = self.k8s.custom.get_namespaced_custom_object(
                CNPG_GROUP, CNPG_VERSION, self.namespace, "backups", self._backup_name
            )
            phase = backup.get("status", {}).get("phase")
            if phase == "failed":
                raise RuntimeError(
                    f"backup failed: {backup['status'].get('error', '(no error detail)')}"
                )
            return phase == "completed"

        wait_until(completed, 600, interval=5, desc="backup not completed")
        self.events.emit("backup.ok", f"base backup {self._backup_name} completed")

    def verify_backup(self) -> VerifyResult:
        if not self._backup_name:
            return VerifyResult.make("backup", False, "no backup was taken")
        status = self.k8s.custom.get_namespaced_custom_object(
            CNPG_GROUP, CNPG_VERSION, self.namespace, "backups", self._backup_name
        ).get("status", {})
        passed = status.get("phase") == "completed"
        return VerifyResult.make(
            "backup",
            passed,
            f"{self._backup_name}: phase={status.get('phase')}, "
            f"beginLSN={status.get('beginLSN')}, endLSN={status.get('endLSN')}",
        )

    def verify_pitr(self) -> VerifyResult:
        """Restore a second cluster to the mid-run pause phase and compare row
        sets exactly: every pre-pause acked write present, nothing after."""
        target_ts, expected_ids = self._pitr_target()
        target = datetime.fromtimestamp(target_ts, timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S.%f+00"
        )
        self.events.emit("pitr.start", f"restore target: {target} ({len(expected_ids)} rows expected)")

        # make sure WAL covering the target is archived before restoring
        self._psql("select pg_switch_wal()", db="postgres")
        self._psql("checkpoint", db="postgres")
        time.sleep(15)

        source = self._cluster(self.cluster_name)
        store = source["spec"]["backup"]["barmanObjectStore"]
        restore_name = f"{self.cluster_name}-pitr"
        self.k8s.custom.create_namespaced_custom_object(
            CNPG_GROUP, CNPG_VERSION, self.namespace, "clusters",
            load_resource(
                self._resolve_resource("pitr-cluster.yaml"),
                {
                    "RESTORE_NAME": restore_name,
                    "TARGET_TIME": target,
                    "STORAGE_JSON": json.dumps(source["spec"]["storage"]),
                    "STORE_JSON": json.dumps({**store, "serverName": self.cluster_name}),
                },
            ),
        )
        self._wait_cluster_healthy(restore_name, timeout=600)

        restored = {
            int(line)
            for line in self._psql(
                "select id from k8ost_ops", pod=f"{restore_name}-1"
            ).splitlines()
            if line
        }
        expected = set(expected_ids)
        missing, extra = expected - restored, restored - expected
        passed = not missing and not extra
        detail = (
            f"restored exactly the {len(expected)} pre-pause rows"
            if passed
            else f"{len(missing)} expected rows missing, {len(extra)} post-target rows present"
        )
        return VerifyResult.make("pitr", passed, detail)

    def _pitr_target(self) -> tuple[float, list[int]]:
        """Target = middle of the first pause (zero-rate) phase; expected rows =
        acked writes from before it."""
        assert self.spec.load is not None
        pause_idx = next(
            (
                i
                for i, p in enumerate(self.spec.load.phases)
                if parse_rate(p.rate) == 0
            ),
            None,
        )
        if pause_idx is None:
            raise RuntimeError("pitr verification needs a zero-rate pause phase in the load plan")
        pause = self.spec.load.phases[pause_idx]
        before = [r for r in self._journal if r["phase"] < pause_idx]
        if not before:
            raise RuntimeError("no acked writes before the pause phase")
        last_write_ts = max(r["t"] for r in before)
        return last_write_ts + pause.duration_s / 2, [r["id"] for r in before]


DRIVER = CnpgDriver
