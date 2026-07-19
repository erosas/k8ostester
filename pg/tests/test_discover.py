"""Unit tests for the pure snapshot transform — no cluster."""
from k8ostester_kernel.control import capabilities
from k8ostester_pg.control import CNPG_ACTIONS
from k8ostester_pg.discover import build_snapshot, pg_version


def cluster(**status):
    return {
        "spec": {
            "instances": 3,
            "imageName": "ghcr.io/cloudnative-pg/postgresql:16.4",
            "backup": {"barmanObjectStore": {}},
            "managed": {"roles": [{"name": "app_a"}, {"name": "app_b"}]},
        },
        "status": {"currentPrimary": "pg-1", "readyInstances": 3, **status},
    }


def test_pg_version_extracts_the_tag():
    assert pg_version("ghcr.io/cloudnative-pg/postgresql:16.4") == "16.4"
    assert pg_version("no-tag") == ""


def test_healthy_snapshot_reflects_the_cluster():
    s = build_snapshot(cluster(), ["pg-2", "pg-3"], ["a", "b", "c"],
                       [{"status": {"phase": "completed"}}], partitioned=False,
                       target="ghcr.io/cloudnative-pg/postgresql:16.6")
    assert s["ready"] and s["primary"] == "pg-1"
    assert s["version"] == "16.4" and s["target"] == "16.6"
    assert s["backup_configured"] and s["backups_completed"] == 1 and s["pitr_window"]
    assert s["blue_green"] and s["zones"] == ["a", "b", "c"]
    assert s["fault_in_flight"] is False


def test_not_ready_when_instances_missing():
    assert build_snapshot(cluster(readyInstances=2), [], [], [], False)["ready"] is False


def test_upgrading_phase_detected():
    s = build_snapshot(cluster(phase="Upgrading cluster"), [], [], [], False)
    assert s["upgrading"] is True


def test_backup_view_carries_phase_and_times():
    b = {"metadata": {"name": "bk1", "creationTimestamp": "2026-01-01T00:00:00Z"},
         "status": {"phase": "completed", "startedAt": "t1", "stoppedAt": "t2"}}
    s = build_snapshot(cluster(), [], [], [b], False)
    assert s["backups"][0] == {"name": "bk1", "phase": "completed",
                               "startedAt": "t1", "stoppedAt": "t2"}


def test_busy_locks_ops_but_not_chaos():
    # a running base backup makes the cluster busy (the exclusivity lock)
    running = {"metadata": {"name": "bk"}, "status": {"phase": "running"}}
    s = build_snapshot(cluster(), ["pg-2", "pg-3"], ["a"], [running], False)
    assert s["busy"] and s["busy_reason"] == "base backup running"
    caps = {c["id"]: c["enabled"] for c in capabilities(CNPG_ACTIONS, s)}
    assert caps["backup"] is False and caps["rotate"] is False   # mutating ops locked
    assert caps["kill-primary"] is True                          # chaos stays available


def test_snapshot_drives_the_capability_map_end_to_end():
    # the whole point: discovered state -> preconditions -> enabled controls
    s = build_snapshot(cluster(), ["pg-2", "pg-3"], ["a", "b", "c"],
                       [{"status": {"phase": "completed"}}], partitioned=True,
                       target="postgresql:16.6")
    caps = {c["id"]: c["enabled"] for c in capabilities(CNPG_ACTIONS, s)}
    assert caps["upgrade"] is True and caps["rotate"] is True
    assert caps["kill-primary"] is False   # a partition fault is in flight → interlock


def test_sync_policy_reads_quorum_priority_and_async():
    from k8ostester_pg.discover import _sync_policy
    assert _sync_policy({"postgresql": {"synchronous": {"method": "any", "number": 1}}}) == {
        "mode": "quorum", "method": "any", "number": 1, "label": "quorum · any 1"}
    assert _sync_policy({"postgresql": {"synchronous": {"method": "first", "number": 1}}})["mode"] \
        == "priority"
    assert _sync_policy({"maxSyncReplicas": 2, "minSyncReplicas": 1})["mode"] == "quorum"
    assert _sync_policy({})["mode"] == "async"


def test_object_store_parses_bucket_path_and_endpoint():
    from k8ostester_pg.discover import _object_store
    os = _object_store({"backup": {"barmanObjectStore": {
        "destinationPath": "s3://pgbackups/pg", "endpointURL": "http://seaweedfs:8333"}}})
    assert os == {"configured": True, "endpoint": "http://seaweedfs:8333",
                  "bucket": "pgbackups", "path": "pg"}
    assert _object_store({})["configured"] is False   # no backup stanza → not configured


def test_parse_archiver_reads_segment_counts():
    from k8ostester_pg.discover import _parse_archiver
    assert _parse_archiver("42|000000010000000000000009|0") == {
        "archived": 42, "last": "000000010000000000000009", "failed": 0}
    assert _parse_archiver("") == {}


def test_parse_replication_maps_standby_to_sync_and_lag():
    from k8ostester_pg.discover import _parse_replication
    r = _parse_replication("pg-2|quorum|0\npg-3|async|8192\n")
    assert r["pg-2"] == {"sync_state": "quorum", "lag_bytes": 0}
    assert r["pg-3"] == {"sync_state": "async", "lag_bytes": 8192}
    assert _parse_replication("") == {}
