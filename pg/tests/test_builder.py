"""The manifest builder renders valid, well-formed CNPG YAML from options."""
import yaml
from k8ostester_pg.builder import build_manifest


def kinds(manifest: str) -> list[str]:
    return [d["kind"] for d in yaml.safe_load_all(manifest) if d]


def test_minimal_options_yield_a_single_valid_cluster():
    docs = list(yaml.safe_load_all(build_manifest({})))
    docs = [d for d in docs if d]
    assert len(docs) == 1
    c = docs[0]
    assert c["kind"] == "Cluster" and c["spec"]["instances"] == 3
    assert c["spec"]["postgresql"]["synchronous"] == {"method": "any", "number": 1}


def test_async_omits_the_synchronous_block():
    c = next(d for d in yaml.safe_load_all(build_manifest({"sync": "async"})) if d)
    assert "synchronous" not in c["spec"].get("postgresql", {})


def test_priority_sync_uses_first():
    c = next(d for d in yaml.safe_load_all(build_manifest({"sync": "priority"})) if d)
    assert c["spec"]["postgresql"]["synchronous"]["method"] == "first"


def test_pooler_and_backups_and_schedule_produce_extra_docs():
    m = build_manifest({"name": "db", "pooler": True, "pooler_instances": 3,
                        "backups": True, "bucket": "b", "path": "p", "retention": "30d",
                        "schedule": True, "schedule_cron": "0 30 1 * * *"})
    # backups emit a standalone ObjectStore CR (the Barman Cloud plugin), not an in-tree stanza
    assert kinds(m) == ["ObjectStore", "Cluster", "Pooler", "ScheduledBackup"]
    docs = {d["kind"]: d for d in yaml.safe_load_all(m) if d}
    store = docs["ObjectStore"]
    assert store["metadata"]["name"] == "db-store"
    assert store["spec"]["retentionPolicy"] == "30d"
    assert store["spec"]["configuration"]["destinationPath"] == "s3://b/p"
    # the cluster loads the plugin as its WAL archiver, pointing at that ObjectStore
    plugin = next(p for p in docs["Cluster"]["spec"]["plugins"]
                  if p["name"] == "barman-cloud.cloudnative-pg.io")
    assert plugin["isWALArchiver"] is True
    assert plugin["parameters"]["barmanObjectName"] == "db-store"
    assert "backup" not in docs["Cluster"]["spec"]   # no deprecated in-tree stanza
    # the ScheduledBackup drives the plugin too
    assert docs["ScheduledBackup"]["spec"]["method"] == "plugin"
    assert docs["ScheduledBackup"]["spec"]["pluginConfiguration"]["name"] == "barman-cloud.cloudnative-pg.io"
    assert docs["ScheduledBackup"]["spec"]["schedule"] == "0 30 1 * * *"
    assert docs["Pooler"]["spec"]["instances"] == 3
    assert docs["Pooler"]["metadata"]["name"] == "db-rw"


def test_backups_emit_a_plugin_install_snippet_with_the_chosen_barman_image():
    # default barman image appears in the leading install snippet
    m = build_manifest({"name": "db", "backups": True})
    assert "Barman Cloud plugin" in m
    assert "plugin-barman-cloud-sidecar:v0.13.0" in m
    assert "set env deploy/barman-cloud SIDECAR_IMAGE=" in m
    # default install URLs appear
    assert "cert-manager/cert-manager/releases" in m
    assert "plugin-barman-cloud/releases" in m
    # a custom (mirror) barman image + install manifests are threaded in verbatim
    m2 = build_manifest({"name": "db", "backups": True,
                         "barman_image": "registry.internal/mirror/barman-sidecar:v0.13.0",
                         "cert_manager_manifest": "https://mirror.internal/cert-manager.yaml",
                         "plugin_manifest": "https://mirror.internal/barman-plugin.yaml"})
    assert "registry.internal/mirror/barman-sidecar:v0.13.0" in m2
    assert "https://mirror.internal/cert-manager.yaml" in m2
    assert "https://mirror.internal/barman-plugin.yaml" in m2
    # the snippet is a YAML comment, so the manifest still parses to the real docs
    assert kinds(m) == ["ObjectStore", "Cluster"]
    # no backups → no snippet
    assert "Barman Cloud plugin" not in build_manifest({})


def test_zone_spread_emits_topology_constraints_scoped_to_the_cluster():
    c = next(d for d in yaml.safe_load_all(
        build_manifest({"name": "az", "instances": 3, "zone_spread": True})) if d)
    tsc = c["spec"]["topologySpreadConstraints"]
    assert tsc[0]["topologyKey"] == "topology.kubernetes.io/zone"
    assert tsc[0]["maxSkew"] == 1 and tsc[0]["whenUnsatisfiable"] == "DoNotSchedule"
    # scoped to instance pods — else the pooler pods (same cluster label) skew the spread
    assert tsc[0]["labelSelector"]["matchLabels"] == {
        "cnpg.io/cluster": "az", "cnpg.io/podRole": "instance"}
    # off by default, and never on a single instance (nothing to spread)
    assert "topologySpreadConstraints" not in next(
        d for d in yaml.safe_load_all(build_manifest({})) if d)["spec"]
    assert "topologySpreadConstraints" not in next(
        d for d in yaml.safe_load_all(
            build_manifest({"instances": 1, "zone_spread": True})) if d)["spec"]


def test_backup_credentials_secret_vs_iam_role():
    # default: an explicit key Secret + endpoint
    store = next(d for d in yaml.safe_load_all(
        build_manifest({"name": "db", "backups": True})) if d and d["kind"] == "ObjectStore")
    cfg = store["spec"]["configuration"]
    assert cfg["s3Credentials"]["accessKeyId"] == {"name": "seaweed-s3", "key": "ACCESS_KEY"}
    assert cfg["endpointURL"] == "http://seaweedfs:8333"
    assert "inheritFromIAMRole" not in cfg["s3Credentials"]
    # IAM role (node / IRSA): no stored keys, and a blank endpoint uses AWS's default
    store = next(d for d in yaml.safe_load_all(build_manifest(
        {"name": "db", "backups": True, "credentials": "iam", "endpoint": ""})) if d and d["kind"] == "ObjectStore")
    cfg = store["spec"]["configuration"]
    assert cfg["s3Credentials"] == {"inheritFromIAMRole": True}
    assert "endpointURL" not in cfg   # blank endpoint → omitted (AWS default)


def test_default_image_is_the_standard_trixie_operand():
    # standard images don't bundle barman-cloud (the plugin provides it) but carry the
    # common extensions — CNPG's recommended flavor for new clusters
    c = next(d for d in yaml.safe_load_all(build_manifest({})) if d)
    assert c["spec"]["imageName"] == "ghcr.io/cloudnative-pg/postgresql:17-standard-trixie"


def test_app_roles_emits_two_login_roles_and_their_secrets():
    m = build_manifest({"app_roles": True})
    docs = {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(m) if d}
    # two basic-auth secrets, one per role
    assert ("Secret", "app-cred-a") in docs and ("Secret", "app-cred-b") in docs
    assert docs[("Secret", "app-cred-a")]["stringData"]["username"] == "app_a"
    # both are login roles that inherit the app owner — the rotation prerequisite
    roles = next(d for k, d in docs.items() if k[0] == "Cluster")["spec"]["managed"]["roles"]
    assert {r["name"] for r in roles} == {"app_a", "app_b"}
    for r in roles:
        assert r["login"] is True and r["inRoles"] == ["app"]
    assert "app_roles" not in build_manifest({})  # off by default -> no managed roles
    assert "managed" not in next(d for d in yaml.safe_load_all(build_manifest({})) if d)["spec"]


def test_monitoring_and_otel_are_optional_and_render():
    plain = next(d for d in yaml.safe_load_all(build_manifest({})) if d)
    assert "monitoring" not in plain["spec"]

    m = build_manifest({"name": "db", "monitoring": True,
                        "otel_endpoint": "otel-collector.obs.svc:4317"})
    docs = list(yaml.safe_load_all(m))
    cluster = next(d for d in docs if d and d["kind"] == "Cluster")
    assert cluster["spec"]["monitoring"]["enablePodMonitor"] is True
    # an OTEL endpoint emits a collector (SA + RBAC + ConfigMap + Deployment)
    kinds = {d["kind"] for d in docs if d}
    assert {"ServiceAccount", "Role", "ConfigMap", "Deployment"} <= kinds
    # the OTEL collector's config ConfigMap (monitoring also emits a custom-queries CM)
    cm = next(d for d in docs if d and d["kind"] == "ConfigMap" and "config.yaml" in d.get("data", {}))
    assert "otel-collector.obs.svc:4317" in cm["data"]["config.yaml"]
    assert "regex: db" in cm["data"]["config.yaml"]   # scrapes this cluster's pods
    # monitoring attaches the custom-queries ConfigMap (connection age / idle-in-txn)
    q = next(d for d in docs if d and d["kind"] == "ConfigMap" and "queries.yaml" in d.get("data", {}))
    assert q["metadata"]["name"] == "db-queries"
    assert cluster["spec"]["monitoring"]["customQueriesConfigMap"] == [{"name": "db-queries", "key": "queries.yaml"}]


def test_dashboard_panels_adapt_to_the_config():
    import json

    from k8ostester_pg.dashboard import build_dashboard
    single = json.loads(build_dashboard({"name": "solo", "instances": 1, "backups": False}))
    titles = [p["title"] for p in single["panels"]]
    assert "Replication lag" not in titles and "WAL archiving" not in titles
    assert "Connections by role (credential)" in titles   # always present
    assert single["uid"] == "k8ost-solo"

    full = json.loads(build_dashboard({"name": "pg", "instances": 3, "backups": True}))
    ftitles = [p["title"] for p in full["panels"]]
    for t in ("Replication lag", "Replication & slots", "Backups & recovery window",
              "WAL archive lag", "WAL archive failures (recent)"):
        assert t in ftitles
    # queries scope to this cluster's instance pods, default 'pod' label
    assert 'pod=~"pg-[0-9]+"' in full["panels"][0]["targets"][0]["expr"]


def test_dashboard_scrape_label_is_configurable():
    import json

    from k8ostester_pg.dashboard import build_dashboard
    d = json.loads(build_dashboard({"name": "pg", "scrape_label": "instance"}))
    assert 'instance=~"pg-[0-9]+"' in d["panels"][0]["targets"][0]["expr"]
    # CNPG-metric panels honour the scrape label; resource panels keep cAdvisor's own
    # labels (pod / persistentvolumeclaim), which are not the CNPG scrape label
    conns = next(p for p in d["panels"] if p["title"].startswith("Active connections"))
    assert 'cnpg_backends_total{instance=~"pg-[0-9]+"}' in conns["targets"][0]["expr"]
    cpu = next(p for p in d["panels"] if p["title"].startswith("CPU"))
    assert 'pod=~"pg-[0-9]+"' in cpu["targets"][0]["expr"]
    # goals/alerts honour the same label (alerts need monitoring on)
    m = build_manifest({"name": "pg", "scrape_label": "instance", "monitoring": True,
                        "goals": {"repl_lag": 30}})
    rule = next(x for x in yaml.safe_load_all(m) if x and x["kind"] == "PrometheusRule")
    assert 'instance=~"pg-[0-9]+"' in rule["spec"]["groups"][0]["rules"][0]["expr"]


def test_alerts_need_monitoring_dashboard_waterline_does_not():
    import json

    from k8ostester_pg.dashboard import build_dashboard
    # goals set but no PodMonitor -> no PrometheusRule (nothing would load it)...
    kinds = {d["kind"] for d in yaml.safe_load_all(
        build_manifest({"goals": {"repl_lag": 30}})) if d}
    assert "PrometheusRule" not in kinds
    # ...but the dashboard waterline still renders (works over OTEL too)
    d = json.loads(build_dashboard({"name": "pg", "goals": {"repl_lag": 30}}))
    lag = next(p for p in d["panels"] if p["title"] == "Replication lag")
    assert lag["fieldConfig"]["defaults"]["thresholds"]["steps"][-1]["value"] == 30


def test_goals_become_waterlines_and_alert_rules():
    import json

    from k8ostester_pg.dashboard import build_dashboard
    opts = {"name": "pg", "instances": 3, "backups": True, "monitoring": True,
            "goals": {"repl_lag": 30, "connections": "", "archive_delay": 120}}

    # waterline: a red threshold line lands on the matching panel, not others
    d = json.loads(build_dashboard(opts))
    panel = {p["title"]: p for p in d["panels"]}
    lag = panel["Replication lag"]["fieldConfig"]["defaults"]
    assert lag["thresholds"]["steps"][-1]["value"] == 30
    assert lag["custom"]["thresholdsStyle"]["mode"] == "line"
    # no goal set for connections -> no threshold on that panel
    assert "thresholds" not in panel["Active connections (total)"]["fieldConfig"]["defaults"]

    # alerts: one PrometheusRule with a rule per set goal (connections skipped)
    docs = [x for x in yaml.safe_load_all(build_manifest(opts)) if x]
    rule = next(x for x in docs if x["kind"] == "PrometheusRule")
    alerts = {r["alert"]: r for r in rule["spec"]["groups"][0]["rules"]}
    assert set(alerts) == {"ReplicationLagHigh", "ArchiveDelayHigh"}
    assert alerts["ReplicationLagHigh"]["expr"] == 'cnpg_pg_replication_lag{pod=~"pg-[0-9]+"} > 30'


def test_cpu_memory_goals_are_percent_of_limit_txid_is_millions():
    # cpu/memory goals are a % of the per-instance limit; txid is in millions of xids.
    # The alert expr must carry the converted ABSOLUTE threshold (cores/Gi/xids).
    m = build_manifest({"name": "pg", "instances": 3, "monitoring": True,
                        "cpu": "2", "memory": "4Gi",
                        "goals": {"cpu": 80, "memory": 75, "txid": 150}})
    rule = next(x for x in yaml.safe_load_all(m) if x and x["kind"] == "PrometheusRule")
    expr = {r["alert"]: r["expr"] for r in rule["spec"]["groups"][0]["rules"]}
    assert "> 1.6" in expr["CpuHigh"]          # 80% of 2 cores
    assert "> 3.0" in expr["MemoryHigh"]       # 75% of 4Gi
    assert "> 150000000" in expr["TxidWraparound"]   # 150 million xids

    import json

    from k8ostester_pg.dashboard import build_dashboard
    d = json.loads(build_dashboard({"name": "pg", "instances": 3, "cpu": "2", "memory": "4Gi",
                                    "goals": {"cpu": 80}}))
    cpu = next(p for p in d["panels"] if p["title"].startswith("CPU"))
    assert cpu["fieldConfig"]["defaults"]["thresholds"]["steps"][-1]["value"] == 1.6   # same conversion


def test_disk_fill_window_goal_emits_predict_linear_alert_and_inverted_waterline():
    # rate-of-change / urgency: predict_linear extrapolates growth to a time-to-full.
    # Disk only — memory working-set sawtooths, so a linear projection there is unreliable.
    opts = {"name": "pg", "instances": 3, "monitoring": True, "goals": {"disk_fill": 24}}
    rule = next(x for x in yaml.safe_load_all(build_manifest(opts)) if x and x["kind"] == "PrometheusRule")
    expr = {r["alert"]: r["expr"] for r in rule["spec"]["groups"][0]["rules"]}
    assert "predict_linear(kubelet_volume_stats_used_bytes" in expr["DiskFillingSoon"]
    assert "24*3600) > kubelet_volume_stats_capacity_bytes" in expr["DiskFillingSoon"]
    assert "MemoryFillingSoon" not in expr   # memory projection intentionally dropped

    import json

    from k8ostester_pg.dashboard import build_dashboard
    d = json.loads(build_dashboard(opts))
    disk = next(p for p in d["panels"] if p["title"] == "Projected time to disk-full (hours)")
    steps = disk["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert steps == [{"color": "red", "value": None}, {"color": "green", "value": 24}]   # red BELOW the line
    assert not any(p["title"].startswith("Projected time to memory") for p in d["panels"])


def test_no_goals_no_prometheus_rule():
    kinds = {d["kind"] for d in yaml.safe_load_all(build_manifest({})) if d}
    assert "PrometheusRule" not in kinds


def test_schedule_requires_backups():
    # a ScheduledBackup with nowhere to store is meaningless — omit it
    assert "ScheduledBackup" not in kinds(build_manifest({"schedule": True}))


def test_instances_are_clamped():
    c = next(d for d in yaml.safe_load_all(build_manifest({"instances": 99})) if d)
    assert c["spec"]["instances"] == 9


def test_single_instance_omits_synchronous():
    # quorum sync with only 1 instance is invalid (no standby to wait on) — omit it
    c = next(d for d in yaml.safe_load_all(
        build_manifest({"instances": 1, "sync": "quorum"})) if d)
    assert "synchronous" not in c["spec"].get("postgresql", {})


def test_single_instance_disables_pdb():
    # CNPG's default PDB (minAvailable 1) blocks node drains forever with no standby;
    # a single-instance cluster must set enablePDB:false so the node can be cordoned.
    c = next(d for d in yaml.safe_load_all(build_manifest({"instances": 1})) if d)
    assert c["spec"]["enablePDB"] is False
    # multi-instance leaves CNPG's default PDBs in place (they allow replica disruptions)
    c = next(d for d in yaml.safe_load_all(build_manifest({"instances": 3})) if d)
    assert "enablePDB" not in c["spec"]


def test_compute_requests_and_optional_limits():
    # default: requests only (Burstable QoS)
    c = next(d for d in yaml.safe_load_all(build_manifest({})) if d)
    assert c["spec"]["resources"] == {"requests": {"cpu": "100m", "memory": "256Mi"}}
    assert "limits" not in c["spec"]["resources"]
    # limits mirror requests when asked (Guaranteed QoS)
    c = next(d for d in yaml.safe_load_all(
        build_manifest({"cpu": "2", "memory": "4Gi", "limits": True})) if d)
    assert c["spec"]["resources"]["requests"] == {"cpu": 2, "memory": "4Gi"}
    assert c["spec"]["resources"]["limits"] == {"cpu": 2, "memory": "4Gi"}


def test_image_repo_overrides_the_default_registry():
    # default: the official CNPG build at the given version
    c = next(d for d in yaml.safe_load_all(build_manifest({"version": "16.6"})) if d)
    assert c["spec"]["imageName"] == "ghcr.io/cloudnative-pg/postgresql:16.6"
    # a custom repo is joined to the bare tag
    c = next(d for d in yaml.safe_load_all(
        build_manifest({"version": "17.2", "image_repo": "my.mirror/pg"})) if d)
    assert c["spec"]["imageName"] == "my.mirror/pg:17.2"
    # a full reference in the version field is used verbatim (repo ignored)
    c = next(d for d in yaml.safe_load_all(
        build_manifest({"version": "reg.io/team/pg:16.6", "image_repo": "ignored"})) if d)
    assert c["spec"]["imageName"] == "reg.io/team/pg:16.6"
