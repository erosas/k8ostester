#!/usr/bin/env python3
"""k8os-testbed — the production-readiness golden path.

A single, linear script (no engine) that provisions the ideal CNPG config and
walks it through the operations you must prove before production:

    provision → deploy cluster + app → steady → base backup
      → rotate credentials → minor PG upgrade → PITR restore → verify

Every step appends a line to events.jsonl (the console's annotation source, phase
2) and prints a human line. Orchestration is plain kubectl/helm — transparent and
standalone (it does not import k8ostester-core). Run against a kube context that
can install an operator:

    python flow.py                 # run the golden path
    python flow.py --context my-remote --keep
    python flow.py cleanup         # delete the testbed namespace (leaves operator)

Needs: kubectl, helm on PATH. See README.md.
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

NS = "k8os-testbed"
MANIFESTS = Path(__file__).parent / "manifests"
EVENTS = Path(__file__).parent / "events.jsonl"

CNPG_REPO = "https://cloudnative-pg.github.io/charts"
CNPG_CHART_VERSION = "0.28.3"          # CloudNativePG operator 1.29.x
PG_IMAGE_FROM = "ghcr.io/cloudnative-pg/postgresql:16.4"
PG_IMAGE_TO = "ghcr.io/cloudnative-pg/postgresql:16.6"   # minor upgrade target

CONTEXT: list[str] = []                # filled from --context
AZ = False                             # --az: spread across zones + verify cross-AZ sync

GF_LOCAL_PORT = 3000                   # local port for the Grafana port-forward
ANNOTATE_KINDS = {"backup", "rotate", "version", "restore", "summary"}
_gf_pf: subprocess.Popen | None = None  # persistent Grafana port-forward


# --------------------------------------------------------------------------- #
# shell + k8s helpers
# --------------------------------------------------------------------------- #
def sh(*args: str, input: str | None = None, check: bool = True) -> str:
    """Run a command, return stdout. Raises on non-zero unless check=False."""
    r = subprocess.run(
        list(args), input=input, capture_output=True, text=True
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"$ {' '.join(args)}\n{r.stdout}\n{r.stderr}".strip())
    return r.stdout.strip()


def kubectl(*args: str, input: str | None = None, check: bool = True) -> str:
    return sh("kubectl", *CONTEXT, *args, input=input, check=check)


def kns(*args: str, input: str | None = None, check: bool = True) -> str:
    return kubectl("-n", NS, *args, input=input, check=check)


def helm(*args: str) -> str:
    return sh("helm", *CONTEXT, *args)


def primary_pod() -> str:
    return kns("get", "cluster", "pg", "-o", "jsonpath={.status.currentPrimary}")


def psql(query: str, db: str = "app", pod: str | None = None) -> str:
    """Run a query on a cluster pod as the local superuser."""
    pod = pod or primary_pod()
    return kns("exec", pod, "-c", "postgres", "--",
               "psql", "-U", "postgres", "-d", db, "-tAqc", query)


def poll(desc: str, fn, timeout: int = 600, interval: int = 5):
    """Poll fn() until it returns truthy, or raise after timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for: {desc}")


# --------------------------------------------------------------------------- #
# events / reporting
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def event(step: str, kind: str, status: str, detail: str, **extra) -> None:
    rec = {"ts": now_iso(), "step": step, "kind": kind, "status": status,
           "detail": detail, **extra}
    with EVENTS.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    mark = {"ok": "✔", "fail": "✗", "info": "•"}.get(status, "•")
    print(f"  {mark} [{step}] {detail}")
    if kind in ANNOTATE_KINDS:
        annotate(f"{step}: {detail}", ["k8os-testbed", kind, status])


# --------------------------------------------------------------------------- #
# grafana annotations (best-effort — never break the flow)
# --------------------------------------------------------------------------- #
def start_grafana_pf() -> None:
    global _gf_pf
    try:
        _gf_pf = subprocess.Popen(
            ["kubectl", *CONTEXT, "-n", NS, "port-forward",
             "svc/grafana", f"{GF_LOCAL_PORT}:3000"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
    except Exception:
        _gf_pf = None


def stop_grafana_pf() -> None:
    if _gf_pf is not None:
        _gf_pf.terminate()


def annotate(text: str, tags: list[str]) -> None:
    if _gf_pf is None:
        return
    try:
        data = json.dumps(
            {"text": text, "tags": tags, "time": int(time.time() * 1000)}
        ).encode()
        auth = base64.b64encode(b"admin:admin").decode()
        req = urllib.request.Request(
            f"http://localhost:{GF_LOCAL_PORT}/api/annotations", data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Basic {auth}"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass  # annotations are a convenience, not a gate


def app_metrics() -> dict[str, float]:
    """Scrape the dummy app's /metrics from inside a pod (image has python)."""
    pod = kns("get", "pod", "-l", "app=dummy-app",
              "-o", "jsonpath={.items[0].metadata.name}")
    raw = kns("exec", pod, "--", "python", "-c",
              "import urllib.request as u;"
              "print(u.urlopen('http://localhost:8000/metrics').read().decode())")
    out: dict[str, float] = {}
    for line in raw.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        key, _, val = line.rpartition(" ")
        try:
            out[key] = float(val)
        except ValueError:
            pass
    return out


def app_ok_ops(m: dict[str, float]) -> float:
    return sum(v for k, v in m.items() if k.startswith("app_ops_total") and 'result="ok"' in k)


# --------------------------------------------------------------------------- #
# steps
# --------------------------------------------------------------------------- #
def provision() -> None:
    print("→ provision: operator, object store, cluster, app")
    helm("repo", "add", "cnpg", CNPG_REPO)
    helm("repo", "update", "cnpg")
    helm("upgrade", "--install", "cnpg", "cnpg/cloudnative-pg",
         "-n", "cnpg-system", "--create-namespace",
         "--version", CNPG_CHART_VERSION, "--wait")
    event("provision", "provision", "ok", "cnpg operator installed")

    kubectl("create", "namespace", NS, check=False)   # idempotent
    kns("apply", "-f", str(MANIFESTS / "01-seaweedfs.yaml"))
    kns("rollout", "status", "deploy/seaweedfs", "--timeout=180s")
    # create the backup bucket (Barman writes objects, never creates the bucket)
    sw = kns("get", "pod", "-l", "app=seaweedfs",
             "-o", "jsonpath={.items[0].metadata.name}")
    kns("exec", sw, "--", "sh", "-c",
        'echo "s3.bucket.create -name backups" | weed shell', check=False)
    event("provision", "provision", "ok", "seaweedfs + backups bucket ready")

    kns("apply", "-f", str(MANIFESTS / "02-cluster.yaml"))
    if AZ:
        # one instance per zone → primary alone in its AZ → sync is always cross-AZ
        poll("cluster object exists",
             lambda: kns("get", "cluster", "pg", "-o", "jsonpath={.metadata.name}") == "pg",
             timeout=60, interval=2)
        kns("patch", "cluster", "pg", "--type", "merge",
            "--patch-file", str(MANIFESTS / "az" / "spread.yaml"))
        event("provision", "provision", "ok", "AZ spread applied (1 instance/zone)")
    poll("cluster pg healthy (3/3)",
         lambda: kns("get", "cluster", "pg",
                     "-o", "jsonpath={.status.readyInstances}") == "3",
         timeout=600)
    event("provision", "provision", "ok", "cluster pg healthy (3 instances)")

    kns("apply", "-f", str(MANIFESTS / "03-app.yaml"))
    kns("rollout", "status", "deploy/app", "--timeout=180s")
    event("provision", "provision", "ok", "dummy app running")

    # the console: Prometheus + Grafana (dashboards-as-code)
    monitoring = Path(__file__).parent / "monitoring"
    kns("apply", "-f", str(monitoring / "prometheus.yaml"))
    kns("apply", "-f", str(monitoring / "grafana.yaml"))
    kns("rollout", "status", "deploy/prometheus", "--timeout=180s")
    kns("rollout", "status", "deploy/grafana", "--timeout=180s")
    event("provision", "provision", "ok", "prometheus + grafana console up")


def steady(seconds: int = 30) -> None:
    print(f"→ steady: {seconds}s baseline")
    time.sleep(seconds)
    m = app_metrics()
    event("steady", "steady", "ok",
          f"baseline: {int(app_ok_ops(m))} ok ops, app_up={int(m.get('app_up', 0))}")


def step_backup() -> bool:
    print("→ backup: base backup to the object store")
    name = "backup-" + datetime.now(timezone.utc).strftime("%H%M%S")
    kns("apply", "-f", "-", input=(
        "apiVersion: postgresql.cnpg.io/v1\nkind: Backup\n"
        f"metadata:\n  name: {name}\n"
        "spec:\n  cluster:\n    name: pg\n  method: barmanObjectStore\n"))
    ok = poll("backup completed",
              lambda: kns("get", "backup", name,
                          "-o", "jsonpath={.status.phase}") == "completed",
              timeout=600)
    event("backup", "backup", "ok" if ok else "fail", f"base backup {name} completed")
    return bool(ok)


def step_rotate_credentials() -> bool:
    print("→ rotate-credentials: blue/green switch (both roles valid → no auth gap)")
    # blue/green: the app uses app_<active>; we refresh the IDLE role's password
    # (safe — nothing is using it), then flip the selector and roll the app onto
    # it. Both roles stay valid throughout, so the rolling restart never hits a
    # rejected auth. Rollback = flip the selector back (old role untouched).
    active = kns("get", "configmap", "app-active", "-o", "jsonpath={.data.active}")
    idle = "b" if active == "a" else "a"
    new_pw = f"app-{idle}-" + datetime.now(timezone.utc).strftime("%H%M%S")
    # 1) refresh the idle role's password; operator reconciles ALTER ROLE
    kns("patch", "secret", f"app-cred-{idle}", "--type", "merge",
        "-p", json.dumps({"stringData": {"password": new_pw}}))
    time.sleep(20)
    before = app_ok_ops(app_metrics())
    # 2) flip the selector to the idle role and roll the app onto it
    kns("patch", "configmap", "app-active", "--type", "merge",
        "-p", json.dumps({"data": {"active": idle}}))
    kns("rollout", "restart", "deploy/app")
    kns("rollout", "status", "deploy/app", "--timeout=180s")
    # 3) assert the app recovered on the new role
    time.sleep(15)
    m = app_metrics()
    recovered = app_ok_ops(m) > before and int(m.get("app_up", 0)) == 1
    event("rotate", "rotate", "ok" if recovered else "fail",
          f"blue/green app_{active} → app_{idle} "
          + ("(recovered, both creds valid → no auth gap)" if recovered
             else "(app did NOT recover)"))
    return recovered


def step_minor_upgrade() -> bool:
    print(f"→ minor-upgrade: {PG_IMAGE_FROM.split(':')[-1]} → {PG_IMAGE_TO.split(':')[-1]}")
    v_from = psql("show server_version").split()[0]
    kns("patch", "cluster", "pg", "--type", "merge",
        "-p", json.dumps({"spec": {"imageName": PG_IMAGE_TO}}))
    # operator rolls replicas then switches over; wait for all pods on the new
    # image and the cluster healthy again
    poll("all instances on new image",
         lambda: PG_IMAGE_TO in kns(
             "get", "pods", "-l", "cnpg.io/cluster=pg",
             "-o", "jsonpath={.items[*].spec.containers[0].image}")
         and kns("get", "cluster", "pg",
                 "-o", "jsonpath={.status.readyInstances}") == "3",
         timeout=900)
    v_to = psql("show server_version").split()[0]
    ok = v_to != v_from
    event("upgrade", "version", "ok" if ok else "fail",
          f"{v_from} → {v_to}", **{"from": v_from, "to": v_to})
    return ok


def step_restore_pitr() -> bool:
    print("→ restore-pitr: restore a second cluster to a chosen point")
    # capture the target point: max id + the DB's own clock
    row = psql("select coalesce(max(id),0), now() from app_writes")
    target_id, target_time = [c.strip() for c in row.split("|")]
    # make sure WAL covering the target is archived before restoring
    psql("select pg_switch_wal()", db="postgres")
    psql("checkpoint", db="postgres")
    time.sleep(15)
    image = kns("get", "cluster", "pg", "-o", "jsonpath={.spec.imageName}")

    kns("delete", "cluster", "pg-restore", "--ignore-not-found")
    kns("apply", "-f", "-", input=f"""apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: pg-restore
spec:
  instances: 1
  imageName: {image}
  storage: {{size: 1Gi}}
  bootstrap:
    recovery:
      source: origin
      recoveryTarget:
        targetTime: "{target_time}"
  externalClusters:
    - name: origin
      barmanObjectStore:
        destinationPath: s3://backups/testbed
        endpointURL: http://seaweedfs.k8os-testbed.svc:8333
        serverName: pg
        s3Credentials:
          accessKeyId: {{name: seaweed-s3, key: ACCESS_KEY}}
          secretAccessKey: {{name: seaweed-s3, key: SECRET_KEY}}
""")
    poll("restore cluster healthy",
         lambda: kns("get", "cluster", "pg-restore",
                     "-o", "jsonpath={.status.readyInstances}") == "1",
         timeout=900)
    got = psql("select coalesce(max(id),0), count(*) from app_writes",
               pod="pg-restore-1")
    restored_id, count = [c.strip() for c in got.split("|")]
    # PITR is correct if the restore holds rows up to (not past) the target
    ok = int(restored_id) <= int(target_id) and int(count) > 0
    event("restore", "restore", "ok" if ok else "fail",
          f"PITR → {target_time}: restored max id {restored_id} "
          f"(target {target_id}), {count} rows")
    return ok


def pod_zone(pod: str) -> str:
    node = kns("get", "pod", pod, "-o", "jsonpath={.spec.nodeName}")
    return kubectl("get", "node", node, "-o",
                   r"jsonpath={.metadata.labels.topology\.kubernetes\.io/zone}")


def verify_sync_az() -> bool:
    print("→ verify-sync-az: every replica in a different AZ than the primary")
    primary = primary_pod()
    pz = pod_zone(primary)
    # CNPG sets application_name to the standby's pod name in pg_stat_replication
    standbys = [s for s in psql(
        "select application_name from pg_stat_replication", db="postgres"
    ).splitlines() if s.strip()]
    zones = {s: pod_zone(s) for s in standbys}
    cross = all(z and z != pz for z in zones.values())
    detail = (f"primary {primary}@{pz}; standbys " +
              ", ".join(f"{s}@{z}" for s, z in zones.items()))
    event("verify-sync-az", "verify", "ok" if cross else "fail",
          (detail + "  → all cross-AZ" if cross else detail + "  → SAME-AZ replica present"))
    return cross


def verify() -> bool:
    print("→ verify: cluster healthy and app serving")
    healthy = kns("get", "cluster", "pg",
                  "-o", "jsonpath={.status.readyInstances}") == "3"
    up = int(app_metrics().get("app_up", 0)) == 1
    ok = healthy and up
    event("verify", "verify", "ok" if ok else "fail",
          "cluster healthy and app serving" if ok else "cluster or app unhealthy")
    return ok


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def cleanup() -> None:
    print(f"→ cleanup: deleting namespace {NS} (operator left installed)")
    kubectl("delete", "namespace", NS, "--ignore-not-found", "--wait=false")


def run(keep: bool) -> int:
    EVENTS.write_text("")   # fresh event log per run
    provision()
    start_grafana_pf()      # so step events annotate the console
    try:
        steady(30)
        results = {
            "backup": step_backup(),
            "rotate": step_rotate_credentials(),
            "upgrade": step_minor_upgrade(),
            "restore": step_restore_pitr(),
            "verify": verify(),
        }
        if AZ:
            results["sync-az"] = verify_sync_az()
        passed = all(results.values())
        print("\n" + "=" * 60)
        print("GOLDEN PATH:", "PASS ✔" if passed else "FAIL ✗")
        for step, ok in results.items():
            print(f"  {'✔' if ok else '✗'} {step}")
        print("=" * 60)
        event("summary", "summary", "ok" if passed else "fail",
              "golden path " + ("PASSED" if passed else "FAILED"),
              results=results)
    finally:
        stop_grafana_pf()
    print("\nConsole:  kubectl -n {0} port-forward svc/grafana 3000:3000  "
          "→ http://localhost:3000 (admin/admin)".format(NS))
    if not keep:
        cleanup()
    else:
        print(f"[--keep] namespace {NS} left running; `python flow.py cleanup` to remove")
    return 0 if passed else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="k8os-testbed golden path")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "cleanup"])
    ap.add_argument("--context", help="kube context (default: current)")
    ap.add_argument("--keep", action="store_true",
                    help="leave the namespace running after the run")
    ap.add_argument("--az", action="store_true",
                    help="spread instances 1-per-zone and verify cross-AZ sync "
                         "(needs a multi-node, zone-labeled cluster — see kind/kind-az.yaml)")
    args = ap.parse_args()
    global AZ
    AZ = args.az
    if args.context:
        CONTEXT[:] = ["--context", args.context]
    if args.command == "cleanup":
        cleanup()
        return 0
    try:
        return run(args.keep)
    except Exception as e:
        print(f"\n✗ flow aborted: {e}", file=sys.stderr)
        event("abort", "abort", "fail", str(e).splitlines()[0][:200])
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
