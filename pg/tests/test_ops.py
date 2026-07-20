"""Unit tests for the multi-step CNPG ops — mock the k8s client, no cluster."""
from unittest.mock import MagicMock

from k8ostester_pg import ops


def cluster_obj(**meta):
    return {
        "metadata": {"annotations": meta.get("annotations", {})},
        "spec": {"imageName": "ghcr.io/cloudnative-pg/postgresql:16.4",
                 "storage": {"size": "1Gi"},
                 "backup": {"barmanObjectStore": {"destinationPath": "s3://backups/x"}},
                 "managed": {"roles": [
                     {"name": "app_a", "login": True, "passwordSecret": {"name": "app-cred-a"}},
                     {"name": "app_b", "login": True, "passwordSecret": {"name": "app-cred-b"}}]}},
        "status": {"currentPrimary": "pg-1"},
    }


def test_minor_upgrade_bare_tag_keeps_the_repo():
    k8s = MagicMock()
    k8s.custom.get_namespaced_custom_object.return_value = cluster_obj()
    ops.minor_upgrade(k8s, "ns", "16.6")   # bare tag -> current repo
    patch = k8s.custom.patch_namespaced_custom_object.call_args.args[-1]
    assert patch["spec"]["imageName"] == "ghcr.io/cloudnative-pg/postgresql:16.6"


def test_minor_upgrade_full_ref_switches_the_repo():
    k8s = MagicMock()
    k8s.custom.get_namespaced_custom_object.return_value = cluster_obj()
    ops.minor_upgrade(k8s, "ns", "my-mirror.io/pg/postgresql:16.6")   # full ref -> as-is
    patch = k8s.custom.patch_namespaced_custom_object.call_args.args[-1]
    assert patch["spec"]["imageName"] == "my-mirror.io/pg/postgresql:16.6"


def test_expand_storage_patches_the_new_size():
    k8s = MagicMock()
    k8s.custom.get_namespaced_custom_object.return_value = cluster_obj()   # currently 1Gi
    ops.expand_storage(k8s, "ns", "5Gi")
    patch = k8s.custom.patch_namespaced_custom_object.call_args.args[-1]
    assert patch == {"spec": {"storage": {"size": "5Gi"}}}


def test_maintenance_runs_whitelisted_sql_on_the_primary_app_db():
    k8s = MagicMock()
    cl = cluster_obj()
    cl["spec"]["bootstrap"] = {"initdb": {"database": "orders"}}
    k8s.custom.get_namespaced_custom_object.return_value = cl
    ops.maintenance(k8s, "ns", "vacuum", "pg")
    argv = k8s.exec_pod.call_args.args[2]
    assert argv == ["psql", "-U", "postgres", "-d", "orders", "-c", "VACUUM (ANALYZE)"]
    assert k8s.exec_pod.call_args.args[1] == "pg-1"   # the current primary


def test_maintenance_rejects_unknown_ops():
    import pytest
    k8s = MagicMock()
    with pytest.raises(RuntimeError):
        ops.maintenance(k8s, "ns", "drop-everything", "pg")
    k8s.exec_pod.assert_not_called()


def test_storage_expandable_reads_the_explicit_class():
    from unittest.mock import MagicMock
    k8s = MagicMock()
    cl = cluster_obj()
    cl["spec"]["storage"] = {"size": "1Gi", "storageClass": "fast"}
    k8s.custom.get_namespaced_custom_object.return_value = cl
    k8s.storage.read_storage_class.return_value = MagicMock(allow_volume_expansion=True)
    ok, sc = ops.storage_expandable(k8s, "ns", "pg")
    assert (ok, sc) == (True, "fast")
    assert k8s.storage.read_storage_class.call_args.args[0] == "fast"


def test_storage_expandable_false_when_class_disallows():
    from unittest.mock import MagicMock
    k8s = MagicMock()
    cl = cluster_obj()
    cl["spec"]["storage"] = {"size": "1Gi", "storageClass": "standard"}
    k8s.custom.get_namespaced_custom_object.return_value = cl
    k8s.storage.read_storage_class.return_value = MagicMock(allow_volume_expansion=None)
    assert ops.storage_expandable(k8s, "ns", "pg") == (False, "standard")


def test_qty_bytes_parses_k8s_quantities():
    assert ops._qty_bytes("10Gi") == 10 * 2**30
    assert ops._qty_bytes("500M") == 500 * 10**6
    assert ops._qty_bytes("1024") == 1024
    assert ops._qty_bytes("bad") is None


def test_expand_storage_rejects_a_shrink():
    from unittest.mock import MagicMock

    import pytest
    k8s = MagicMock()
    cl = cluster_obj()
    cl["spec"]["storage"] = {"size": "10Gi"}
    k8s.custom.get_namespaced_custom_object.return_value = cl
    with pytest.raises(RuntimeError, match="grow-only"):
        ops.expand_storage(k8s, "ns", "5Gi")   # smaller than current
    k8s.custom.patch_namespaced_custom_object.assert_not_called()


def test_expand_storage_rejects_a_noop():
    import pytest
    k8s = MagicMock()
    k8s.custom.get_namespaced_custom_object.return_value = cluster_obj()   # already 1Gi
    with pytest.raises(RuntimeError):
        ops.expand_storage(k8s, "ns", "1Gi")
    k8s.custom.patch_namespaced_custom_object.assert_not_called()


def test_rotate_alters_idle_role_and_records_active_on_the_cluster():
    k8s = MagicMock()
    # no active-role annotation yet -> defaults to the first role (app_a) as active
    k8s.custom.get_namespaced_custom_object.return_value = cluster_obj()
    msg = ops.rotate_credentials(k8s, "ns")
    # ALTER ROLE on the IDLE role (app_b), through the primary, quoting via :'pw'
    cmd = k8s.exec_pod.call_args.args[2]
    assert "alter role app_b password $pw$" in cmd[-1] and cmd[-1].endswith("$pw$")
    # the idle role's OWN secret is refreshed
    assert k8s.core.patch_namespaced_secret.call_args.args[0] == "app-cred-b"
    # active role recorded on the Cluster annotation (no configmap/deployment)
    patch = k8s.custom.patch_namespaced_custom_object.call_args.args[-1]
    assert patch["metadata"]["annotations"]["k8ostester.io/active-role"] == "app_b"
    k8s.apps.patch_namespaced_deployment.assert_not_called()
    assert "app_a → app_b" in msg


def test_rotate_uses_a_supplied_password_verbatim_even_with_special_chars():
    k8s = MagicMock()
    k8s.custom.get_namespaced_custom_object.return_value = cluster_obj()
    tricky = "a'b\\c$d!"
    ops.rotate_credentials(k8s, "ns", "pg", password=tricky)
    # the raw value sits inside a dollar-quoted literal — no escaping applied to it
    assert f"$pw${tricky}$pw$" in k8s.exec_pod.call_args.args[2][-1]
    # and the secret stores it raw
    assert k8s.core.patch_namespaced_secret.call_args.args[2]["stringData"]["password"] == tricky


def test_rotate_switches_back_when_app_b_is_active():
    k8s = MagicMock()
    k8s.custom.get_namespaced_custom_object.return_value = cluster_obj(
        annotations={"k8ostester.io/active-role": "app_b"})
    ops.rotate_credentials(k8s, "ns")
    assert "alter role app_a password" in k8s.exec_pod.call_args.args[2][-1]
    assert k8s.core.patch_namespaced_secret.call_args.args[0] == "app-cred-a"


def test_restore_creates_a_uniquely_named_recovery_cluster():
    k8s = MagicMock()
    k8s.custom.get_namespaced_custom_object.return_value = cluster_obj()
    ops.restore(k8s, "ns")
    body = k8s.custom.create_namespaced_custom_object.call_args.args[-1]
    assert body["kind"] == "Cluster" and body["metadata"]["name"].startswith("pg-restore-")
    # recover to latest (no recoveryTarget) from the source's object store
    assert body["spec"]["bootstrap"]["recovery"] == {"source": "origin"}
    assert body["spec"]["externalClusters"][0]["barmanObjectStore"]["serverName"] == "pg"
