"""`k8ost clean` — remove orphaned run namespaces.

A hard-killed run/session (closed terminal, `kill -9`) or a teardown that timed
out can leave a `k8ostester.io/run`-labeled namespace behind, which then trips
the concurrent-run guard. This sweeps them.
"""

from __future__ import annotations

import typer

from k8ostester.cli.app import app, console
from k8ostester.core.k8s import ClusterClient
from k8ostester.core.runner import RUN_LABEL


@app.command()
def clean(
    context: str = typer.Option(None, "--context", "-c", help="Kubeconfig context (default: current)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Delete without confirmation"),
) -> None:
    """Delete leftover k8ost run namespaces (labeled k8ostester.io/run)."""
    k8s = ClusterClient(context)
    namespaces = [
        ns.metadata.name
        for ns in k8s.core.list_namespace(label_selector=RUN_LABEL).items
        if ns.status.phase != "Terminating"  # already on their way out
    ]
    if not namespaces:
        console.print("[green]nothing to clean[/green] — no k8ost run namespaces found")
        return

    console.print(f"[bold]{len(namespaces)} k8ost run namespace(s):[/bold]")
    for name in namespaces:
        console.print(f"  {name}")
    if not yes and not typer.confirm("delete these namespaces?"):
        console.print("aborted")
        raise typer.Exit(1)

    for name in namespaces:
        k8s.delete_namespace(name, wait=False)  # fire the deletes; don't block
        console.print(f"[dim]deleting[/dim] {name}")
    console.print(f"[green]✔[/green] requested deletion of {len(namespaces)} namespace(s)")
