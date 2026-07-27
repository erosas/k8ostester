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

from k8ostester_pg.goals import GOALS, RUNBOOK_ANCHOR, clamp, goal_threshold

# alerts link back to the runbook doc; override the base for your own fork/host
RUNBOOK_BASE = "https://github.com/erosas/k8ostester/blob/main/docs/runbooks.md"

# the Barman Cloud plugin (out-of-tree backups) and its prerequisites — pinned so the
# emitted install snippet is copy-pasteable; bump when you move the cluster's plugin.
BARMAN_SIDECAR_IMAGE = "ghcr.io/cloudnative-pg/plugin-barman-cloud-sidecar:v0.13.0"
CERT_MANAGER_MANIFEST = "https://github.com/cert-manager/cert-manager/releases/download/v1.16.2/cert-manager.yaml"
BARMAN_PLUGIN_MANIFEST = "https://github.com/cloudnative-pg/plugin-barman-cloud/releases/download/v0.13.0/manifest.yaml"


def _backup_prereq_note(barman_image: str, cert_manager: str, plugin_manifest: str) -> str:
    """A YAML comment header explaining the plugin these backup manifests need, and
    how to point its sidecar at a mirror — so the generated bundle is self-documenting.
    The manifest URLs and sidecar image are all overridable (mirror/proxy/pinned version)."""
    return (
        "# ── Backups use the CloudNativePG Barman Cloud plugin ──────────────────────\n"
        "# Install it once in the cluster (it also needs cert-manager):\n"
        f"#   kubectl apply -f {cert_manager}\n"
        f"#   kubectl apply -f {plugin_manifest}\n"
        f"# Sidecar image: {barman_image}\n"
        "# To pull it from your mirror instead, override after install:\n"
        f"#   kubectl -n cnpg-system set env deploy/barman-cloud SIDECAR_IMAGE={barman_image}\n"
        "# ───────────────────────────────────────────────────────────────────────────\n"
    )

# sync policy choice -> (CNPG method, number). "async" omits the block entirely.
_SYNC = {"quorum": ("any", 1), "priority": ("first", 1)}


def _tmpl(name: str) -> Template:
    text = resources.files("k8ostester_pg").joinpath("resources", name).read_text()
    return Template(text)


def build_manifest(opts: dict) -> str:
    """Render the manifest for these options. Unknown/blank fields fall back to
    sensible defaults, so a bare ``{}`` still yields a valid single-Cluster spec."""
    name = (opts.get("name") or "pg").strip()
    # the standard Debian-trixie operand image: PostgreSQL + the common extensions
    # (pgvector, pgaudit, failover slots, JIT) and no bundled barman-cloud — backups
    # come from the separate Barman Cloud plugin (below). CNPG's recommended flavor for
    # new clusters. A rolling major tag CNPG keeps patched; pin a full ref to fix it.
    version = (opts.get("version") or "17-standard-trixie").strip()
    repo = (opts.get("image_repo") or "ghcr.io/cloudnative-pg/postgresql").strip()
    # a bare tag is joined to the repo; a value that already looks like a full
    # reference (has a registry path or an explicit tag) is used as-is
    image = version if ("/" in version or ":" in version) else f"{repo}:{version}"
    storage = (opts.get("storage") or "10Gi").strip()
    instances = clamp(opts.get("instances"), 1, 9, 3)

    # compute per instance. Setting limits == requests gives the pod Guaranteed
    # QoS (it won't be evicted under node pressure) — the production choice.
    cpu = (opts.get("cpu") or "100m").strip()
    memory = (opts.get("memory") or "256Mi").strip()
    resources = f"  resources:\n    requests: {{cpu: {cpu}, memory: {memory}}}\n"
    if opts.get("limits"):
        resources += f"    limits: {{cpu: {cpu}, memory: {memory}}}\n"

    # optional spec fragments, in the order they appear under spec
    extra = ""
    # synchronous replication needs at least one standby to wait on — omit it for a
    # single instance (CNPG rejects a sync number >= the instance count).
    method_number = _SYNC.get(opts.get("sync") or "quorum")
    if method_number and instances >= 2:
        extra += _tmpl("cluster-sync.tmpl.yaml").substitute(
            method=method_number[0], number=method_number[1])
    # a single-instance cluster must disable the PodDisruptionBudget: CNPG's default
    # PDB (minAvailable 1) allows zero disruptions when there's no standby, so a node
    # drain blocks forever — the node can never be cordoned without deleting the pod.
    if instances < 2:
        extra += "  enablePDB: false\n"
    # multi-AZ: hard-spread instances one-per-zone (maxSkew 1, DoNotSchedule) so the
    # cluster survives a zone loss, and — with quorum sync — the sync replica is always
    # cross-AZ for free. Needs nodes labelled topology.kubernetes.io/zone (≥ instances
    # zones); a displaced pod stays Pending during an outage rather than doubling up.
    if opts.get("zone_spread") and instances >= 2:
        extra += _tmpl("cluster-topology.tmpl.yaml").substitute(name=name)
    # backups go through the CloudNativePG Barman Cloud plugin (the in-tree
    # spec.backup.barmanObjectStore is deprecated): a standalone ObjectStore CR holds
    # the destination, and the cluster loads the plugin as its WAL archiver.
    store_docs = []
    barman_image = (opts.get("barman_image") or BARMAN_SIDECAR_IMAGE).strip()
    cert_manager = (opts.get("cert_manager_manifest") or CERT_MANAGER_MANIFEST).strip()
    plugin_manifest = (opts.get("plugin_manifest") or BARMAN_PLUGIN_MANIFEST).strip()
    if opts.get("backups"):
        store = f"{name}-store"
        extra += _tmpl("cluster-plugins.tmpl.yaml").substitute(store=store)
        # endpoint: explicit for S3-compatible stores (SeaweedFS/MinIO); blank uses AWS's
        # default endpoint. Absent (bare opts) keeps the local dev default.
        ep = opts.get("endpoint")
        endpoint = (ep if ep is not None else "http://seaweedfs:8333").strip()
        endpoint_line = f"    endpointURL: {endpoint}\n" if endpoint else ""
        # credentials: an explicit key Secret, or the node/IRSA IAM role (no stored keys)
        if opts.get("credentials") == "iam":
            s3credentials = "    s3Credentials:\n      inheritFromIAMRole: true\n"
        else:
            secret = (opts.get("secret") or "seaweed-s3").strip()
            s3credentials = (
                "    s3Credentials:\n"
                f"      accessKeyId: {{name: {secret}, key: ACCESS_KEY}}\n"
                f"      secretAccessKey: {{name: {secret}, key: SECRET_KEY}}\n")
        store_docs.append(_tmpl("objectstore.tmpl.yaml").substitute(
            store=store,
            bucket=(opts.get("bucket") or "backups").strip(),
            path=(opts.get("path") or name).strip(),
            endpoint_line=endpoint_line,
            s3credentials=s3credentials,
            retention=(opts.get("retention") or "7d").strip(),
        ))

    # native Prometheus scrape (CNPG exposes metrics; the operator makes a PodMonitor).
    # We also attach a custom-queries ConfigMap so CNPG exposes connection age +
    # idle-in-transaction (not in the default metric set) — for the ORR dashboards.
    mon_docs = []
    if opts.get("monitoring"):
        extra += _tmpl("cluster-monitoring.tmpl.yaml").substitute(name=name)
        mon_docs.append(_tmpl("custom-queries.tmpl.yaml").substitute(name=name))

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

    docs = [*store_docs, *secret_docs, *mon_docs, _tmpl("cluster.tmpl.yaml").substitute(
        name=name, instances=instances, image=image, storage=storage,
        resources=resources, extra=extra)]

    if opts.get("pooler"):
        docs.append(_tmpl("pooler.tmpl.yaml").substitute(
            name=name, instances=clamp(opts.get("pooler_instances"), 1, 5, 2)))
    if opts.get("backups") and opts.get("schedule"):
        docs.append(_tmpl("scheduledbackup.tmpl.yaml").substitute(
            name=name, schedule=(opts.get("schedule_cron") or "0 0 2 * * *").strip()))
    # an OTEL endpoint => an OpenTelemetry Collector that scrapes the cluster's
    # metrics and exports OTLP to it (Prometheus stays available via PodMonitor)
    endpoint = (opts.get("otel_endpoint") or "").strip()
    if endpoint:
        docs.append(_tmpl("otel-collector.tmpl.yaml").substitute(name=name, endpoint=endpoint))

    # goals -> Prometheus alert rules — only with a PodMonitor (a PrometheusRule is
    # consumed by the Prometheus Operator; over OTEL-only there's nothing to load it).
    # The same goals still become dashboard waterlines regardless.
    if opts.get("monitoring"):
        rules = _alert_rules(name, opts.get("goals") or {},
                             (opts.get("scrape_label") or "pod").strip(), opts)
        if rules:
            docs.append(_tmpl("prometheus-rules.tmpl.yaml").substitute(name=name, rules=rules))

    manifest = "\n---\n".join(d.strip() for d in docs) + "\n"
    # lead with the plugin-install snippet (pointed at the chosen sidecar image) so the
    # backup manifests below are self-documenting about their one cluster prerequisite
    if opts.get("backups"):
        manifest = _backup_prereq_note(barman_image, cert_manager, plugin_manifest) + manifest
    return manifest


def _alert_rules(name: str, goals: dict, label: str = "pod", opts: dict | None = None) -> str:
    """The PrometheusRule entries for whichever goals are set (indented for YAML).
    Goal values are converted to absolute thresholds (CPU/mem % → cores/Gi, txid
    millions → xids) so the expr and the dashboard waterline agree."""
    pods = f"{name}-[0-9]+"
    o = opts or {}
    frags = []
    for key, (_panel, alert, expr_t, summary_t) in GOALS.items():
        v = goal_threshold(key, goals.get(key), o)
        if v is None:
            continue
        anchor = RUNBOOK_ANCHOR.get(key, "")
        frags.append(_tmpl("prometheus-rule.tmpl.yaml").substitute(
            alert=alert, expr=expr_t.format(pods=pods, v=v, label=label),
            summary=summary_t.format(v=v), name=name,
            runbook=f"{RUNBOOK_BASE}#{anchor}" if anchor else RUNBOOK_BASE))
    return "".join(frags)
