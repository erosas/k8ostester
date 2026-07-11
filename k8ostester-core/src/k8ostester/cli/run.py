"""Experiment commands: validate a directory, run it end-to-end."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.live import Live
from rich.prompt import IntPrompt
from rich.table import Table

from k8ostester.cli.app import app, console
from k8ostester.cli.live import LiveRunView
from k8ostester.core.experiment import load_experiment


def verdict_table(result) -> Table:
    """The end-of-run verdict: verification and goal outcomes."""
    table = Table(title="Verdict", title_justify="left")
    for col in ("", "Goal / check", "Value", "Threshold", "Detail"):
        table.add_column(col)
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
    return table


def find_experiments(root: Path = Path(".")) -> list[Path]:
    """Experiment directories (holding experiment.yaml) under root, skipping
    hidden trees and run artifacts."""
    skip = {"results", "node_modules", "__pycache__"}
    dirs = []
    for yaml_path in sorted(root.glob("**/experiment.yaml")):
        rel = yaml_path.relative_to(root)
        if any(part.startswith(".") or part in skip for part in rel.parts):
            continue
        dirs.append(yaml_path.parent)
    return dirs


def pick_experiment(root: Path = Path(".")) -> Path:
    """Interactive picker for `k8ost run` with no path."""
    dirs = find_experiments(root)
    if not dirs:
        console.print("[red]no experiments found[/red] — no experiment.yaml under this directory")
        raise typer.Exit(1)

    table = Table(title=f"{len(dirs)} experiment(s)", title_justify="left")
    for col in ("#", "Experiment", "Technology", "Load phases", "Faults", "Goals"):
        table.add_column(col)
    for i, d in enumerate(dirs, 1):
        try:
            spec = load_experiment(d)
            table.add_row(
                str(i), str(d), spec.technology,
                str(len(spec.load.phases) if spec.load else 0),
                str(len(spec.faults)), str(len(spec.goals)),
            )
        except Exception as e:
            table.add_row(str(i), str(d), f"[red]invalid: {e}[/red]", "", "", "")
    console.print(table)
    try:
        idx = IntPrompt.ask(
            "Run which experiment?",
            choices=[str(i) for i in range(1, len(dirs) + 1)],
            show_choices=False,
        )
    except EOFError:
        raise typer.Exit(1)
    return dirs[idx - 1]


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
    path: Path = typer.Argument(None, help="Experiment directory (omit to pick interactively)"),
    keep: bool = typer.Option(False, "--keep", help="Leave the namespace running after the run"),
    context: str = typer.Option(None, "--context", "-c", help="Override the experiment's kubeconfig context"),
    group: str = typer.Option(None, "--group", "-g", help="Record this run under a group for reporting"),
    allow_concurrent: bool = typer.Option(
        False, "--allow-concurrent",
        help="Run even if another experiment occupies the cluster (results may cross-contaminate)",
    ),
    plain: bool = typer.Option(False, "--plain", help="Line-by-line event output instead of the live view"),
    tui: bool = typer.Option(False, "--tui", help="Full-screen TUI with drill-in views (metrics, topology, events)"),
) -> None:
    """Run an experiment end-to-end."""
    from k8ostester.core.runner import Runner

    if path is None:
        path = pick_experiment()
    spec = load_experiment(path)

    if tui:
        from k8ostester.cli.tui import run_tui

        raise typer.Exit(run_tui(spec, keep=keep, context=context, group=group,
                                 allow_concurrent=allow_concurrent))

    live_view = LiveRunView(spec.name, spec.technology, context or spec.cluster.context) \
        if console.is_terminal and not plain else None

    def show(event: dict) -> None:
        console.print(f"[dim]{event['t_rel']:>8.1f}s[/dim]  [bold]{event['type']:<18}[/bold] {event['msg']}")

    runner = Runner(spec, keep=keep, context_override=context, group_override=group,
                    on_event=live_view.on_event if live_view else show,
                    allow_concurrent=allow_concurrent)
    try:
        if live_view:
            with Live(live_view, console=console, refresh_per_second=8):
                result = runner.run()
        else:
            result = runner.run()
    except Exception as e:
        console.print(f"\n[red]run error:[/red] {e}")
        raise typer.Exit(1)

    if result.goals or result.verifications:
        console.print(verdict_table(result))

    console.print(
        f"\n[bold]{'[green]PASSED[/green]' if result.status == 'passed' else '[red]' + result.status.upper() + '[/red]'}[/bold]"
        f"  results: {result.run_dir}"
    )
    if result.status != "passed":
        raise typer.Exit(2)