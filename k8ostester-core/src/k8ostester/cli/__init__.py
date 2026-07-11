"""The k8ost CLI, split by command group.

`app.py` holds the bare typer application; the command modules register
themselves on import. The console entry point is `k8ostester.cli:app`.
"""

from k8ostester.cli.app import app
from k8ostester.cli import env, report, run  # noqa: F401  (register commands)

__all__ = ["app"]