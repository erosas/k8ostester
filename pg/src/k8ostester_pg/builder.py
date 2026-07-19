"""Generate a starter CNPG manifest from a few high-level choices.

A design aid, not a deploy path: the console's builder posts a set of options
(how many instances, pooler yes/no, backups + schedule, sync policy) and gets
back a Cluster — plus an optional Pooler and ScheduledBackup — to copy or apply.

The YAML lives entirely in resource templates (``resources/*.tmpl.yaml``) with
``${VAR}`` substitution; nothing here bakes manifest strings into Python.
"""
from __future__ import annotations

from importlib import resources
from string import Template

from k8ostester_pg.goals import GOALS, num

# sync policy choice -> (CNPG method, number). "async" omits the block entirely.
_SYNC = {"quorum": ("any", 1), "priority": ("first", 1)}


def _tmpl(name: str) -> Template:
    text = resources.files("k8ostester_pg").joinpath("resources", name).read_text()
    return Template(text)


def _clamp(value, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def build_manifest(opts: dict) -> str:
    """Render the manifest for these options. Unknown/blank fields fall back to
    sensible defaults, so a bare ``{}`` still yields a valid single-Cluster spec."""
    name = (opts.get("name") or "pg").strip()
    version = (opts.get("version") or "16.6").strip()
    storage = (opts.get("storage") or "10Gi").strip()
    instances = _clamp(opts.get("instances"), 1, 9, 3)

    # optional spec fragments, in the order they appear under spec
    extra = ""
    method_number = _SYNC.get(opts.get("sync") or "quorum")
    if method_number:
        extra += _tmpl("cluster-sync.tmpl.yaml").substitute(
            method=method_number[0], number=method_number[1])
    if opts.get("backups"):
        extra += _tmpl("cluster-backup.tmpl.yaml").substitute(
            bucket=(opts.get("bucket") or "backups").strip(),
            path=(opts.get("path") or name).strip(),
            endpoint=(opts.get("endpoint") or "http://seaweedfs:8333").strip(),
            secret=(opts.get("secret") or "seaweed-s3").strip(),
            retention=(opts.get("retention") or "7d").strip(),
        )

    # native Prometheus scrape (CNPG exposes metrics; the operator makes a PodMonitor)
    if opts.get("monitoring"):
        extra += _tmpl("cluster-monitoring.tmpl.yaml").substitute()

    # blue/green application roles for credential rotation: two login roles that
    # both inherit the app owner (so they share the data), each from its own
    # secret. The console's Rotate refreshes the idle one and switches to it.
    secret_docs = []
    if opts.get("app_roles"):
        owner, ra, rb = "app", "app_a", "app_b"
        sa, sb = "app-cred-a", "app-cred-b"
        extra += _tmpl("cluster-roles.tmpl.yaml").substitute(
            role_a=ra, role_b=rb, owner=owner, secret_a=sa, secret_b=sb)
        for role, secret, pw in ((ra, sa, "CHANGE-ME-app-a"), (rb, sb, "CHANGE-ME-app-b")):
            secret_docs.append(_tmpl("role-secret.tmpl.yaml").substitute(
                secret=secret, role=role, password=pw))

    docs = [*secret_docs, _tmpl("cluster.tmpl.yaml").substitute(
        name=name, instances=instances, version=version, storage=storage, extra=extra)]

    if opts.get("pooler"):
        docs.append(_tmpl("pooler.tmpl.yaml").substitute(
            name=name, instances=_clamp(opts.get("pooler_instances"), 1, 5, 2)))
    if opts.get("backups") and opts.get("schedule"):
        docs.append(_tmpl("scheduledbackup.tmpl.yaml").substitute(
            name=name, schedule=(opts.get("schedule_cron") or "0 0 2 * * *").strip()))
    # an OTEL endpoint => an OpenTelemetry Collector that scrapes the cluster's
    # metrics and exports OTLP to it (Prometheus stays available via PodMonitor)
    endpoint = (opts.get("otel_endpoint") or "").strip()
    if endpoint:
        docs.append(_tmpl("otel-collector.tmpl.yaml").substitute(name=name, endpoint=endpoint))

    # goals -> Prometheus alert rules (the same goals become dashboard waterlines)
    rules = _alert_rules(name, opts.get("goals") or {})
    if rules:
        docs.append(_tmpl("prometheus-rules.tmpl.yaml").substitute(name=name, rules=rules))

    return "\n---\n".join(d.strip() for d in docs) + "\n"


def _alert_rules(name: str, goals: dict) -> str:
    """The PrometheusRule entries for whichever goals are set (indented for YAML)."""
    pods = f"{name}-[0-9]+"
    frags = []
    for key, (_panel, alert, expr_t, summary_t) in GOALS.items():
        v = num(goals.get(key))
        if v is None:
            continue
        frags.append(_tmpl("prometheus-rule.tmpl.yaml").substitute(
            alert=alert, expr=expr_t.format(pods=pods, v=v),
            summary=summary_t.format(v=v), name=name))
    return "".join(frags)
