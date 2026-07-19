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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from k8ostester_kernel.control import capabilities
from k8ostester_kernel.k8s import ClusterClient

from k8ostester_pg import discover, execute
from k8ostester_pg.control import CNPG_ACTIONS

SPA = (Path(__file__).parent / "console.html").read_text()


class Console:
    """The control plane: discovery + capability + execution over one cluster."""

    def __init__(self, context: str | None, namespace: str, target: str):
        self.k8s = ClusterClient(context)
        self.ns = namespace
        self.target = target

    def state(self) -> dict:
        snap = discover.snapshot(self.k8s, self.ns, target=self.target)
        return {"snapshot": snap, "capabilities": capabilities(CNPG_ACTIONS, snap)}

    def act(self, action_id: str, params: dict | None = None) -> str:
        snap = discover.snapshot(self.k8s, self.ns, target=self.target)
        return execute.execute(self.k8s, self.ns, action_id, snap, params)


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
                        try:
                            payload = console.state()
                        except Exception as e:  # surface discovery errors to the UI
                            payload = {"error": str(e).splitlines()[0][:200]}
                        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                        self.wfile.flush()
                        time.sleep(2)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self._send(404, "text/plain", b"not found")

        def do_POST(self):
            if self.path != "/api/action":
                self._send(404, "text/plain", b"not found")
                return
            n = int(self.headers.get("Content-Length", 0) or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
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
