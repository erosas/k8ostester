"""Experiment spec: pydantic models for experiment.yaml plus the loader.

An experiment directory contains experiment.yaml and (usually) a manifests/
directory with the config under test. The spec is fully validated up front so
a bad experiment fails before anything touches the cluster.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h)$")
_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_rate(value: str | int | float | None) -> float:
    """'50/s' / bare number → ops per second; None/0 → 0 (pause)."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return float(value.strip().removesuffix("/s"))


def parse_duration(value: str | int | float) -> float:
    """'30s' / '2m' / '150ms' / bare seconds → seconds as float."""
    if isinstance(value, (int, float)):
        return float(value)
    m = _DURATION_RE.match(value.strip())
    if not m:
        raise ValueError(f"invalid duration {value!r} (expected e.g. '30s', '2m')")
    return float(m.group(1)) * _DURATION_UNITS[m.group(2)]


class ClusterSpec(BaseModel):
    context: str | None = None  # kubeconfig context; None = current
    namespace: str | None = None  # base name; runner appends a run suffix


class ConfigSpec(BaseModel):
    manifests: Path = Path("manifests")


class ClientsSpec(BaseModel):
    count: int = 10
    mode: Literal["persistent", "churn"] = "persistent"


class LoadPhase(BaseModel):
    duration: str
    rate: str | None = None  # e.g. "50/s"; None = unthrottled
    mix: dict[str, float] | None = None  # e.g. {read: 0.7, write: 0.3}
    clients: ClientsSpec | None = None  # override for this phase

    @property
    def duration_s(self) -> float:
        return parse_duration(self.duration)

    @field_validator("duration")
    @classmethod
    def _valid_duration(cls, v: str) -> str:
        parse_duration(v)
        return v


class LoadSpec(BaseModel):
    endpoint: str = "auto"  # Service to hit; "auto" = driver default
    runner: Literal["journal", "pgbench"] = "journal"  # journal = built-in loadgen (full goal set)
    params: dict[str, Any] = {}  # runner-specific knobs, e.g. pgbench {scale: 20}
    workers: int = 1  # loadgen pods (Indexed Job); clients + rate shard across them
    clients: ClientsSpec = ClientsSpec()
    phases: list[LoadPhase] = []


class FaultSpec(BaseModel):
    at: str  # offset from load start, e.g. "3m"
    worker: str  # worker name, e.g. "pod_kill"
    target: dict[str, Any] = {}
    duration: str | None = None  # how long the fault holds (network_* workers); instant faults omit it
    params: dict[str, Any] = {}  # worker-specific knobs, e.g. {loss: "50"} or {latency: "100ms"}

    @property
    def at_s(self) -> float:
        return parse_duration(self.at)

    @field_validator("at")
    @classmethod
    def _valid_at(cls, v: str) -> str:
        parse_duration(v)
        return v

    @field_validator("duration")
    @classmethod
    def _valid_duration(cls, v: str | None) -> str | None:
        if v is not None:
            parse_duration(v)
        return v


class GoalSpec(BaseModel):
    metric: str | None = None  # metric goal: rto, rpo, availability, *_latency_p99…
    check: str | None = None  # procedural goal: pitr, backup, integrity
    max: str | float | None = None
    min: str | float | None = None
    window: str = "whole-run"
    must: Literal["pass"] | None = None

    @field_validator("check")
    @classmethod
    def _metric_or_check(cls, v: str | None, info) -> str | None:
        if v is None and info.data.get("metric") is None:
            raise ValueError("goal needs either 'metric' or 'check'")
        return v


class ExperimentSpec(BaseModel):
    name: str
    technology: str
    group: str | None = None  # runs sharing a group are reported/graphed together
    cluster: ClusterSpec = ClusterSpec()
    infra: list[str | dict[str, Any]] = []
    config: ConfigSpec = ConfigSpec()
    load: LoadSpec | None = None
    faults: list[FaultSpec] = []
    verify: list[str | dict[str, Any]] = []
    goals: list[GoalSpec] = []

    # populated by the loader; excluded from (de)serialization of the yaml itself
    dir: Path = Field(default=Path("."), exclude=True)

    @model_validator(mode="after")
    def _runner_supports_goals(self) -> "ExperimentSpec":
        """The pgbench runner has no acked-write journal and its clients abort
        on connection loss — reject experiments that need either, up front."""
        if not self.load or self.load.runner != "pgbench":
            return self
        if self.faults:
            raise ValueError(
                "runner 'pgbench' cannot run fault timelines (pgbench clients abort "
                "on connection loss) — use the journal runner for fault experiments"
            )
        if len(self.load.phases) != 1:
            raise ValueError("runner 'pgbench' takes exactly one load phase")
        if self.load.workers != 1:
            raise ValueError("runner 'pgbench' supports workers: 1 (one pod drives thousands of clients)")
        verify_names = {v if isinstance(v, str) else next(iter(v)) for v in self.verify}
        if bad := verify_names & {"integrity", "pitr"}:
            raise ValueError(f"runner 'pgbench' has no acked-write journal — cannot verify: {sorted(bad)}")
        for g in self.goals:
            metric_ok = g.metric is None or g.metric == "tps" or g.metric.startswith("write_latency_")
            check_ok = g.check in (None, "backup")
            if not (metric_ok and check_ok):
                raise ValueError(
                    f"goal {g.metric or g.check!r} needs the journal runner — "
                    "pgbench supports tps, write_latency_*, and the backup check"
                )
        return self

    @property
    def manifests_dir(self) -> Path:
        return (self.dir / self.config.manifests).resolve()

    @property
    def namespace_base(self) -> str:
        return self.cluster.namespace or f"exp-{self.name}"


def load_experiment(path: Path) -> ExperimentSpec:
    """Load and validate an experiment directory (or a direct experiment.yaml path)."""
    path = path.resolve()
    yaml_path = path / "experiment.yaml" if path.is_dir() else path
    if not yaml_path.exists():
        raise FileNotFoundError(f"no experiment.yaml at {yaml_path}")
    with open(yaml_path) as f:
        raw = yaml.safe_load(f)
    spec = ExperimentSpec.model_validate(raw)
    spec.dir = yaml_path.parent
    if not spec.manifests_dir.is_dir():
        raise FileNotFoundError(f"manifests directory not found: {spec.manifests_dir}")
    return spec
