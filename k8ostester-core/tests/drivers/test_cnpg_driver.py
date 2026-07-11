import base64
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, ANY
from k8ostester.technologies.postgres_cnpg.driver import CnpgDriver
from k8ostester.core.experiment import (
    ExperimentSpec, FaultSpec, GoalSpec, LoadSpec, ClusterSpec,
)
from k8ostester.core.exceptions import K8osConfigError

@pytest.fixture
def mock_context(tmp_path):
    k8s = MagicMock()
    events = MagicMock()
    spec = ExperimentSpec(
        name="test-cnpg",
        technology="postgres-cnpg",
        dir=tmp_path,
        cluster=ClusterSpec(context="test-ctx"),
        infra=[]
    )
    return k8s, events, spec, "test-ns"

def stub_clusters(k8s, *names, poolers=()):
    """Route custom-object listings by plural: the CNPG Cluster list plus any
    Pooler CRs the test declares."""
    def list_custom(group, version, namespace, plural):
        if plural == "poolers":
            return {"items": [
                {"metadata": {"name": n}, "spec": {"type": "rw"}} for n in poolers
            ]}
        return {"items": [{"metadata": {"name": n}} for n in names]}
    k8s.custom.list_namespaced_custom_object.side_effect = list_custom

def stub_app_secret(k8s):
    """The <cluster>-app secret the driver reads DSN credentials from."""
    secret = MagicMock()
    secret.data = {
        k: base64.b64encode(v).decode()
        for k, v in {"dbname": b"app", "username": b"user", "password": b"pass"}.items()
    }
    k8s.core.read_namespaced_secret.return_value = secret

def test_cnpg_install_prereqs_declares_operator(mock_context):
    k8s, events, spec, ns = mock_context
    spec.infra = [{"operator": "cnpg"}]
    driver = CnpgDriver(k8s, spec, ns, events)

    with patch("k8ostester.technologies.postgres_cnpg.driver.Helm") as mock_helm_cls:
        mock_helm = mock_helm_cls.return_value
        driver.install_prereqs()

        mock_helm.repo_add.assert_called_with("cnpg", ANY)
        mock_helm.upgrade_install.assert_called_with(
            "cnpg", "cnpg/cloudnative-pg", "cnpg-system", version=ANY
        )

def test_cnpg_install_prereqs_missing_operator_error(mock_context):
    k8s, events, spec, ns = mock_context
    spec.infra = [] # No operator declared
    k8s.has_crd.return_value = False
    driver = CnpgDriver(k8s, spec, ns, events)

    with pytest.raises(RuntimeError, match="CloudNativePG is not installed"):
        driver.install_prereqs()

def test_cnpg_ensure_operator_tolerates_repo_blip(mock_context):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)

    from k8ostester.core.helm import HelmError
    with patch("k8ostester.technologies.postgres_cnpg.driver.Helm") as mock_helm_cls:
        mock_helm = mock_helm_cls.return_value
        mock_helm.repo_add.side_effect = HelmError("repo unreachable")

        # already installed: the blip is tolerated
        mock_helm.release_exists.return_value = True
        driver._ensure_operator()

        # not installed: the blip is fatal
        mock_helm.release_exists.return_value = False
        with pytest.raises(HelmError):
            driver._ensure_operator()

def test_cnpg_topology(mock_context):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)

    stub_clusters(k8s, "my-cluster")
    k8s.custom.get_namespaced_custom_object.return_value = {
        "status": {
            "currentPrimary": "pod-1",
            "instanceNames": ["pod-1", "pod-2"]
        }
    }

    topo = driver.topology()
    assert topo == {"primary": "pod-1", "replicas": ["pod-2"]}

def test_cnpg_cluster_name_requires_exactly_one(mock_context):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)

    stub_clusters(k8s)
    with pytest.raises(RuntimeError, match="expected exactly 1 CNPG Cluster"):
        driver.cluster_name
    stub_clusters(k8s, "a", "b")
    with pytest.raises(RuntimeError, match="found 2"):
        driver.cluster_name

def test_cnpg_start_load_journal(mock_context):
    k8s, events, spec, ns = mock_context
    spec.load = LoadSpec(
        phases=[{"duration": "10s", "rate": "10/s"}],
        endpoint="auto",
        workers=1
    )
    driver = CnpgDriver(k8s, spec, ns, events)

    stub_app_secret(k8s)
    stub_clusters(k8s, "my-cluster")

    driver.start_load(Path("/tmp"))

    k8s.core.create_namespaced_config_map.assert_called_once()
    k8s.batch.create_namespaced_job.assert_called_once()
    events.emit.assert_any_call("load.start", ANY, total_s=ANY)

def test_cnpg_start_load_pgbench(mock_context):
    k8s, events, spec, ns = mock_context
    spec.load = LoadSpec(
        runner="pgbench",
        phases=[{"duration": "10s", "rate": "10/s"}],
        workers=1
    )
    driver = CnpgDriver(k8s, spec, ns, events)

    stub_app_secret(k8s)
    stub_clusters(k8s, "my-cluster")
    k8s.custom.get_namespaced_custom_object.return_value = {
        "spec": {"imageName": "postgres:16"}
    }

    driver.start_load(Path("/tmp"))

    k8s.batch.create_namespaced_job.assert_called_once()
    events.emit.assert_any_call("load.start", ANY, total_s=ANY)

@pytest.mark.parametrize("load_kwargs,spec_updates,error", [
    ({}, {"faults": [FaultSpec(at="1s", worker="pod_kill")]}, "cannot run fault timelines"),
    ({"phases": [{"duration": "10s"}, {"duration": "10s"}]}, {}, "exactly one load phase"),
    ({"workers": 2}, {}, "supports workers: 1"),
    ({}, {"verify": ["integrity"]}, "no acked-write journal"),
    ({}, {"goals": [GoalSpec(metric="availability", min="99%")]}, "needs the journal runner"),
])
def test_cnpg_pgbench_rejects_journal_features(mock_context, load_kwargs, spec_updates, error):
    """pgbench has no acked-write journal and aborts on connection loss — the
    spec combinations that need either are rejected before anything deploys."""
    k8s, events, spec, ns = mock_context
    load = {"runner": "pgbench", "phases": [{"duration": "10s"}], **load_kwargs}
    spec = spec.model_copy(update={"load": LoadSpec(**load), **spec_updates})
    driver = CnpgDriver(k8s, spec, ns, events)

    with pytest.raises(K8osConfigError, match=error):
        driver.start_load(Path("/tmp"))

def test_cnpg_wait_load_started(mock_context, fake_clock):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)
    driver._workers = 1

    job_status = MagicMock()
    job_status.active = 1
    job_status.failed = 0
    k8s.batch.read_namespaced_job.return_value.status = job_status

    # First call no logs, second call start record
    with patch.object(driver, "_loadgen_logs") as mock_logs:
        mock_logs.side_effect = ["", '{"kind": "start"}']
        ts = driver.wait_load_started()
        assert ts > 0
        assert mock_logs.call_count == 2

def test_cnpg_wait_load_started_job_failure(mock_context, fake_clock):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)
    driver._workers = 1

    job_status = MagicMock()
    job_status.failed = 1
    k8s.batch.read_namespaced_job.return_value.status = job_status

    with patch.object(driver, "_loadgen_logs", return_value="boom"):
        with pytest.raises(RuntimeError, match="loadgen job failed"):
            driver.wait_load_started()

def test_cnpg_verify_integrity_passed(mock_context):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)
    driver._journal = [
        {"id": 1, "checksum": "abc"},
        {"id": 2, "checksum": "def"}
    ]

    with patch.object(driver, "_psql") as mock_psql:
        mock_psql.return_value = "1|abc\n2|def"
        res = driver.verify_integrity()
        assert res["passed"] is True
        assert "all present" in res["detail"]

def test_cnpg_verify_integrity_failed(mock_context):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)
    driver._journal = [
        {"id": 1, "checksum": "abc"},
        {"id": 2, "checksum": "def"}
    ]

    with patch.object(driver, "_psql") as mock_psql:
        # 1 is correct, 2 is missing
        mock_psql.return_value = "1|abc"
        res = driver.verify_integrity()
        assert res["passed"] is False
        assert res["missing"] == 1
        assert "1 acked writes LOST" in res["detail"]

def test_cnpg_ensure_backup(mock_context, fake_clock):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)

    stub_clusters(k8s, "my-cluster")

    # backup status: first pending then completed
    k8s.custom.get_namespaced_custom_object.side_effect = [
        {"status": {"phase": "pending"}},
        {"status": {"phase": "completed"}}
    ]

    driver.ensure_backup()
    assert driver._backup_name is not None
    k8s.custom.create_namespaced_custom_object.assert_called_once()

def test_cnpg_ensure_backup_failure(mock_context, fake_clock):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)

    stub_clusters(k8s, "my-cluster")
    k8s.custom.get_namespaced_custom_object.return_value = {
        "status": {"phase": "failed", "error": "object store unreachable"}
    }

    with pytest.raises(RuntimeError, match="object store unreachable"):
        driver.ensure_backup()

def test_cnpg_verify_pitr(mock_context, fake_clock):
    k8s, events, spec, ns = mock_context
    spec.load = LoadSpec(phases=[
        {"duration": "1s", "rate": "10/s"}, # phase 0
        {"duration": "1s", "rate": "0/s"},  # phase 1 (pause)
    ])
    driver = CnpgDriver(k8s, spec, ns, events)
    driver._journal = [
        {"id": 1, "phase": 0, "t": 100, "checksum": "x"}
    ]

    stub_clusters(k8s, "my-cluster")
    k8s.custom.get_namespaced_custom_object.side_effect = [
        {
            "spec": {
                "backup": {"barmanObjectStore": {}},
                "storage": {"size": "1Gi"}
            }
        }, # source cluster
        {"status": {"phase": "Cluster in healthy state", "instances": 1, "readyInstances": 1}}, # restore cluster
    ]

    with patch.object(driver, "_psql") as mock_psql:
        mock_psql.side_effect = [
            "ok", # pg_switch_wal
            "ok", # checkpoint
            "1",  # select id from k8ost_ops (on restore cluster)
        ]

        res = driver.verify_pitr()
        assert res["passed"] is True
        assert "exactly the 1 pre-pause rows" in res["detail"]

def test_cnpg_pitr_target_requires_pause_phase(mock_context):
    k8s, events, spec, ns = mock_context
    spec.load = LoadSpec(phases=[{"duration": "1s", "rate": "10/s"}])
    driver = CnpgDriver(k8s, spec, ns, events)
    driver._journal = [{"id": 1, "phase": 0, "t": 100}]

    with pytest.raises(RuntimeError, match="zero-rate pause phase"):
        driver._pitr_target()

def test_cnpg_pitr_target_requires_writes_before_pause(mock_context):
    k8s, events, spec, ns = mock_context
    spec.load = LoadSpec(phases=[
        {"duration": "1s", "rate": "0/s"},
        {"duration": "1s", "rate": "10/s"},
    ])
    driver = CnpgDriver(k8s, spec, ns, events)
    driver._journal = [{"id": 1, "phase": 1, "t": 100}]  # only after the pause

    with pytest.raises(RuntimeError, match="no acked writes before"):
        driver._pitr_target()

def test_cnpg_wait_ready(mock_context, fake_clock):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)

    stub_clusters(k8s, "my-cluster")
    k8s.custom.get_namespaced_custom_object.return_value = {
        "status": {"phase": "Cluster in healthy state", "instances": 1, "readyInstances": 1}
    }

    driver.wait_ready()
    k8s.wait_workloads_ready.assert_called_with(ns, timeout=300)

def test_cnpg_wait_load_done(mock_context, fake_clock):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)
    driver._workers = 1
    driver._load_total_s = 10
    driver._run_dir = spec.dir

    job_status = MagicMock()
    job_status.succeeded = 1
    job_status.failed = 0
    k8s.batch.read_namespaced_job.return_value.status = job_status

    with patch.object(driver, "_loadgen_logs") as mock_logs:
        mock_logs.return_value = '{"kind": "op", "op": "write", "id": 1, "ok": true, "checksum": "abc"}'
        driver.wait_load_done()
        assert len(driver._journal) == 1
        assert (spec.dir / "loadgen.log").exists()

def test_cnpg_wait_load_done_pgbench(mock_context, fake_clock):
    k8s, events, spec, ns = mock_context
    spec.load = LoadSpec(runner="pgbench", phases=[{"duration": "10s"}])
    driver = CnpgDriver(k8s, spec, ns, events)
    driver._workers = 1
    driver._load_total_s = 10
    driver._run_dir = spec.dir

    job_status = MagicMock()
    job_status.succeeded = 1
    job_status.failed = 0
    k8s.batch.read_namespaced_job.return_value.status = job_status

    with patch.object(driver, "_loadgen_logs") as mock_logs:
        mock_logs.return_value = "K8OST_TXNLOG_BEGIN\n1 1 1000 0 100 0\nK8OST_TXNLOG_END"
        driver.wait_load_done()
        assert len(driver._records) == 1
        assert driver._records[0]["lat_ms"] == 1.0

def test_cnpg_op_records(mock_context):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)
    driver._records = [
        {"kind": "op", "op": "read"},
        {"kind": "start"},
        {"kind": "op", "op": "write"}
    ]
    ops = driver.op_records
    assert len(ops) == 2
    assert all(o["kind"] == "op" for o in ops)

def test_cnpg_loadgen_logs_concatenates_pods(mock_context):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)

    pod_b, pod_a = MagicMock(), MagicMock()
    pod_b.metadata.name = "loadgen-1"
    pod_a.metadata.name = "loadgen-0"
    k8s.core.list_namespaced_pod.return_value.items = [pod_b, pod_a]
    k8s.pod_logs.side_effect = lambda ns_, pod: f"logs of {pod}"

    assert driver._loadgen_logs() == "logs of loadgen-0\nlogs of loadgen-1"

def test_cnpg_loadgen_logs_skips_pending_pod(mock_context):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)

    pod = MagicMock()
    pod.metadata.name = "loadgen-0"
    k8s.core.list_namespaced_pod.return_value.items = [pod]
    k8s.pod_logs.side_effect = Exception("ContainerCreating")

    assert driver._loadgen_logs() == ""

def test_cnpg_psql(mock_context):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)

    stub_clusters(k8s, "my-cluster")
    k8s.custom.get_namespaced_custom_object.return_value = {
        "status": {"currentPrimary": "pod-1", "instanceNames": ["pod-1"]}
    }

    driver._psql("select 1")
    k8s.exec_pod.assert_called_with(ns, "pod-1", ["psql", "-d", "app", "-qtAc", "select 1"], container="postgres")

@pytest.mark.parametrize("check,method", [
    ("integrity", "verify_integrity"),
    ("backup", "verify_backup"),
    ("pitr", "verify_pitr"),
])
def test_cnpg_verify_dispatch(mock_context, check, method):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)
    with patch.object(driver, method, return_value={"passed": True}) as mock_verify:
        assert driver.verify(check, {}) == {"passed": True}
        mock_verify.assert_called_once()

def test_cnpg_verify_unknown_check(mock_context):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)
    with pytest.raises(ValueError, match="unknown verify step"):
        driver.verify("nonsense", {})

def test_cnpg_verify_backup(mock_context):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)
    driver._backup_name = "test-backup"

    k8s.custom.get_namespaced_custom_object.return_value = {
        "status": {"phase": "completed", "beginLSN": "0/1", "endLSN": "0/2"}
    }

    res = driver.verify_backup()
    assert res["passed"] is True
    assert "completed" in res["detail"]

def test_cnpg_verify_backup_no_backup(mock_context):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)
    res = driver.verify_backup()
    assert res["passed"] is False
    assert "no backup was taken" in res["detail"]

def test_cnpg_resolve_resource_override(mock_context, tmp_path):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)
    # create an override manifest in the experiment dir
    manifests = spec.dir / "manifests"
    manifests.mkdir(exist_ok=True)
    override = manifests / "pitr-cluster.yaml"
    override.write_text("kind: Override")

    resolved = driver._resolve_resource("pitr-cluster.yaml")
    assert resolved == override
    assert resolved.read_text() == "kind: Override"

    # verify fallback for non-overridden resource
    resolved_fallback = driver._resolve_resource("backup.yaml")
    assert "k8ostester/technologies/postgres_cnpg/resources/backup.yaml" in str(resolved_fallback)


def test_cnpg_wait_cluster_healthy_timeout(mock_context, fake_clock):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)

    k8s.custom.get_namespaced_custom_object.return_value = {
        "status": {"phase": "pending", "readyInstances": 0, "instances": 1}
    }

    with pytest.raises(TimeoutError, match="not healthy"):
        driver._wait_cluster_healthy("my-cluster", timeout=100)
def test_cnpg_install_prereqs_unknown_infra_entry(mock_context):
    k8s, events, spec, ns = mock_context
    spec.infra = [{"operator": "not-cnpg"}]
    driver = CnpgDriver(k8s, spec, ns, events)
    with pytest.raises(ValueError, match="unknown infra entry"):
        driver.install_prereqs()

def test_cnpg_install_prereqs_preinstalled_operator_ok(mock_context):
    k8s, events, spec, ns = mock_context
    spec.infra = []  # operator not declared, but the CRD is already there
    k8s.has_crd.return_value = True
    CnpgDriver(k8s, spec, ns, events).install_prereqs()  # must not raise

def test_cnpg_deploy_substitutes_namespace(mock_context):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)
    k8s.apply_manifests.return_value = "cluster.postgresql.cnpg.io/pg created"

    driver.deploy()

    k8s.apply_manifests.assert_called_once_with(
        spec.manifests_dir, ns, variables={"K8OST_NAMESPACE": ns})
    events.emit.assert_any_call("manifest.applied", "cluster.postgresql.cnpg.io/pg created")

def test_cnpg_run_load_sequences_start_wait_done(mock_context):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)
    with patch.object(driver, "start_load") as start, \
         patch.object(driver, "wait_load_started") as started, \
         patch.object(driver, "wait_load_done") as done:
        driver.run_load(Path("/tmp"))
        start.assert_called_once()
        started.assert_called_once()
        done.assert_called_once()

def test_cnpg_pgbench_requires_cluster_image(mock_context):
    k8s, events, spec, ns = mock_context
    spec.load = LoadSpec(runner="pgbench", phases=[{"duration": "10s"}])
    driver = CnpgDriver(k8s, spec, ns, events)

    stub_app_secret(k8s)
    stub_clusters(k8s, "my-cluster")
    k8s.custom.get_namespaced_custom_object.return_value = {"spec": {}}  # no imageName

    with pytest.raises(RuntimeError, match="cannot determine cluster image"):
        driver.start_load(Path("/tmp"))

def test_cnpg_wait_load_started_retries_while_pods_pending(mock_context, fake_clock):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)
    driver._workers = 1

    job_status = MagicMock()
    job_status.active = 1
    job_status.failed = 0
    k8s.batch.read_namespaced_job.return_value.status = job_status

    with patch.object(driver, "_loadgen_logs") as mock_logs:
        # logs unavailable (pod listing raced) → treated as not started yet
        mock_logs.side_effect = [Exception("ContainerCreating"), '{"kind": "start"}']
        assert driver.wait_load_started() > 0

def test_cnpg_wait_load_done_job_failure(mock_context, fake_clock):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)
    driver._workers = 1
    driver._load_total_s = 10

    job_status = MagicMock()
    job_status.succeeded = 0
    job_status.failed = 1
    k8s.batch.read_namespaced_job.return_value.status = job_status

    with patch.object(driver, "_loadgen_logs", return_value="traceback"):
        with pytest.raises(RuntimeError, match="loadgen job failed"):
            driver.wait_load_done()

def test_cnpg_parse_loadgen_output_skips_noise_and_requires_acks(mock_context, tmp_path):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)

    logs = "\n".join([
        "Collecting psycopg[binary]==3.2.*",  # pip noise: not JSON
        '{"kind": "op", "op": "write", "ok": true, "id": 1, "checksum": "abc"}',
    ])
    driver._parse_loadgen_output(logs, tmp_path)
    assert len(driver._records) == 1
    assert len(driver._journal) == 1

    with pytest.raises(RuntimeError, match="no acked writes"):
        driver._parse_loadgen_output("pip noise only", tmp_path)

def test_cnpg_parse_pgbench_output_skips_noise_and_requires_txns(mock_context, tmp_path):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)

    logs = "\n".join([
        "starting vacuum...",           # outside the markers: ignored
        "K8OST_TXNLOG_BEGIN",
        "1 1",                          # short line: ignored
        "1 1 2000 0 100 500000",
        "K8OST_TXNLOG_END",
        "done.",
    ])
    driver._parse_pgbench_output(logs, tmp_path)
    assert len(driver._records) == 1
    assert driver._records[0]["lat_ms"] == 2.0

    with pytest.raises(RuntimeError, match="no transaction log"):
        driver._parse_pgbench_output("K8OST_TXNLOG_BEGIN\nK8OST_TXNLOG_END", tmp_path)

def test_cnpg_install_prereqs_delegates_common_infra(mock_context):
    k8s, events, spec, ns = mock_context
    spec.infra = ["chaos-mesh", {"operator": "cnpg"}]
    driver = CnpgDriver(k8s, spec, ns, events)

    with patch("k8ostester.technologies.postgres_cnpg.driver.Helm"), \
         patch("k8ostester.core.infra.Helm") as infra_helm_cls:
        driver.install_prereqs()
        infra_helm_cls.return_value.upgrade_install.assert_called_once()  # chaos-mesh

def test_cnpg_wait_load_started_waits_for_active_pods(mock_context, fake_clock):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)
    driver._workers = 1

    pending = MagicMock(active=0, succeeded=0, failed=0)
    active = MagicMock(active=1, succeeded=0, failed=0)
    k8s.batch.read_namespaced_job.return_value.status = pending
    statuses = iter([pending, active])
    k8s.batch.read_namespaced_job.side_effect = \
        lambda *a: MagicMock(status=next(statuses))

    with patch.object(driver, "_loadgen_logs", return_value='{"kind": "start"}'):
        assert driver.wait_load_started() > 0

def test_cnpg_live_sample_aggregates_journal(mock_context):
    k8s, events, spec, ns = mock_context
    spec.goals = [
        GoalSpec(metric="uptime", min="50%"),
        GoalSpec(metric="rto", max="10s"),  # fault-anchored: not live-scorable
    ]
    driver = CnpgDriver(k8s, spec, ns, events)

    logs = "\n".join([
        "pip noise",
        '{"kind": "start"}',
        '{"kind": "op", "op": "write", "ok": true, "t": 100, "id": 1, "checksum": "a"}',
        '{"kind": "op", "op": "write", "ok": false, "t": 108, "err": "OSError"}',
        '{"kind": "op", "op": "read", "ok": true, "t": 109}',
    ])
    sample = driver._live_sample(logs)

    assert sample["total_ops"] == 3
    assert sample["failed"] == 1
    assert sample["acked_writes"] == 1
    assert sample["ops_s"] == 0.3  # 3 ops in the 10s window ending at t=109
    (goal,) = sample["goals"]  # rto filtered out, uptime scored live
    assert goal["goal"] == "uptime"

def test_cnpg_live_sample_empty_logs(mock_context):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)
    assert driver._live_sample("pip noise only") is None

def test_cnpg_wait_load_done_emits_live_telemetry(mock_context, fake_clock):
    k8s, events, spec, ns = mock_context
    spec.goals = [GoalSpec(metric="error_rate", max="5%")]
    driver = CnpgDriver(k8s, spec, ns, events)
    driver._workers = 1
    driver._load_total_s = 10
    driver._run_dir = spec.dir

    running = MagicMock(succeeded=0, failed=0)
    done = MagicMock(succeeded=1, failed=0)
    statuses = iter([running, done])
    k8s.batch.read_namespaced_job.side_effect = lambda *a: MagicMock(status=next(statuses))

    logs = '{"kind": "op", "op": "write", "ok": true, "t": 100, "id": 1, "checksum": "a"}'
    with patch.object(driver, "_loadgen_logs", return_value=logs), \
         patch.object(driver, "topology_graph", return_value={
             "primary": "pg-1", "replicas": ["pg-2"],
             "nodes": [{"id": "pg-1", "role": "primary"}],
             "edges": [{"source": "pg-1", "target": "pg-2", "detail": "sync"}]}):
        driver.wait_load_done()

    sample_calls = [c for c in events.emit.call_args_list if c[0][0] == "load.sample"]
    assert len(sample_calls) == 1  # one per in-flight poll, none once done
    assert sample_calls[0][1]["total_ops"] == 1
    assert sample_calls[0][1]["goals"][0]["goal"] == "error_rate"

    topo_calls = [c for c in events.emit.call_args_list if c[0][0] == "topology"]
    assert topo_calls[0][1]["primary"] == "pg-1"
    assert topo_calls[0][1]["edges"][0]["detail"] == "sync"
    assert "pg-2 sync" in topo_calls[0][0][1]  # human-readable msg

def test_cnpg_wait_load_done_survives_telemetry_failure(mock_context, fake_clock):
    """Live telemetry is best-effort: a topology/log hiccup must not fail the run."""
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)
    driver._workers = 1
    driver._load_total_s = 10
    driver._run_dir = spec.dir

    running = MagicMock(succeeded=0, failed=0)
    done = MagicMock(succeeded=1, failed=0)
    statuses = iter([running, done])
    k8s.batch.read_namespaced_job.side_effect = lambda *a: MagicMock(status=next(statuses))

    logs = '{"kind": "op", "op": "write", "ok": true, "t": 1, "id": 1, "checksum": "a"}'
    with patch.object(driver, "_loadgen_logs", return_value=logs), \
         patch.object(driver, "topology_graph", side_effect=RuntimeError("no primary")):
        driver.wait_load_done()  # must not raise

def stub_cluster_status(k8s, **status):
    k8s.custom.get_namespaced_custom_object.return_value = {"status": status}

def test_cnpg_topology_graph_full_path(mock_context):
    """loadgen → pooler → primary with per-replica sync state from
    pg_stat_replication and health from the CR."""
    k8s, events, spec, ns = mock_context
    spec.load = LoadSpec(endpoint="pooler-rw", phases=[{"duration": "10s"}])
    driver = CnpgDriver(k8s, spec, ns, events)
    driver._run_dir = spec.dir  # load has started

    stub_clusters(k8s, "pg", poolers=["pooler-rw"])
    stub_cluster_status(
        k8s,
        currentPrimary="pg-1",
        instanceNames=["pg-1", "pg-2", "pg-3"],
        instancesStatus={"healthy": ["pg-1", "pg-2"], "failed": ["pg-3"]},
    )

    with patch.object(driver, "_psql", return_value="pg-2|quorum\n"):
        graph = driver.topology_graph()

    roles = {n["id"]: n["role"] for n in graph["nodes"]}
    assert roles == {"loadgen": "client", "pooler-rw": "proxy",
                     "pg-1": "primary", "pg-2": "replica", "pg-3": "replica"}
    details = {n["id"]: n.get("detail") for n in graph["nodes"]}
    assert details["pooler-rw"] == "pgbouncer (rw)"
    assert details["pg-3"] == "failed"

    edges = {(e["source"], e["target"]): e.get("detail") for e in graph["edges"]}
    assert ("loadgen", "pooler-rw") in edges
    assert ("pooler-rw", "pg-1") in edges
    assert edges[("pg-1", "pg-2")] == "quorum"
    assert edges[("pg-1", "pg-3")] == "detached"  # not in pg_stat_replication

def test_cnpg_topology_graph_direct_endpoint(mock_context):
    k8s, events, spec, ns = mock_context
    spec.load = LoadSpec(endpoint="auto", phases=[{"duration": "10s"}])
    driver = CnpgDriver(k8s, spec, ns, events)
    driver._run_dir = spec.dir  # load has started

    stub_clusters(k8s, "pg")
    stub_cluster_status(k8s, currentPrimary="pg-1", instanceNames=["pg-1"])

    with patch.object(driver, "_psql", return_value=""):
        graph = driver.topology_graph()

    edges = {(e["source"], e["target"]): e.get("detail") for e in graph["edges"]}
    assert edges[("loadgen", "pg-1")] == "pg-rw"  # via the rw service, no pooler

def test_cnpg_replication_states_unreachable_primary(mock_context):
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)
    with patch.object(driver, "_psql", side_effect=RuntimeError("pod is gone")):
        assert driver._replication_states("pg-1") == {}
    assert driver._replication_states(None) == {}

def test_cnpg_topology_graph_client_waiting_before_load_starts(mock_context):
    k8s, events, spec, ns = mock_context
    spec.load = LoadSpec(endpoint="auto", phases=[{"duration": "10s"}])
    driver = CnpgDriver(k8s, spec, ns, events)  # start_load not called

    stub_clusters(k8s, "pg")
    stub_cluster_status(k8s, currentPrimary="pg-1", instanceNames=["pg-1"])

    with patch.object(driver, "_psql", return_value=""):
        graph = driver.topology_graph()
    details = {n["id"]: n.get("detail") for n in graph["nodes"]}
    assert set(details) == {"loadgen", "pg-1"}  # client visible from the start
    assert "(waiting)" in details["loadgen"]

def test_cnpg_topology_graph_shows_bypassed_pooler(mock_context):
    """A deployed Pooler is part of the config under test even when the load
    plan connects directly."""
    k8s, events, spec, ns = mock_context
    spec.load = LoadSpec(endpoint="auto", phases=[{"duration": "10s"}])
    driver = CnpgDriver(k8s, spec, ns, events)
    driver._run_dir = spec.dir

    stub_clusters(k8s, "pg", poolers=["pg-pooler"])
    stub_cluster_status(k8s, currentPrimary="pg-1", instanceNames=["pg-1"])

    with patch.object(driver, "_psql", return_value=""):
        graph = driver.topology_graph()

    edges = {(e["source"], e["target"]): e.get("detail") for e in graph["edges"]}
    assert edges[("loadgen", "pg-1")] == "pg-rw"   # direct path
    assert ("pg-pooler", "pg-1") in edges           # pooler still on the map

def test_cnpg_wait_ready_emits_bootstrap_telemetry(mock_context, fake_clock):
    """During cluster bootstrap the topology view sees replicas joining."""
    k8s, events, spec, ns = mock_context
    driver = CnpgDriver(k8s, spec, ns, events)

    stub_clusters(k8s, "my-cluster")
    k8s.custom.get_namespaced_custom_object.return_value = {
        "status": {"phase": "Cluster in healthy state", "instances": 2, "readyInstances": 2,
                   "currentPrimary": "pg-1", "instanceNames": ["pg-1", "pg-2"]}
    }

    with patch.object(driver, "_psql", return_value="pg-2|quorum\n"):
        driver.wait_ready()

    topo_calls = [c for c in events.emit.call_args_list if c[0][0] == "topology"]
    assert topo_calls, "readiness polling should emit topology telemetry"
    assert topo_calls[0][1]["primary"] == "pg-1"
