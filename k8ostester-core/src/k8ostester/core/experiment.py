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
from pydantic import BaseModel, Field, field_validator
from k8ostester.core.exceptions import K8osConfigError

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
    rate: str | int | float | None = None  # "50/s" / bare number; 0 = pause, None = unthrottled
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
    endpoint_ro: str | None = None  # read-only endpoint; set → reads route here, writes to endpoint
    runner: Literal["journal", "pgbench"] = "journal"  # journal = built-in loadgen (full goal set)
    params: dict[str, Any] = {}  # runner-specific knobs, e.g. pgbench {scale: 20}
    workers: int = 1  # loadgen pods (Indexed Job); clients + rate shard across them
    image: str | None = None  # loadgen image override (prebuilt for private clusters, D12)
    pull_secret: str | None = None  # imagePullSecret name for the loadgen/pgbench Job
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
            raise K8osConfigError("goal needs either 'metric' or 'check'")
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

    @property
    def manifests_dir(self) -> Path:
        return (self.dir / self.config.manifests).resolve()

    @property
    def namespace_base(self) -> str:
        return self.cluster.namespace or f"exp-{self.name}"


def _deep_merge(base: dict, over: dict) -> dict:
    """over wins; nested dicts merge, everything else (incl. lists) replaces."""
    out = dict(base)
    for key, value in over.items():
        if isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_raw(yaml_path: Path, _seen: set[Path] | None = None) -> dict:
    """Parse experiment YAML, resolving `extends:` — a shared scenario file
    (load/faults/goals) that config variants merge over (the variant wins).
    This is how a comparison suite holds one scenario fixed across N configs:
    same faults and goals, only the manifests and endpoint differ."""
    _seen = _seen or set()
    if yaml_path in _seen:
        raise K8osConfigError(f"circular extends at {yaml_path}")
    _seen.add(yaml_path)
    with open(yaml_path) as f:
        raw = yaml.safe_load(f) or {}
    base_ref = raw.pop("extends", None)
    if base_ref is None:
        return raw
    base_path = (yaml_path.parent / base_ref).resolve()
    if not base_path.exists():
        raise FileNotFoundError(f"extends target not found: {base_path} (from {yaml_path})")
    return _deep_merge(_load_raw(base_path, _seen), raw)


def load_experiment(path: Path) -> ExperimentSpec:
    """Load and validate an experiment directory (or a direct experiment.yaml path)."""
    path = path.resolve()
    yaml_path = path / "experiment.yaml" if path.is_dir() else path
    if not yaml_path.exists():
        raise FileNotFoundError(f"no experiment.yaml at {yaml_path}")
    raw = _load_raw(yaml_path)
    spec = ExperimentSpec.model_validate(raw)
    spec.dir = yaml_path.parent
    if not spec.manifests_dir.is_dir():
        raise FileNotFoundError(f"manifests directory not found: {spec.manifests_dir}")
    return spec
