"""loadgen imports psycopg, which is deliberately not a project dependency
(the in-cluster Job pip-installs it; it is LGPL, so it stays out of our tree).
Stub it so the module imports in the test venv. setdefault keeps a real
install usable if one is ever present."""

import sys
from unittest.mock import MagicMock

sys.modules.setdefault("psycopg", MagicMock())
sys.modules.setdefault("psycopg.rows", MagicMock())