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
            "managed": {"roles": [
                {"name": "app_a", "login": True, "passwordSecret": {"name": "app-cred-a"}},
                {"name": "app_b", "login": True, "passwordSecret": {"name": "app-cred-b"}}]},
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


def test_backup_view_carries_phase_times_and_wal():
    b = {"metadata": {"name": "bk1", "creationTimestamp": "2026-01-01T00:00:00Z"},
         "status": {"phase": "completed", "startedAt": "t1", "stoppedAt": "t2",
                    "endWal": "000000010000000000000012"}}
    s = build_snapshot(cluster(), [], [], [b], False)
    assert s["backups"][0] == {"name": "bk1", "phase": "completed", "startedAt": "t1",
                               "stoppedAt": "t2", "endWal": "000000010000000000000012"}


def test_retention_policy_surfaced():
    c = cluster()
    c["spec"]["backup"]["retentionPolicy"] = "7d"
    assert build_snapshot(c, [], [], [], False)["retention"] == "7d"


def test_busy_locks_ops_but_not_chaos():
    # a running base backup makes the cluster busy (the exclusivity lock)
    running = {"metadata": {"name": "bk"}, "status": {"phase": "running"}}
    s = build_snapshot(cluster(), ["pg-2", "pg-3"], ["a"], [running], False)
    assert s["busy"] and s["busy_reason"] == "base backup running"
    caps = {c["id"]: c["enabled"] for c in capabilities(CNPG_ACTIONS, s)}
    assert caps["backup"] is False and caps["rotate"] is False   # mutating ops locked
    assert caps["kill-pod"] is True                          # chaos stays available


def test_snapshot_drives_the_capability_map_end_to_end():
    # the whole point: discovered state -> preconditions -> enabled controls
    s = build_snapshot(cluster(), ["pg-2", "pg-3"], ["a", "b", "c"],
                       [{"status": {"phase": "completed"}}], partitioned=True,
                       target="postgresql:16.6")
    caps = {c["id"]: c["enabled"] for c in capabilities(CNPG_ACTIONS, s)}
    assert caps["upgrade"] is True and caps["rotate"] is True
    assert caps["kill-pod"] is False   # a partition fault is in flight → interlock


def test_credentials_active_role_from_the_cluster_annotation():
    from k8ostester_pg.discover import _credentials
    roles = [{"name": "app_a"}, {"name": "app_b"}]
    # no annotation -> defaults to the first login role
    assert _credentials({}, roles) == {"active_role": "app_a", "rotated_at": "", "roles": ["app_a", "app_b"]}
    # annotation records the active role + when it rotated
    c = {"metadata": {"annotations": {"k8ostester.io/active-role": "app_b",
                                      "k8ostester.io/rotatedAt": "20260101000000"}}}
    got = _credentials(c, roles)
    assert got["active_role"] == "app_b" and got["rotated_at"] == "20260101000000"


def test_database_and_login_roles_for_connection_info():
    from k8ostester_pg.discover import _database, _login_roles
    spec = {"bootstrap": {"initdb": {"database": "orders", "owner": "app"}},
            "managed": {"roles": [
                {"name": "app_a", "login": True, "passwordSecret": {"name": "cred-a"},
                 "inRoles": ["app"]},
                {"name": "reporting", "login": False}]}}   # non-login role excluded
    assert _database(spec) == {"name": "orders", "owner": "app"}
    assert _login_roles(spec) == [
        {"name": "app_a", "secret": "cred-a", "in_roles": ["app"]}]
    assert _database({}) == {"name": "app", "owner": "app"}   # CNPG defaults


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


def test_wal_seg_index_is_lsn_ordered_and_timeline_independent():
    from k8ostester_pg.discover import wal_seg_index
    assert wal_seg_index("00000001000000000000001A") == 0x1A
    assert wal_seg_index("000000010000000000000100") == 256   # logical rollover
    # same LSN segment on a later timeline -> same index
    assert wal_seg_index("00000002000000000000001A") == 0x1A
    assert wal_seg_index("") is None and wal_seg_index("short") is None


def test_parse_archiver_reads_segment_counts_and_lag():
    from k8ostester_pg.discover import _parse_archiver
    # short form (no current WAL) — no lag computed
    assert _parse_archiver("42|000000010000000000000009|0") == {
        "archived": 42, "last": "000000010000000000000009", "failed": 0}
    # full form: last=0x09, current=0x0C -> 3 segments behind
    full = _parse_archiver(
        "42|000000010000000000000009|1|1700000000|00000001000000000000000C")
    assert full["lag_segments"] == 3 and full["last_time"] == "1700000000"
    assert full["current"] == "00000001000000000000000C" and full["failed"] == 1
    assert _parse_archiver("") == {}


def test_continuous_archiving_condition_surfaced():
    from k8ostester_pg.discover import build_snapshot
    c = cluster()
    c["status"]["conditions"] = [
        {"type": "ContinuousArchiving", "status": "True", "message": "OK"}]
    assert build_snapshot(c, [], [], [], False)["archiving"] == {"ok": True, "message": "OK"}
    # absent condition -> {}
    assert build_snapshot(cluster(), [], [], [], False)["archiving"] == {}


def test_parse_df_connections_and_slots():
    from k8ostester_pg.discover import _parse_conn, _parse_df, _parse_slots
    df = _parse_df("Filesystem 1024-blocks Used Available Capacity Mounted\n"
                   "/dev/sda1 1048576 262144 786432 25% /var/lib/postgresql/data")
    assert df == {"size": 1048576 * 1024, "used": 262144 * 1024, "pct": 25.0}
    assert _parse_conn("12|200") == {"active": 12, "max": 200}
    assert _parse_conn("bad") == {}
    slots = _parse_slots("pg-2|t|0\norphan|f|33554432\n")
    assert slots == [{"name": "pg-2", "active": True, "retained_bytes": 0},
                     {"name": "orphan", "active": False, "retained_bytes": 33554432}]


def test_parse_replication_maps_standby_to_sync_and_lag():
    from k8ostester_pg.discover import _parse_replication
    r = _parse_replication("pg-2|quorum|0\npg-3|async|8192\n")
    assert r["pg-2"] == {"sync_state": "quorum", "lag_bytes": 0}
    assert r["pg-3"] == {"sync_state": "async", "lag_bytes": 8192}
    assert _parse_replication("") == {}
