"""The remote-control console server.

Enumerates the kubeconfig's contexts, discovers the CNPG clusters in the selected
context, streams the selected cluster's snapshot + capability map over SSE, and
executes gated actions against it. Stdlib http.server — no framework, no new deps.
See docs/remote-control.md.

    uv run python -m k8ostester_pg.server            # all contexts, pick in the UI
    uv run python -m k8ostester_pg.server --context prod --namespace prod-east --cluster orders
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
from k8ostester_kernel.k8s import ClusterClient
from kubernetes import config

from k8ostester_pg import builder, discover, execute
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
                 cluster: str = "", target: str = "",
                 interval: float = 2.0, heavy_interval: float = 20.0,
                 start: bool = True):
        self.target = target
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

    # --- inventory + selection --------------------------------------------
    def contexts_info(self) -> dict:
        return {"contexts": self._contexts, "current": self._current,
                "selected": self._sel, "locked": bool(self._only_context)}

    def list_clusters(self, context: str | None) -> list[dict]:
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
                        time.sleep(2)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self._send(404, "text/plain", b"not found")

        def do_POST(self):
            if self.path not in ("/api/action", "/api/manifest", "/api/wal-count",
                                 "/api/select"):
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
    ap.add_argument("--port", type=int, default=8700)
    args = ap.parse_args()

    console = Console(args.context, args.namespace, args.cluster, args.target)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), _handler(console))
    where = f"{args.namespace}/{args.cluster}" if args.cluster else "pick a cluster in the UI"
    print(f"console → http://127.0.0.1:{args.port}   ({where})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
