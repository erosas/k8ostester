"""Recorded-results commands: list runs, render comparison reports."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from k8ostester.cli.app import app, console


@app.command()
def report(
    runs: list[Path] = typer.Argument(None, help="Run directories to compare"),
    group: str = typer.Option(None, "--group", "-g", help="Include every run recorded with this group"),
    all_runs: bool = typer.Option(False, "--all", help="Include every recorded run"),
    latest: bool = typer.Option(False, "--latest", help="One row per experiment: its most recent passed/failed run"),
    out: Path = typer.Option(Path("results/report.html"), "--out", "-o"),
    title: str = typer.Option(None, "--title"),
    open_browser: bool = typer.Option(False, "--open", help="Open the report in the browser"),
) -> None:
    """Render a self-contained HTML report comparing runs (graphs + goal matrix)."""
    import webbrowser

    from k8ostester.core import report as report_mod

    dirs = list(runs or [])
    if group:  # auto-reduce to the latest verdict per experiment in the group
        dirs += [d for d in report_mod.find_latest_runs(group=group) if d not in dirs]
    if all_runs:
        dirs += [d for d in report_mod.find_all_runs() if d not in dirs]
    if latest:
        dirs += [d for d in report_mod.find_latest_runs() if d not in dirs]
    if not dirs:
        console.print("[red]no runs selected[/red] — pass run directories, --group, --all or --latest")
        raise typer.Exit(1)
    default_title = "all experiments" if latest else ("all runs" if all_runs else "K8osTester comparison")
    data = [report_mod.gather_run(d) for d in dirs]
    path = report_mod.render(data, title or group or default_title, out)
    console.print(f"[green]✔[/green] report with {len(data)} run(s): {path}")
    if open_browser:
        webbrowser.open(path.resolve().as_uri())


@app.command()
def runs(
    results: Path = typer.Option(Path("results"), "--results"),
) -> None:
    """List recorded runs (experiment, group, status) newest first."""
    import json

    rows = []
    for summary_path in results.glob("*/*/summary.json"):
        try:
            rows.append(json.loads(summary_path.read_text()))
        except json.JSONDecodeError:
            continue
    if not rows:
        console.print("no runs recorded")
        return
    rows.sort(key=lambda s: s["run_id"], reverse=True)
    table = Table(title=f"{len(rows)} run(s)", title_justify="left")
    for col in ("Run", "Experiment", "Group", "Status", "Duration"):
        table.add_column(col)
    for s in rows:
        status = s.get("status", "?")
        color = "green" if status == "passed" else "red"
        table.add_row(
            s["run_id"], s["experiment"], s.get("group") or "—",
            f"[{color}]{status}[/{color}]", f"{s.get('duration_s', 0):.0f}s",
        )
    console.print(table)