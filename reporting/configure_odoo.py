from __future__ import annotations

import argparse
import getpass
import json
import os
import re

from pathlib import Path


ENV_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def save_env_values(path: Path, values: dict[str, str]) -> None:
    """Replace selected dotenv values atomically without printing secrets."""

    path = Path(path)
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    updated: list[str] = []
    seen: set[str] = set()

    for line in lines:
        match = ENV_ASSIGNMENT.match(line)
        key = match.group(1) if match else None
        if key in values:
            updated.append(f"{key}={json.dumps(values[key], ensure_ascii=False)}")
            seen.add(key)
        else:
            updated.append(line)

    missing = [key for key in values if key not in seen]
    if missing and updated and updated[-1]:
        updated.append("")
    updated.extend(f"{key}={json.dumps(values[key], ensure_ascii=False)}" for key in missing)

    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    os.chmod(temp_path, 0o600)
    temp_path.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enregistre localement les identifiants Odoo dans le fichier .env."
    )
    parser.add_argument(
        "--from-environment",
        action="store_true",
        help="utilise ODOO_LOGIN et ODOO_PASSWORD déjà présents dans l'environnement",
    )
    args = parser.parse_args()

    if args.from_environment:
        login = os.environ.get("ODOO_LOGIN", "").strip()
        password = os.environ.get("ODOO_PASSWORD", "")
    else:
        login = input("Identifiant Odoo : ").strip()
        password = getpass.getpass("Mot de passe Odoo (saisie masquée) : ")

    if not login or not password:
        raise SystemExit("Identifiant et mot de passe Odoo obligatoires.")

    env_path = Path(__file__).resolve().parent.parent / ".env"
    save_env_values(
        env_path,
        {"ODOO_LOGIN": login, "ODOO_PASSWORD": password},
    )
    print(f"Identifiants enregistrés dans {env_path} (valeurs non affichées).")


if __name__ == "__main__":
    main()
