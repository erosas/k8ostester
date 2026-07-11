"""Experiment commands: validate a directory, run it end-to-end."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from k8ostester.cli.app import app, console
from k8ostester.core.experiment import load_experiment


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
def run(
    path: Path,
    keep: bool = typer.Option(False, "--keep", help="Leave the namespace running after the run"),
    context: str = typer.Option(None, "--context", "-c", help="Override the experiment's kubeconfig context"),
    group: str = typer.Option(None, "--group", "-g", help="Record this run under a group for reporting"),
    allow_concurrent: bool = typer.Option(
        False, "--allow-concurrent",
        help="Run even if another experiment occupies the cluster (results may cross-contaminate)",
    ),
) -> None:
    """Run an experiment end-to-end."""
    from k8ostester.core.runner import Runner

    spec = load_experiment(path)

    def show(event: dict) -> None:
        console.print(f"[dim]{event['t_rel']:>8.1f}s[/dim]  [bold]{event['type']:<18}[/bold] {event['msg']}")

    runner = Runner(spec, keep=keep, context_override=context, group_override=group, on_event=show,
                    allow_concurrent=allow_concurrent)
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