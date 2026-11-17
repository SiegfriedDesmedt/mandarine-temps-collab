from __future__ import annotations

import csv
import os
import socket
import xmlrpc.client

from datetime import datetime, timezone
from typing import Callable

import openpyxl

from .odoo_exporter import (
    EXPORT_SPECS,
    ODOO_DB,
    ODOO_URL,
    TimeoutSafeTransport,
    _execute_kw,
    build_ssl_context,
)
from .sellsy_client import SellsyReadOnlyClient
from .sellsy_importer import (
    DOCUMENT_PATHS,
    EXPERIENCE_CLIENT_ID_KEY,
    EXPERIENCE_CLIENT_SECRET_KEY,
)
from .sellsy_scope import SELLSY_DOCUMENT_START_ISO
from .snapshot_store import SOURCE_FILE_NAMES, active_source_files
from .source_database import table_counts
from .source_revision_store import active_revision


ODOO_LOGICAL_BY_FILENAME = {
    "clients.csv": "odoo_clients",
    "commandes.csv": "odoo_orders",
    "lignes_commandes.csv": "odoo_order_lines",
    "projets.csv": "odoo_projects",
    "taches.csv": "odoo_tasks",
    "temps.csv": "odoo_times",
}
SELLSY_LOGICAL_BY_SOURCE = {
    "orders": "sellsy_orders",
    "invoices": "sellsy_invoices",
    "credit-notes": "sellsy_credit_notes",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local_counts(source: str) -> dict[str, int]:
    revision = active_revision(source)
    if not revision:
        return {}
    manifest = revision["manifestData"]
    stored = (manifest.get("storage") or {}).get("tables")
    if isinstance(stored, dict):
        return {str(key): int(value) for key, value in stored.items()}

    paths = active_source_files()
    keys = [key for key in SOURCE_FILE_NAMES if key.startswith(source + "_")]
    counts = {}
    for key in keys:
        path = paths[key]
        if path.suffix.casefold() == ".sqlite":
            return table_counts(path)
        if path.suffix.casefold() == ".csv":
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                counts[key] = sum(1 for _ in csv.DictReader(stream, delimiter=";"))
        else:
            workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
            try:
                rows = workbook.active.iter_rows(values_only=True)
                next(rows, None)
                counts[key] = sum(
                    1
                    for values in rows
                    if any(value not in (None, "") for value in values)
                )
            finally:
                workbook.close()
    return counts


def _local_state(source: str) -> dict:
    revision = active_revision(source)
    if not revision:
        return {
            "revisionId": None,
            "lastSuccessfulSync": None,
            "cursor": None,
            "counts": {},
            "storageFormat": None,
        }
    manifest = revision["manifestData"]
    return {
        "revisionId": revision["revisionId"],
        "lastSuccessfulSync": manifest.get("completedAt"),
        "cursor": (manifest.get("synchronization") or {}).get("cursor") or manifest.get("completedAt"),
        "counts": _local_counts(source),
        "storageFormat": (manifest.get("storage") or {}).get("format", "legacy-files"),
    }


def probe_odoo() -> dict:
    login = os.environ.get("ODOO_LOGIN", "").strip()
    password = os.environ.get("ODOO_PASSWORD", "")
    if not login or not password:
        raise RuntimeError("Configuration Odoo incomplète")
    timeout = float(os.environ.get("ODOO_TIMEOUT_SECONDS", "60"))
    transport = TimeoutSafeTransport(timeout, build_ssl_context())
    try:
        common = xmlrpc.client.ServerProxy(
            f"{ODOO_URL}/xmlrpc/2/common", transport=transport, allow_none=True
        )
        uid = common.authenticate(ODOO_DB, login, password, {})
        if not uid:
            raise RuntimeError("Authentification Odoo refusée")
        models = xmlrpc.client.ServerProxy(
            f"{ODOO_URL}/xmlrpc/2/object", transport=transport, allow_none=True
        )
        local = _local_state("odoo")
        counts = {}
        changes_since_local = {}
        latest_values = []
        for spec in EXPORT_SPECS:
            logical = ODOO_LOGICAL_BY_FILENAME[spec.filename]
            domain = [list(condition) for condition in spec.domain]
            counts[logical] = int(
                _execute_kw(
                    models,
                    uid,
                    password,
                    spec.model,
                    "search_count",
                    [domain],
                    {"context": {"active_test": False}},
                )
            )
            changes_since_local[logical] = None
            if local.get("cursor"):
                cursor = datetime.fromisoformat(
                    str(local["cursor"]).replace("Z", "+00:00")
                )
                if cursor.tzinfo is None:
                    cursor = cursor.replace(tzinfo=timezone.utc)
                changed_domain = [
                    *domain,
                    ["write_date", ">=", cursor.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")],
                ]
                changes_since_local[logical] = int(
                    _execute_kw(
                        models,
                        uid,
                        password,
                        spec.model,
                        "search_count",
                        [changed_domain],
                        {"context": {"active_test": False}},
                    )
                )
            latest = _execute_kw(
                models,
                uid,
                password,
                spec.model,
                "search_read",
                [domain],
                {
                    "fields": ["id", "write_date"],
                    "limit": 1,
                    "order": "write_date desc",
                    "context": {"active_test": False},
                },
            )
            if latest and latest[0].get("write_date"):
                latest_values.append(str(latest[0]["write_date"]))
        counts["odoo_collaborators"] = int(
            _execute_kw(
                models,
                uid,
                password,
                "hr.employee",
                "search_count",
                [[]],
                {"context": {"active_test": False}},
            )
        )
        return {
            "available": True,
            "counts": counts,
            "lastUpdated": max(latest_values, default=None),
            "verified": [
                "authentification",
                "volumes des six modèles Odoo principaux",
                "date write_date la plus récente",
            ],
            "limitations": [
                "les coûts fixes filtrés par produits ne sont pas recomptés",
                "aucune ligne métier n'est téléchargée pendant ce contrôle",
            ],
            "changesSinceLocal": changes_since_local,
        }
    except (OSError, socket.timeout, xmlrpc.client.Error) as error:
        raise RuntimeError(f"Connexion Odoo impossible : {error}") from error


def probe_sellsy(
    client: SellsyReadOnlyClient | None = None,
    experience_client: SellsyReadOnlyClient | None = None,
) -> dict:
    api = client or SellsyReadOnlyClient.from_env()
    experience_api = experience_client
    if experience_api is None:
        experience_id = os.environ.get(EXPERIENCE_CLIENT_ID_KEY, "").strip()
        experience_secret = os.environ.get(EXPERIENCE_CLIENT_SECRET_KEY, "").strip()
        if bool(experience_id) != bool(experience_secret):
            raise RuntimeError("Configuration Sellsy Mandarine Expérience incomplète")
        if experience_id and experience_secret:
            experience_api = SellsyReadOnlyClient.from_env(
                EXPERIENCE_CLIENT_ID_KEY,
                EXPERIENCE_CLIENT_SECRET_KEY,
            )
    local = _local_state("sellsy")
    counts = {}
    changes_since_local = {}
    latest_values = []
    for source_name, path in DOCUMENT_PATHS.items():
        base_filters = {"date": {"start": SELLSY_DOCUMENT_START_ISO}}
        payload = api.search_endpoint(
            f"{path}/search",
            filters=base_filters,
            query={"limit": 1, "order": "id", "direction": "desc"},
        )
        pagination = payload.get("pagination") or {}
        data = payload.get("data") or []
        logical = SELLSY_LOGICAL_BY_SOURCE[source_name]
        counts[logical] = int(pagination.get("total") or len(data))
        changes_since_local[logical] = None
        cursor = local.get("cursor") or local["lastSuccessfulSync"]
        if cursor:
            changed_payload = api.search_endpoint(
                f"{path}/search",
                filters={
                    **base_filters,
                    "updated": {"start": cursor},
                },
                query={"limit": 1, "order": "id", "direction": "desc"},
            )
            changed_data = changed_payload.get("data") or []
            changed_pagination = changed_payload.get("pagination") or {}
            changes_since_local[logical] = int(
                changed_pagination.get("total") or len(changed_data)
            )
            if changed_data:
                updated = changed_data[0].get("updated_at") or changed_data[0].get("updated")
                if updated:
                    latest_values.append(str(updated))
        if source_name == "orders" and experience_api is not None:
            experience_payload = experience_api.search_endpoint(
                "/orders/search",
                filters=base_filters,
                query={"limit": 1, "order": "id", "direction": "desc"},
            )
            experience_data = experience_payload.get("data") or []
            experience_pagination = experience_payload.get("pagination") or {}
            counts[logical] += int(
                experience_pagination.get("total") or len(experience_data)
            )
            if cursor:
                changed_experience_payload = experience_api.search_endpoint(
                    "/orders/search",
                    filters={**base_filters, "updated": {"start": cursor}},
                    query={"limit": 1, "order": "id", "direction": "desc"},
                )
                changed_experience_data = changed_experience_payload.get("data") or []
                changed_experience_pagination = (
                    changed_experience_payload.get("pagination") or {}
                )
                changes_since_local[logical] = int(changes_since_local[logical] or 0) + int(
                    changed_experience_pagination.get("total")
                    or len(changed_experience_data)
                )
                if changed_experience_data:
                    updated = (
                        changed_experience_data[0].get("updated_at")
                        or changed_experience_data[0].get("updated")
                    )
                    if updated:
                        latest_values.append(str(updated))
    return {
        "available": True,
        "counts": counts,
        "lastUpdated": max(latest_values, default=None),
        "verified": [
            "authentification OAuth2",
            "volumes commandes des deux sociétés, factures et avoirs depuis 2021",
            "date de modification la plus récente lorsqu'elle est exposée",
        ],
        "limitations": [
            "un échantillon d'un document par catégorie seulement",
            "les champs personnalisés ne sont pas relus pendant ce contrôle",
            "le second Sellsy ne contribue qu'aux commandes Mandarine Expérience",
        ],
        "changesSinceLocal": changes_since_local,
    }


def _compare(source: str, remote: dict) -> dict:
    local = _local_state(source)
    divergences = []
    for table, remote_count in remote.get("counts", {}).items():
        local_count = local["counts"].get(table)
        if local_count is not None and int(local_count) != int(remote_count):
            divergences.append(
                {"table": table, "local": int(local_count), "remote": int(remote_count)}
            )
    for table, changed_count in remote.get("changesSinceLocal", {}).items():
        if changed_count:
            divergences.append(
                {"table": table, "local": "synchronisé", "remote": f"{changed_count} modification(s)"}
            )
    return {
        "source": source,
        "status": "divergence" if divergences else "ok",
        "checkedAt": _utc_now(),
        "lastSuccessfulSync": local["lastSuccessfulSync"],
        "revisionId": local["revisionId"],
        "storageFormat": local["storageFormat"],
        "remoteLastUpdated": remote.get("lastUpdated"),
        "localCounts": local["counts"],
        "remoteCounts": remote.get("counts", {}),
        "divergences": divergences,
        "verified": remote.get("verified", []),
        "limitations": remote.get("limitations", []),
        "error": None,
    }


def run_synchronization_check(
    *,
    odoo_probe: Callable[[], dict] | None = None,
    sellsy_probe: Callable[[], dict] | None = None,
) -> dict:
    """Run bounded, read-only checks; source errors never expose credentials."""

    results = {}
    for source, probe in (
        ("odoo", odoo_probe or probe_odoo),
        ("sellsy", sellsy_probe or probe_sellsy),
    ):
        try:
            results[source] = _compare(source, probe())
        except Exception as error:
            local = _local_state(source)
            results[source] = {
                "source": source,
                "status": "error",
                "checkedAt": _utc_now(),
                "lastSuccessfulSync": local["lastSuccessfulSync"],
                "revisionId": local["revisionId"],
                "storageFormat": local["storageFormat"],
                "remoteLastUpdated": None,
                "localCounts": local["counts"],
                "remoteCounts": {},
                "divergences": [],
                "verified": [],
                "limitations": [],
                "error": str(error)[:800],
            }
    statuses = {item["status"] for item in results.values()}
    overall = "error" if statuses == {"error"} else "divergence" if "divergence" in statuses else "warning" if "error" in statuses else "ok"
    return {"status": overall, "checkedAt": _utc_now(), "sources": results}

