"""The k8ost CLI, split by command group.

`app.py` holds the bare typer application; the command modules register
themselves on import. The console entry point is `k8ostester.cli:app`.
"""

from k8ostester.cli import clean, env, report, run, session  # noqa: F401  (register commands)
from k8ostester.cli.app import app

__all__ = ["app"]