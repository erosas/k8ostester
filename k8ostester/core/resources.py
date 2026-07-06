"""Resource templates: YAML/JSON files with ${VAR} substitution.

Convention (same as apply_manifests): scalar placeholders are written quoted
('${X}') in the template; structured values substitute a JSON string into an
unquoted ${X} — JSON is a YAML subset, so it parses into the right shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_resource(path: Path, variables: dict[str, str] | None = None) -> Any:
    text = path.read_text()
    for key, value in (variables or {}).items():
        text = text.replace("${" + key + "}", value)
    return yaml.safe_load(text)
