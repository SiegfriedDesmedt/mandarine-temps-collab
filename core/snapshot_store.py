from __future__ import annotations

from pathlib import Path

from .source_database import existing_database_path
from .source_revision_store import active_revision, file_descriptor, load_active_sources


SOURCE_FILE_NAMES = {
    "odoo_clients": "clients.csv",
    "odoo_orders": "commandes.csv",
    "odoo_order_lines": "lignes_commandes.csv",
    "odoo_projects": "projets.csv",
    "odoo_tasks": "taches.csv",
    "odoo_times": "temps.csv",
    "odoo_fixed_costs": "couts_fixes.csv",
    "odoo_collaborators": "collaborateurs.csv",
    "sellsy_orders": "orders.xlsx",
    "sellsy_invoices": "invoices.xlsx",
    "sellsy_credit_notes": "credit-notes.xlsx",
}


def active_source_files() -> dict[str, Path]:
    """Return a compatible handle for every logical reporting source.

    New revisions point every logical table of a source to its SQLite database.
    Historical CSV/XLSX revisions keep their original per-file paths.
    """

    if not load_active_sources():
        raise RuntimeError("Le registre des sources actives est absent")

    odoo_revision = active_revision("odoo")
    sellsy_revision = active_revision("sellsy")
    if not odoo_revision or not sellsy_revision:
        raise RuntimeError("Les revisions actives Odoo et Sellsy sont requises")

    roots = {
        "odoo": Path(odoo_revision["path"]),
        "sellsy": Path(sellsy_revision["path"]),
    }
    paths = {}
    for key, filename in SOURCE_FILE_NAMES.items():
        source = "odoo" if key.startswith("odoo_") else "sellsy"
        root = roots[source]
        source_database = existing_database_path(root, source)
        paths[key] = source_database if source_database else root / filename
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise RuntimeError("Revisions actives incompletes : " + ", ".join(missing))
    return paths


def active_source_provenance() -> dict:
    """Describe the two immutable revisions currently used by Reporting."""

    if not load_active_sources():
        return {"mode": "missing-registry", "odoo": None, "sellsy": None}

    result = {"mode": "independent-revisions", "registrySchemaVersion": 2}
    for source in ("odoo", "sellsy"):
        revision = active_revision(source)
        if not revision:
            result[source] = None
            continue
        manifest = revision["manifestData"]
        result[source] = {
            "revisionId": revision["revisionId"],
            "activatedAt": revision.get("activatedAt"),
            "completedAt": manifest.get("completedAt"),
            "schemaVersion": manifest.get("schemaVersion"),
            "records": manifest.get("records", 0),
            "storageFormat": (manifest.get("storage") or {}).get(
                "format", "legacy-files"
            ),
            "syncMode": (manifest.get("synchronization") or {}).get("mode", "full"),
            "cursor": (manifest.get("synchronization") or {}).get("cursor"),
            "manifestSha256": file_descriptor(
                Path(revision["path"]) / "manifest.json"
            )["sha256"],
        }
    return result
