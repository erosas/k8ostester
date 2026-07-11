"""k8ost — validate Kubernetes configs against resilience and performance goals."""

from __future__ import annotations

import typer
from rich.console import Console

app = typer.Typer(no_args_is_help=True, help=__doc__)
env_app = typer.Typer(no_args_is_help=True, help="Inspect cluster environments.")
app.add_typer(env_app, name="env")

console = Console()