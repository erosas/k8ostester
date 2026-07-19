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


def test_schedule_requires_backups():
    # a ScheduledBackup with nowhere to store is meaningless — omit it
    assert "ScheduledBackup" not in kinds(build_manifest({"schedule": True}))


def test_instances_are_clamped():
    c = next(d for d in yaml.safe_load_all(build_manifest({"instances": 99})) if d)
    assert c["spec"]["instances"] == 9
