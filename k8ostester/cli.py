"""k8ost — validate Kubernetes configs against resilience and performance goals."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from k8ostester.core import capabilities
from k8ostester.core.experiment import load_experiment
from k8ostester.core.k8s import available_contexts

app = typer.Typer(no_args_is_help=True, help=__doc__)
env_app = typer.Typer(no_args_is_help=True, help="Inspect cluster environments.")
app.add_typer(env_app, name="env")

console = Console()


@app.command()
def validate(path: Path) -> None:
    """Parse and validate an experiment directory without touching a cluster."""
    spec = load_experiment(path)
    console.print(f"[green]✔[/green] {spec.name} ({spec.technology}) is valid")
    console.print(
        f"   manifests: {spec.manifests_dir}\n"
        f"   load phases: {len(spec.load.phases) if spec.load else 0}, "
        f"faults: {len(spec.faults)}, goals: {len(spec.goals)}"
    )


@app.command()
def report(
    runs: list[Path] = typer.Argument(None, help="Run directories to compare"),
    group: str = typer.Option(None, "--group", "-g", help="Include every run recorded with this group"),
    out: Path = typer.Option(Path("results/report.html"), "--out", "-o"),
    title: str = typer.Option(None, "--title"),
) -> None:
    """Render a self-contained HTML report comparing runs (graphs + goal matrix)."""
    from k8ostester.core import report as report_mod

    dirs = list(runs or [])
    if group:
        dirs += [d for d in report_mod.find_group_runs(group) if d not in dirs]
    if not dirs:
        console.print("[red]no runs selected[/red] — pass run directories or --group")
        raise typer.Exit(1)
    data = [report_mod.gather_run(d) for d in dirs]
    path = report_mod.render(data, title or group or "K8osTester comparison", out)
    console.print(f"[green]✔[/green] report with {len(data)} run(s): {path}")


@app.command()
def run(
    path: Path,
    keep: bool = typer.Option(False, "--keep", help="Leave the namespace running after the run"),
    context: str = typer.Option(None, "--context", "-c", help="Override the experiment's kubeconfig context"),
    group: str = typer.Option(None, "--group", "-g", help="Record this run under a group for reporting"),
) -> None:
    """Run an experiment end-to-end."""
    from k8ostester.core.runner import Runner

    spec = load_experiment(path)

    def show(event: dict) -> None:
        console.print(f"[dim]{event['t_rel']:>8.1f}s[/dim]  [bold]{event['type']:<18}[/bold] {event['msg']}")

    runner = Runner(spec, keep=keep, context_override=context, group_override=group, on_event=show)
    try:
        result = runner.run()
    except Exception as e:
        console.print(f"\n[red]run error:[/red] {e}")
        raise typer.Exit(1)

    if result.goals or result.verifications:
        table = Table(title="Verdict", title_justify="left")
        table.add_column("")
        table.add_column("Goal / check")
        table.add_column("Value")
        table.add_column("Threshold")
        table.add_column("Detail")
        for v in result.verifications:
            table.add_row(
                "[green]✔[/green]" if v["passed"] else "[red]✘[/red]",
                f"verify:{v['check']}", "", "", v["detail"],
            )
        for g in result.goals:
            table.add_row(
                "[green]✔[/green]" if g["passed"] else "[red]✘[/red]",
                g["goal"], str(g["value"]), str(g["threshold"]), g["detail"],
            )
        console.print(table)

    console.print(
        f"\n[bold]{'[green]PASSED[/green]' if result.status == 'passed' else '[red]' + result.status.upper() + '[/red]'}[/bold]"
        f"  results: {result.run_dir}"
    )
    if result.status != "passed":
        raise typer.Exit(2)


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
    for n in caps.nodes:
        nodes.add_row(
            n.name,
            ",".join(n.roles),
            "[green]yes[/green]" if n.ready else "[red]NO[/red]",
            n.arch,
            n.kubelet_version,
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


if __name__ == "__main__":
    app()
