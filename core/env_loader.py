from __future__ import annotations

import json
import os
import re

from pathlib import Path


ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_value(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("valeur entre guillemets doubles invalide") from error
        if not isinstance(parsed, str):
            raise ValueError("la valeur doit être une chaîne de caractères")
        return parsed
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ValueError("valeur entre apostrophes non terminée")
        return value[1:-1]
    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()


def load_env_file(path: Path, *, override: bool = False) -> set[str]:
    """Load a small dotenv file without adding a third-party dependency.

    Existing process variables win by default. The returned names are safe to
    log because values are deliberately never returned.
    """

    path = Path(path)
    if not path.exists():
        return set()

    loaded: set[str] = set()
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"{path}:{line_number}: '=' manquant")

        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not ENV_KEY.fullmatch(key):
            raise ValueError(f"{path}:{line_number}: nom de variable invalide")

        value = _parse_value(raw_value)
        if override or key not in os.environ:
            os.environ[key] = value
            loaded.add(key)

    return loaded
