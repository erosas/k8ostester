"""The remote-control console server.

Holds the kubeconfig, discovers the CNPG cluster, streams the snapshot +
capability map to the browser over SSE, and executes gated actions. Stdlib
http.server — no framework, no new deps. See docs/remote-control.md.

    uv run python -m k8ostester_pg.server --context <ctx> --namespace <ns> [--target 16.6]
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

from k8ostester_pg import builder, discover, execute
from k8ostester_pg.control import CNPG_ACTIONS

SPA = (Path(__file__).parent / "console.html").read_text()


class Console:
    """The control plane: discovery + capability + execution over one cluster.

    Discovery runs on ONE shared background timer, not per SSE connection — so the
    load on the cluster (and the psql execs into the primary) is fixed regardless
    of how many browser tabs are watching. Connections just read the latest cache.
    """

    def __init__(self, context: str | None, namespace: str, target: str,
                 interval: float = 2.0, start: bool = True):
        self.k8s = ClusterClient(context)
        self.ns = namespace
        self.target = target
        self._interval = interval
        self._lock = threading.Lock()
        self._cache: dict = {"error": "warming up…"}
        if start:                                  # off in tests
            self.refresh()                         # warm synchronously
            threading.Thread(target=self._refresh_loop, daemon=True).start()

    def _safe_state(self) -> dict:
        try:
            snap = discover.snapshot(self.k8s, self.ns, target=self.target)
            return {"snapshot": snap, "capabilities": capabilities(CNPG_ACTIONS, snap)}
        except Exception as e:                     # surface discovery errors to the UI
            return {"error": str(e).splitlines()[0][:200]}

    def refresh(self) -> dict:
        """Recompute the cached state once (called on the timer and at startup)."""
        st = self._safe_state()
        with self._lock:
            self._cache = st
        return st

    def _refresh_loop(self) -> None:
        while True:
            self.refresh()
            time.sleep(self._interval)

    def state(self) -> dict:
        """The latest cached snapshot + capability map (refreshed on the timer)."""
        with self._lock:
            return self._cache

    def act(self, action_id: str, params: dict | None = None) -> str:
        snap = discover.snapshot(self.k8s, self.ns, target=self.target)
        return execute.execute(self.k8s, self.ns, action_id, snap, params)

    def wal_count(self, from_wal: str) -> dict:
        """Exact WAL segments from a base backup to the current WAL position."""
        cluster = self.k8s.custom.get_namespaced_custom_object(
            discover.CNPG_GROUP, discover.CNPG_VERSION, self.ns, "clusters", "pg")
        primary = cluster.get("status", {}).get("currentPrimary", "")
        return discover.wal_segments_since(self.k8s, self.ns, from_wal, primary)


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

        def do_GET(self):
            if self.path == "/":
                self._send(200, "text/html; charset=utf-8", SPA.encode())
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
            if self.path not in ("/api/action", "/api/manifest", "/api/wal-count"):
                self._send(404, "text/plain", b"not found")
                return
            n = int(self.headers.get("Content-Length", 0) or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/manifest":
                # pure YAML generation from the builder — no cluster access
                try:
                    body.setdefault("namespace", console.ns)
                    out = {"ok": True, "manifest": builder.build_manifest(body)}
                except Exception as e:
                    out = {"ok": False, "error": str(e).splitlines()[0][:200]}
                self._send(200, "application/json", json.dumps(out).encode())
                return
            if self.path == "/api/wal-count":
                # exact WAL segments from a base backup to now (queries the primary)
                try:
                    out = {"ok": True, **console.wal_count(body.get("from_wal", ""))}
                except Exception as e:
                    out = {"ok": False, "error": str(e).splitlines()[0][:200]}
                self._send(200, "application/json", json.dumps(out).encode())
                return
            try:
                msg = console.act(body.get("id", ""), body.get("params"))
                out = {"ok": True, "message": msg}
            except Exception as e:
                out = {"ok": False, "error": str(e).splitlines()[0][:200]}
            self._send(200, "application/json", json.dumps(out).encode())

    return H


def main() -> int:
    ap = argparse.ArgumentParser(description="remote-control console for a CNPG cluster")
    ap.add_argument("--context", help="kube context")
    ap.add_argument("--namespace", required=True, help="the cluster's namespace")
    ap.add_argument("--target", default="", help="a newer PG image/version to offer as an upgrade")
    ap.add_argument("--port", type=int, default=8700)
    args = ap.parse_args()

    console = Console(args.context, args.namespace, args.target)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), _handler(console))
    print(f"console → http://127.0.0.1:{args.port}   (namespace {args.namespace})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
