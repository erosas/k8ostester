"""The remote-control console server.

Enumerates the kubeconfig's contexts, discovers the CNPG clusters in the selected
context, streams the selected cluster's snapshot + capability map over SSE, and
executes gated actions against it. Stdlib http.server — no framework, no new deps.
See docs/remote-control.md.

    k8ost-console                                    # all contexts, pick in the UI
    k8ost-console --context prod --namespace prod-east --cluster orders
    # → open http://127.0.0.1:8700
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from k8ostester_kernel.control import capabilities
from k8ostester_kernel.k8s import (
    IN_CLUSTER,
    ClusterClient,
    in_cluster_namespace,
    running_in_cluster,
)
from kubernetes import config

from k8ostester_pg import builder, dashboard, discover, execute
from k8ostester_pg.control import CNPG_ACTIONS

SPA = (Path(__file__).parent / "console.html").read_text()


def _read_kubeconfig_contexts() -> tuple[list[str], str | None]:
    """Enumerate context names + current-context straight from the kubeconfig
    file(s), tolerating a missing current-context (which the client API rejects)."""
    import os

    import yaml
    paths = [p for p in os.environ.get("KUBECONFIG", "").split(os.pathsep) if p] \
        or [os.path.expanduser("~/.kube/config")]
    names: list[str] = []
    current: str | None = None
    for p in paths:
        try:
            with open(p) as f:
                doc = yaml.safe_load(f) or {}
        except Exception:
            continue
        names += [c["name"] for c in (doc.get("contexts") or []) if c.get("name")]
        current = current or doc.get("current-context")
    seen: set[str] = set()
    names = [n for n in names if not (n in seen or seen.add(n))]
    return names, current


class Console:
    """The control plane over the kubeconfig: pick a context + CNPG cluster, then
    discover / gate / execute against it.

    Discovery of the *selected* cluster runs on ONE shared background timer, not
    per SSE connection, so cluster load is fixed regardless of how many tabs watch.
    Switching selection resets the cache and re-warms it.
    """

    def __init__(self, context: str | None = None, namespace: str = "",
                 cluster: str = "", target: str = "", grafana: str = "",
                 interval: float = 2.0, heavy_interval: float = 20.0,
                 start: bool = True):
        self.target = target
        self.grafana = grafana.rstrip("/")     # base URL to deep-link the dashboard
        self._only_context = context           # restrict the picker to one context
        self._scope_ns = namespace             # namespace fallback for cluster listing
        self._interval = interval
        self._heavy_interval = heavy_interval
        self._clients: dict[str | None, ClusterClient] = {}
        self._contexts, self._current = self._load_contexts()
        self._sel: dict | None = None          # {context, namespace, name}
        self._lock = threading.Lock()
        self._cache: dict = {"unselected": True}
        self._heavy: dict = {}
        if namespace and cluster:              # a fully-specified launch target
            self._sel = {"context": context or self._current,
                         "namespace": namespace, "name": cluster}
        if start:
            if self._sel:
                self.refresh_heavy()
                self.refresh()
            threading.Thread(target=self._refresh_loop, daemon=True).start()
            threading.Thread(target=self._heavy_loop, daemon=True).start()

    # --- kubeconfig / client plumbing -------------------------------------
    def _load_contexts(self) -> tuple[list[str], str | None]:
        names: list[str] = []
        current: str | None = None
        try:
            ctxs, active = config.list_kube_config_contexts()
            names = [c["name"] for c in ctxs]
            current = (active or {}).get("name")
        except Exception:
            # list_kube_config_contexts() hard-fails if current-context is unset,
            # even though the contexts exist — read them out of the file directly
            names, current = _read_kubeconfig_contexts()
        if not current and names:
            current = names[0]
        if self._only_context:
            names, current = [self._only_context], self._only_context
        return names, current

    def client(self, context: str | None) -> ClusterClient:
        ctx = context or self._current
        if ctx not in self._clients:
            self._clients[ctx] = ClusterClient(ctx)
        return self._clients[ctx]

    def _reset_client(self, context: str | None) -> None:
        # a long-lived client can hold a dead connection (the API server dropped
        # it); dropping it forces a fresh one on the next call, so we self-heal
        self._clients.pop(context or self._current, None)

    # --- inventory + selection --------------------------------------------
    def contexts_info(self) -> dict:
        return {"contexts": self._contexts, "current": self._current,
                "selected": self._sel, "locked": bool(self._only_context),
                "grafana": self.grafana}

    def list_clusters(self, context: str | None) -> list[dict]:
        try:
            return discover.list_clusters(self.client(context), self._scope_ns or None)
        except Exception:
            self._reset_client(context)                 # stale connection -> retry fresh once
            return discover.list_clusters(self.client(context), self._scope_ns or None)

    def select(self, context: str | None, namespace: str, name: str) -> None:
        with self._lock:
            self._sel = {"context": context or self._current,
                         "namespace": namespace, "name": name}
            self._heavy = {}
            self._cache = {"unselected": False, "warming": True}
        self.refresh_heavy()
        self.refresh()

    # --- discovery tiers ---------------------------------------------------
    def _safe_state(self) -> dict:
        sel = self._sel
        if not sel:
            return {"unselected": True}
        try:
            k8s = self.client(sel["context"])
            snap = discover.snapshot(k8s, sel["namespace"], name=sel["name"],
                                     target=self.target)
            snap.update(self._heavy)
            return {"snapshot": snap, "capabilities": capabilities(CNPG_ACTIONS, snap)}
        except Exception as e:
            self._reset_client(sel["context"])          # rebuild on the next tick (self-heal)
            return {"error": str(e).splitlines()[0][:200]}

    def refresh(self) -> dict:
        st = self._safe_state()
        with self._lock:
            self._cache = st
        return st

    def refresh_heavy(self) -> None:
        sel = self._sel
        if not sel:
            self._heavy = {}
            return
        try:
            self._heavy = discover.heavy(self.client(sel["context"]),
                                         sel["namespace"], name=sel["name"])
        except Exception:
            pass                               # keep last-known on a transient error

    def _refresh_loop(self) -> None:
        while True:
            self.refresh()
            time.sleep(self._interval)

    def _heavy_loop(self) -> None:
        while True:
            time.sleep(self._heavy_interval)
            self.refresh_heavy()

    def state(self) -> dict:
        with self._lock:
            return self._cache

    # --- actions -----------------------------------------------------------
    def act(self, action_id: str, params: dict | None = None) -> str:
        sel = self._sel
        if not sel:
            raise RuntimeError("no cluster selected")
        k8s = self.client(sel["context"])
        snap = discover.snapshot(k8s, sel["namespace"], name=sel["name"], target=self.target)
        return execute.execute(k8s, sel["namespace"], action_id, snap, params, name=sel["name"])

    def wal_count(self, from_wal: str) -> dict:
        sel = self._sel
        if not sel:
            return {}
        k8s = self.client(sel["context"])
        cluster = k8s.custom.get_namespaced_custom_object(
            discover.CNPG_GROUP, discover.CNPG_VERSION, sel["namespace"], "clusters", sel["name"])
        primary = cluster.get("status", {}).get("currentPrimary", "")
        return discover.wal_segments_since(k8s, sel["namespace"], from_wal, primary)

    def _current_image(self) -> str:
        sel = self._sel
        if not sel:
            return ""
        cluster = self.client(sel["context"]).custom.get_namespaced_custom_object(
            discover.CNPG_GROUP, discover.CNPG_VERSION, sel["namespace"], "clusters", sel["name"])
        return cluster.get("spec", {}).get("imageName", "")

    def image_tags(self, image: str = "") -> dict:
        """Release tags for a repo (the given image ref, or the cluster's current) —
        best-effort; empty tags => the modal falls back to free text."""
        from k8ostester_pg import registry
        image = image or self._current_image()
        return {"image": image, "current": discover.pg_version(image),
                "tags": registry.upgrade_tags(image)}

    def image_check(self, image: str) -> dict:
        """Whether an exact image ref is pullable — the modal's pull check."""
        from k8ostester_pg import registry
        return {"exists": registry.image_exists(image)}

    def deploy(self, opts: dict) -> dict:
        """Apply a Builder-generated manifest into the selected context/namespace
        via the dynamic client (no kubectl). Needs the broader 'lab' RBAC to create
        the objects; each doc is applied independently and its outcome reported."""
        import yaml
        from kubernetes.client import ApiException
        from kubernetes.dynamic import DynamicClient
        sel = self._sel or {}
        namespace = (opts.get("namespace") or sel.get("namespace") or "default").strip()
        k8s = self.client(sel.get("context"))
        dyn = DynamicClient(k8s._api_client)
        manifest = builder.build_manifest({**opts, "namespace": namespace})
        created, skipped, failed = [], [], []
        for doc in yaml.safe_load_all(manifest):
            if not doc:
                continue
            ident = f"{doc['kind']}/{doc.get('metadata', {}).get('name', '?')}"
            try:
                res = dyn.resources.get(api_version=doc["apiVersion"], kind=doc["kind"])
            except Exception:
                failed.append(f"{ident} — kind not installed")
                continue
            ns = doc.get("metadata", {}).get("namespace") or (namespace if res.namespaced else None)
            try:
                res.create(body=doc, namespace=ns)
                created.append(ident)
            except ApiException as e:
                if e.status == 409:
                    skipped.append(f"{ident} — exists")
                elif e.status == 403:
                    failed.append(f"{ident} — forbidden (needs deploy RBAC)")
                else:
                    failed.append(f"{ident} — {e.reason}")
        return {"namespace": namespace, "created": created, "skipped": skipped, "failed": failed}

    def secret(self, name: str) -> dict:
        """Decode a basic-auth secret's username/password — ON DEMAND only (never
        streamed in the snapshot), for the Connect sheet's copy-password button."""
        import base64
        sel = self._sel
        if not sel:
            return {}
        s = self.client(sel["context"]).core.read_namespaced_secret(name, sel["namespace"])
        data = s.data or {}

        def dec(k: str) -> str:
            return base64.b64decode(data[k]).decode() if data.get(k) else ""
        return {"username": dec("username"), "password": dec("password")}


def _handler(console: Console) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: dict) -> None:
            self._send(200, "application/json", json.dumps(obj).encode())

        def do_GET(self):
            if self.path == "/":
                self._send(200, "text/html; charset=utf-8", SPA.encode())
            elif self.path == "/api/contexts":
                self._json(console.contexts_info())
            elif self.path.startswith("/api/image-tags"):
                from urllib.parse import parse_qs, urlparse
                img = parse_qs(urlparse(self.path).query).get("image", [""])[0]
                try:
                    self._json({"ok": True, **console.image_tags(img)})
                except Exception as e:
                    self._json({"ok": False, "error": str(e).splitlines()[0][:200]})
            elif self.path.startswith("/api/image-check"):
                from urllib.parse import parse_qs, urlparse
                img = parse_qs(urlparse(self.path).query).get("image", [""])[0]
                try:
                    self._json({"ok": True, **console.image_check(img)})
                except Exception as e:
                    self._json({"ok": False, "error": str(e).splitlines()[0][:200]})
            elif self.path.startswith("/api/secret"):
                from urllib.parse import parse_qs, urlparse
                name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
                try:
                    self._json({"ok": True, **console.secret(name)})
                except Exception as e:
                    self._json({"ok": False, "error": str(e).splitlines()[0][:200]})
            elif self.path.startswith("/api/clusters"):
                from urllib.parse import parse_qs, urlparse
                ctx = parse_qs(urlparse(self.path).query).get("context", [None])[0]
                try:
                    self._json({"ok": True, "clusters": console.list_clusters(ctx)})
                except Exception as e:
                    self._json({"ok": False, "error": str(e).splitlines()[0][:200]})
            elif self.path == "/api/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    while True:
                        # read the shared cache — discovery happens once on the
                        # Console timer, not per connection
                        payload = console.state()
                        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                        self.wfile.flush()
                        time.sleep(console._interval)   # push at the fast-tier discovery rate
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self._send(404, "text/plain", b"not found")

        def do_POST(self):
            if self.path not in ("/api/action", "/api/manifest", "/api/dashboard",
                                 "/api/deploy", "/api/wal-count", "/api/select"):
                self._send(404, "text/plain", b"not found")
                return
            n = int(self.headers.get("Content-Length", 0) or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/manifest":
                try:
                    body.setdefault("namespace", (console._sel or {}).get("namespace", "default"))
                    self._json({"ok": True, "manifest": builder.build_manifest(body)})
                except Exception as e:
                    self._json({"ok": False, "error": str(e).splitlines()[0][:200]})
            elif self.path == "/api/dashboard":
                try:
                    self._json({"ok": True, "dashboard": dashboard.build_dashboard(body)})
                except Exception as e:
                    self._json({"ok": False, "error": str(e).splitlines()[0][:200]})
            elif self.path == "/api/deploy":
                try:
                    self._json({"ok": True, **console.deploy(body)})
                except Exception as e:
                    self._json({"ok": False, "error": str(e).splitlines()[0][:200]})
            elif self.path == "/api/select":
                try:
                    console.select(body.get("context"), body["namespace"], body["name"])
                    self._json({"ok": True})
                except Exception as e:
                    self._json({"ok": False, "error": str(e).splitlines()[0][:200]})
            elif self.path == "/api/wal-count":
                try:
                    self._json({"ok": True, **console.wal_count(body.get("from_wal", ""))})
                except Exception as e:
                    self._json({"ok": False, "error": str(e).splitlines()[0][:200]})
            else:  # /api/action
                try:
                    self._json({"ok": True, "message": console.act(
                        body.get("id", ""), body.get("params"))})
                except Exception as e:
                    self._json({"ok": False, "error": str(e).splitlines()[0][:200]})

    return H


def main() -> int:
    ap = argparse.ArgumentParser(description="remote-control console for CNPG clusters")
    ap.add_argument("--context", help="restrict the picker to one kube context")
    ap.add_argument("--namespace", default="", help="pre-select / scope to this namespace")
    ap.add_argument("--cluster", default="", help="pre-select this cluster (with --namespace)")
    ap.add_argument("--target", default="", help="a newer PG image/version to offer as an upgrade")
    ap.add_argument("--grafana", default="",
                    help="Grafana base URL — Operate then deep-links each cluster's dashboard")
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default localhost; a pod needs 0.0.0.0)")
    args = ap.parse_args()

    context, namespace = args.context, args.namespace
    # deployed in a pod: use the ServiceAccount and, by default, scope the picker
    # to (and bind for) the cluster it runs in
    in_pod = not context and running_in_cluster()
    if in_pod:
        context = IN_CLUSTER
        namespace = namespace or in_cluster_namespace()
    host = args.host if not in_pod else "0.0.0.0"   # reachable via the Service

    console = Console(context, namespace, args.cluster, args.target, args.grafana)
    server = ThreadingHTTPServer((host, args.port), _handler(console))
    where = f"{namespace}/{args.cluster}" if args.cluster else "pick a cluster in the UI"
    mode = "in-cluster ServiceAccount" if in_pod else "kubeconfig"
    print(f"console → http://{host}:{args.port}   ({mode}; {where})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
