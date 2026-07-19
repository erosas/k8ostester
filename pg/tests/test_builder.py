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
    assert kinds(m) == ["Cluster", "Pooler", "ScheduledBackup"]
    docs = {d["kind"]: d for d in yaml.safe_load_all(m) if d}
    assert docs["Cluster"]["spec"]["backup"]["retentionPolicy"] == "30d"
    assert docs["Cluster"]["spec"]["backup"]["barmanObjectStore"]["destinationPath"] == "s3://b/p"
    assert docs["Pooler"]["spec"]["instances"] == 3
    assert docs["ScheduledBackup"]["spec"]["schedule"] == "0 30 1 * * *"
    assert docs["Pooler"]["metadata"]["name"] == "db-rw"


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
    cm = next(d for d in docs if d and d["kind"] == "ConfigMap")
    assert "otel-collector.obs.svc:4317" in cm["data"]["config.yaml"]
    assert "regex: db" in cm["data"]["config.yaml"]   # scrapes this cluster's pods


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
    assert "pod=~" not in json.dumps(d)
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


def test_no_goals_no_prometheus_rule():
    kinds = {d["kind"] for d in yaml.safe_load_all(build_manifest({})) if d}
    assert "PrometheusRule" not in kinds


def test_schedule_requires_backups():
    # a ScheduledBackup with nowhere to store is meaningless — omit it
    assert "ScheduledBackup" not in kinds(build_manifest({"schedule": True}))


def test_instances_are_clamped():
    c = next(d for d in yaml.safe_load_all(build_manifest({"instances": 99})) if d)
    assert c["spec"]["instances"] == 9
