from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from core.source_database import is_database_file
from core.snapshot_store import active_source_files, active_source_provenance


SOURCE_DEFINITIONS: list[dict] = []


def _reporting_record_counts(app_dir: Path) -> dict[str, int]:
    data_path = app_dir / "dist" / "data.json"
    try:
        payload = json.loads(data_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {str(item.get("name", "")).casefold(): int(item.get("records", 0)) for item in payload.get("sources", [])}


def _describe_file(path: Path, definition: dict, now: datetime, records: int | None) -> dict:
    if not path.exists():
        return {**definition, "path": str(path), "exists": False, "status": "missing", "modified_at": None, "age_hours": None, "size_bytes": None, "records": records}
    modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    age_hours = max(0.0, (now - modified_at).total_seconds() / 3600)
    expected = float(definition["expected_hours"])
    status = "fresh" if age_hours <= expected else "warning" if age_hours <= expected * 2 else "stale"
    return {**definition, "path": str(path), "exists": True, "status": status, "modified_at": modified_at.isoformat(), "age_hours": round(age_hours, 1), "size_bytes": path.stat().st_size, "records": records}


def collect_source_freshness(_repo_root: Path, app_dir: Path) -> list[dict]:
    now = datetime.now(timezone.utc)
    counts = _reporting_record_counts(app_dir)
    paths = active_source_files()
    provenance = active_source_provenance()
    definitions = (
        ("odoo_clients_api", "Clients Odoo", "Odoo XML-RPC", "odoo_clients", "clients.csv", "CSV"),
        ("odoo_orders_api", "Commandes Odoo", "Odoo XML-RPC", "odoo_orders", "commandes.csv", "CSV"),
        ("odoo_order_lines_api", "Lignes de commandes Odoo", "Odoo XML-RPC", "odoo_order_lines", "lignes_commandes.csv", "CSV"),
        ("odoo_projects_api", "Projets Odoo", "Odoo XML-RPC", "odoo_projects", "projets.csv", "CSV"),
        ("odoo_tasks_api", "Tâches Odoo", "Odoo XML-RPC", "odoo_tasks", "taches.csv", "CSV"),
        ("odoo_timesheets_api", "Temps Odoo", "Odoo XML-RPC", "odoo_times", "temps.csv", "CSV"),
        ("odoo_fixed_costs_api", "Coûts fixes Odoo", "Odoo XML-RPC", "odoo_fixed_costs", "couts_fixes.csv", "CSV"),
        ("odoo_collaborators_api", "Collaborateurs Odoo", "Odoo XML-RPC", "odoo_collaborators", "collaborateurs.csv", "CSV"),
        ("sellsy_api_orders", "Commandes Sellsy", "Sellsy API V2", "sellsy_orders", "orders.xlsx", "XLSX"),
        ("sellsy_api_invoices", "Factures Sellsy", "Sellsy API V2", "sellsy_invoices", "invoices.xlsx", "XLSX"),
        ("sellsy_api_credit_notes", "Avoirs Sellsy", "Sellsy API V2", "sellsy_credit_notes", "credit-notes.xlsx", "XLSX"),
    )
    sources = []
    for source_id, name, system, key, filename, file_format in definitions:
        source_key = "sellsy" if system.startswith("Sellsy") else "odoo"
        source_provenance = provenance.get(source_key)
        location = f"Révision {source_provenance['revisionId']}"
        storage_path = paths[key]
        actual_format = "SQLite.gz" if storage_path.name.casefold().endswith(".sqlite.gz") else "SQLite" if is_database_file(storage_path) else file_format
        displayed_name = f"{storage_path.name}:{key}" if actual_format == "SQLite" else filename
        definition = {
            "id": source_id, "name": name, "system": system,
            "relative_path": f"{location}/{system.split()[0]}/{displayed_name}",
            "expected_hours": 48 if system.startswith("Sellsy") else 24,
            "format": actual_format,
        }
        sources.append(_describe_file(storage_path, definition, now, counts.get(name.casefold())))
    return sources
