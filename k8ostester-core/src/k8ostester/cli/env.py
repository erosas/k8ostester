"""Cluster environment commands: kubeconfig contexts, capability probe."""

from __future__ import annotations

import typer
from rich.table import Table

from k8ostester.cli.app import console, env_app
from k8ostester.core import capabilities
from k8ostester.core.k8s import available_contexts


@env_app.command("contexts")
def env_contexts() -> None:
    """List kubeconfig contexts."""
    names, active = available_contexts()
    for name in names:
        marker = "*" if name == active else " "
        console.print(f" {marker} {name}")


@env_app.command("check")
def env_check(
    context: str = typer.Option(
        None, "--context", "-c", help="Kubeconfig context (default: current)"
    ),
) -> None:
    """Probe a cluster and report which experiment features it supports."""
    caps = capabilities.probe(context)

    console.print(
        f"\n[bold]Cluster:[/bold] {caps.context}  "
        f"[bold]Server:[/bold] {caps.server_version}\n"
    )

    nodes = Table(title="Nodes", title_justify="left")
    nodes.add_column("Name")
    nodes.add_column("Roles")
    nodes.add_column("Ready")
    nodes.add_column("Arch")
    nodes.add_column("Kubelet")
    nodes.add_column("Zone")
    for n in caps.nodes:
        nodes.add_row(
            n.name,
            ",".join(n.roles),
            "[green]yes[/green]" if n.ready else "[red]NO[/red]",
            n.arch,
            n.kubelet_version,
            n.zone or "[dim]—[/dim]",
        )
    console.print(nodes)

    storage = Table(title="Storage classes", title_justify="left")
    storage.add_column("Name")
    storage.add_column("Provisioner")
    storage.add_column("Default")
    for sc in caps.storage_classes:
        storage.add_row(sc.name, sc.provisioner, "yes" if sc.is_default else "")
    console.print(storage)

    operators = Table(title="Operators / stacks (by CRD)", title_justify="left")
    operators.add_column("Name")
    operators.add_column("Installed")
    for name, installed in caps.operators.items():
        operators.add_row(
            name, "[green]yes[/green]" if installed else "[dim]no[/dim]"
        )
    console.print(operators)

    def verdict(ok: bool, label: str, detail: str) -> None:
        icon = "[green]✔[/green]" if ok else "[yellow]✘[/yellow]"
        console.print(f" {icon} {label:<28} {detail}")

    console.print("\n[bold]Experiment capabilities[/bold]")
    verdict(
        caps.multi_node,
        "node-failure faults",
        f"{caps.worker_count} worker node(s)",
    )
    verdict(
        caps.network_policy_enforced,
        "network partition (native)",
        "CNI enforces NetworkPolicy"
        if caps.network_policy_enforced
        else "CNI does not enforce NetworkPolicy — partition needs params: {engine: chaos-mesh}",
    )
    verdict(
        caps.snapshots_supported,
        "volume snapshots",
        "snapshot CRDs + class present"
        if caps.snapshots_supported
        else "no snapshot CRDs/classes — use object-store backups (Barman+MinIO)",
    )
    verdict(
        caps.helm_version is not None,
        "helm",
        caps.helm_version or "not found on PATH",
    )
    verdict(
        caps.kubectl_version is not None,
        "kubectl",
        caps.kubectl_version or "not found on PATH",
    )
    verdict(
        caps.operators.get("cloudnative-pg", False),
        "cloudnative-pg operator",
        "installed" if caps.operators.get("cloudnative-pg") else "will be installed by postgres experiments",
    )
    verdict(
        caps.operators.get("prometheus-operator", False),
        "monitoring stack",
        "installed" if caps.operators.get("prometheus-operator") else "install via infra/monitoring (phase 4)",
    )
    console.print()