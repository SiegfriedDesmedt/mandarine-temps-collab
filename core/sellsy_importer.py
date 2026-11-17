from __future__ import annotations

import logging
import os
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import openpyxl

from .sellsy_client import SellsyReadError, SellsyReadOnlyClient
from .sellsy_scope import SELLSY_DOCUMENT_START_ISO, is_sellsy_document_in_scope
ProgressCallback = Callable[[dict], None]
SOURCE_NAMES = ("orders", "invoices", "credit-notes")
DOCUMENT_PATHS = {"orders": "/orders", "invoices": "/invoices", "credit-notes": "/credit-notes"}
WORKBOOK_NAMES = {name: f"{name}.xlsx" for name in SOURCE_NAMES}
EXPERIENCE_CLIENT_ID_KEY = "SELLSY_EXPERIENCE_CLIENT_ID"
EXPERIENCE_CLIENT_SECRET_KEY = "SELLSY_EXPERIENCE_CLIENT_SECRET"
HEADERS = (
    "ID", "Numéro", "Date", "Client", "Objet", "Montant HT",
    "commercialmandarine", "bu-mbs", "cpmbs", "type-vente", "idplf", "project",
    "Client_id", "Statut", "Commande_ID", "Commande_Numero", "BL_Date",
)


class SellsyImportError(RuntimeError):
    def __init__(self, message: str, *, files: list[dict] | None = None):
        super().__init__(message)
        self.files = files or []


class SellsyConfigurationError(SellsyImportError):
    pass


class SellsyAuthenticationError(SellsyImportError):
    pass


def get_source_dir() -> Path:
    return (Path(__file__).resolve().parents[1] / "data" / "exports" / "sellsy").resolve()


def get_configuration() -> dict:
    missing = [name for name in ("SELLSY_CLIENT_ID", "SELLSY_CLIENT_SECRET") if not os.environ.get(name, "").strip()]
    if missing:
        raise SellsyConfigurationError("Configuration Sellsy API incomplète : " + ", ".join(missing))
    try:
        workers = int(os.environ.get("SELLSY_API_WORKERS", "4"))
    except ValueError as error:
        raise SellsyConfigurationError("SELLSY_API_WORKERS doit être un entier") from error
    experience_id = os.environ.get(EXPERIENCE_CLIENT_ID_KEY, "").strip()
    experience_secret = os.environ.get(EXPERIENCE_CLIENT_SECRET_KEY, "").strip()
    if bool(experience_id) != bool(experience_secret):
        missing_experience = (
            EXPERIENCE_CLIENT_SECRET_KEY if experience_id else EXPERIENCE_CLIENT_ID_KEY
        )
        raise SellsyConfigurationError(
            f"Configuration Sellsy Mandarine Expérience incomplète : {missing_experience}"
        )
    return {
        "auth_mode": "oauth2_client_credentials",
        "workers": min(max(workers, 1), 8),
        "experience_configured": bool(experience_id and experience_secret),
    }


def configuration_status() -> dict:
    try:
        config = get_configuration()
    except SellsyConfigurationError as error:
        return {"configured": False, "auth_mode": None, "error": str(error)}
    return {
        "configured": True,
        "auth_mode": config["auth_mode"],
        "experience_configured": config["experience_configured"],
        "error": None,
    }


def _configure_logger() -> logging.Logger:
    app_dir = Path(__file__).resolve().parents[1]
    log_dir = Path(os.environ.get("REPORTING_RUNTIME_DIR", app_dir / "runtime")).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "sellsy_import.log"
    logger = logging.getLogger("mandarine.sellsy_import")
    logger.setLevel(logging.INFO)
    if not any(isinstance(item, logging.FileHandler) and Path(item.baseFilename).resolve() == log_path for item in logger.handlers):
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def _close_file_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler):
            logger.removeHandler(handler)
            handler.close()


def _safe_message(error: Exception) -> str:
    message = str(error)
    for key in (
        "SELLSY_CLIENT_ID",
        "SELLSY_CLIENT_SECRET",
        EXPERIENCE_CLIENT_ID_KEY,
        EXPERIENCE_CLIENT_SECRET_KEY,
    ):
        secret = os.environ.get(key, "")
        if secret:
            message = message.replace(secret, "***")
    return message[:800]


def _relation_company_id(document: dict) -> str:
    for relation in document.get("related") or []:
        if isinstance(relation, dict) and relation.get("type") == "company":
            return str(relation.get("id") or "")
    company = (document.get("_embed") or {}).get("company") or {}
    return str(company.get("id") or "")


def _native_order(document: dict) -> tuple[str, str]:
    """Return the native Sellsy parent order, never a business-field guess."""
    candidates = [document.get("parent"), document.get("order")]
    embedded_order = (document.get("_embed") or {}).get("order")
    candidates.append(embedded_order)
    for relation in document.get("related") or []:
        if isinstance(relation, dict) and relation.get("type") == "order":
            candidates.append(relation)
    for candidate in candidates:
        if isinstance(candidate, (int, str)) and str(candidate).isdigit():
            return str(candidate), ""
        if not isinstance(candidate, dict):
            continue
        relation_type = str(candidate.get("type") or "order").casefold()
        if relation_type != "order":
            continue
        order_id = str(candidate.get("id") or candidate.get("document_id") or "")
        if order_id:
            return order_id, str(candidate.get("number") or "")
    return "", ""


def _parent(document: dict) -> tuple[str, str]:
    parent = document.get("parent")
    if not isinstance(parent, dict):
        return "", ""
    return str(parent.get("type") or "").casefold(), str(parent.get("id") or "")


def _load_delivery_order_links(client: SellsyReadOnlyClient, notify) -> dict[str, dict[str, str]]:
    """Load delivery -> order links in pages; avoids one API request per invoice."""
    links: dict[str, dict[str, str]] = {}
    page_number = 1
    total_pages = 1
    while page_number <= total_pages:
        payload = client.v1_read("Document.getList", {
            "doctype": "delivery",
            "pagination": {"nbperpage": 100, "pagenum": page_number},
            "search": {
                "periodecreationDate_start": int(
                    datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp()
                )
            },
        })
        infos = payload.get("infos") or {}
        total_pages = max(1, int(infos.get("nbpages") or 1))
        result = payload.get("result") or {}
        rows = result.values() if isinstance(result, dict) else result
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            delivery_id = str(row.get("docid") or row.get("id") or "")
            order_id = str(row.get("parentid") or "")
            if delivery_id and order_id:
                links[delivery_id] = {
                    "order_id": order_id,
                    "date": str(row.get("displayedDate") or row.get("date") or row.get("created") or "")[:10],
                }
        notify("sellsy:delivery-links", page_number, total_pages)
        page_number += 1
    return links


def _custom_value(field: dict):
    value = field.get("value")
    if field.get("type") == "select":
        for item in (field.get("parameters") or {}).get("items") or []:
            if str(item.get("id")) == str(value):
                return item.get("label") or ""
    if isinstance(value, dict):
        return value.get("amount") or value.get("label") or ""
    if isinstance(value, list):
        return " | ".join(str(item.get("label") if isinstance(item, dict) else item) for item in value)
    return value if value is not None else ""


def _list_documents(
    client: SellsyReadOnlyClient,
    source_name: str,
    *,
    limit: int = 100,
    updated_since: str | None = None,
) -> list[dict]:
    path = DOCUMENT_PATHS[source_name]
    rows: list[dict] = []
    retrieved_count = 0
    offset = None
    seen_offsets = set()
    while True:
        query = {"limit": min(max(limit, 1), 100), "order": "id", "direction": "asc", "embed[]": "company"}
        if offset:
            query["offset"] = offset
        filters = {"date": {"start": SELLSY_DOCUMENT_START_ISO}}
        if updated_since:
            filters["updated"] = {"start": updated_since}
        payload = client.search_endpoint(
            f"{path}/search",
            filters=filters,
            query=query,
        )
        page = payload.get("data") or []
        if not isinstance(page, list):
            raise SellsyImportError(f"Réponse Sellsy invalide pour {source_name}")
        retrieved_count += len(page)
        rows.extend(
            item
            for item in page
            if isinstance(item, dict) and is_sellsy_document_in_scope(item.get("date"))
        )
        pagination = payload.get("pagination") or {}
        next_offset = pagination.get("offset")
        total = int(pagination.get("total") or retrieved_count)
        if not page or retrieved_count >= total or not next_offset or next_offset in seen_offsets:
            break
        seen_offsets.add(next_offset)
        offset = next_offset
    return rows
def _load_custom_fields(client: SellsyReadOnlyClient, source_name: str, document_id) -> dict:
    payload = client.read_endpoint(
        f"{DOCUMENT_PATHS[source_name]}/{int(document_id)}/custom-fields",
        query={"limit": 100},
    )
    return {
        str(field.get("code") or "").casefold(): _custom_value(field)
        for field in payload.get("data") or []
        if isinstance(field, dict) and field.get("code")
    }


def _normalize(document: dict, custom_fields: dict) -> dict:
    amounts = document.get("amounts") or {}
    embedded_company = (document.get("_embed") or {}).get("company") or {}
    order_id, order_number = _native_order(document)
    return {
        "id": str(document.get("id") or ""), "number": str(document.get("number") or ""),
        "date": document.get("date"), "client_id": _relation_company_id(document),
        "client": document.get("company_name") or embedded_company.get("name") or "",
        "object": document.get("subject") or "",
        "amount": amounts.get("total_excl_tax") or amounts.get("total_after_discount_excl_tax") or 0,
        "commercial": custom_fields.get("commercialmandarine", ""),
        "bu": custom_fields.get("bu-mbs", ""), "owner": custom_fields.get("cpmbs", ""),
        "sale_type": custom_fields.get("type-vente") or document.get("sale_type") or "",
        "platform_id": custom_fields.get("idplf", ""), "project": custom_fields.get("project", ""),
        "status": document.get("status") or document.get("order_status") or "",
        "order_id": order_id, "order_number": order_number,
        "delivery_date": str(document.get("delivery_date") or "")[:10],
    }


def _experience_storage_id(document_id) -> str:
    raw_id = str(document_id or "").strip()
    if not raw_id.isdigit() or int(raw_id) <= 0:
        raise SellsyImportError(
            "Identifiant de commande Sellsy Mandarine Expérience invalide"
        )
    # Les identifiants Sellsy sont propres à chaque société. Le signe négatif
    # réserve un espace stable et sans collision à Mandarine Expérience.
    return str(-int(raw_id))


def _normalized_documents(
    client: SellsyReadOnlyClient,
    source_name: str,
    *,
    workers: int,
    notify,
    updated_since: str | None,
    stage_prefix: str = "sellsy",
    experience_namespace: bool = False,
) -> list[dict]:
    documents = _list_documents(client, source_name, updated_since=updated_since)
    custom_by_id: dict[str, dict] = {}
    total = len(documents)
    if source_name != "invoices":
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sellsy-read") as executor:
            futures = {
                executor.submit(
                    _load_custom_fields, client, source_name, row.get("id")
                ): str(row.get("id"))
                for row in documents
            }
            for processed, future in enumerate(as_completed(futures), start=1):
                custom_by_id[futures[future]] = future.result()
                if processed == 1 or processed % 50 == 0 or processed == total:
                    notify(f"{stage_prefix}:details:{source_name}", processed, total)
    if source_name == "invoices" and any(_parent(row)[0] == "delivery" for row in documents):
        delivery_order_links = _load_delivery_order_links(client, notify)
        for row in documents:
            parent_type, parent_id = _parent(row)
            link = delivery_order_links.get(parent_id) if parent_type == "delivery" else None
            if link:
                row["order"] = {"type": "order", "id": link["order_id"]}
                row["delivery_date"] = link["date"]
    normalized = [
        _normalize(row, custom_by_id.get(str(row.get("id")), {}))
        for row in documents
    ]
    if experience_namespace:
        for row in normalized:
            row["id"] = _experience_storage_id(row["id"])
    return normalized


def _write_workbook(path: Path, rows: list[dict]) -> None:
    workbook = openpyxl.Workbook(write_only=True)
    sheet = workbook.create_sheet("Données")
    sheet.append(list(HEADERS))
    for row in rows:
        sheet.append([
            row["id"], row["number"], row["date"], row["client"], row["object"], row["amount"],
            row["commercial"], row["bu"], row["owner"], row["sale_type"], row["platform_id"],
            row["project"], row["client_id"], row["status"],
            row["order_id"], row["order_number"],
            row["delivery_date"],
        ])
    workbook.save(path)


def _publish_directory(staging_dir: Path, source_dir: Path) -> None:
    backup = source_dir.parent / f".{source_dir.name}.backup-{uuid.uuid4().hex}"
    try:
        if source_dir.exists():
            source_dir.replace(backup)
        staging_dir.replace(source_dir)
    except Exception:
        if backup.exists():
            if source_dir.exists():
                shutil.rmtree(source_dir, ignore_errors=True)
            backup.replace(source_dir)
        raise
    else:
        shutil.rmtree(backup, ignore_errors=True)


def import_sellsy(
    progress_callback: ProgressCallback | None = None, *, destination_dir: Path | None = None,
    client: SellsyReadOnlyClient | None = None,
    experience_client: SellsyReadOnlyClient | None = None,
    updated_since: str | None = None,
) -> dict:
    """Read full or modified reporting documents; never call a write endpoint."""
    config = get_configuration()
    source_dir = Path(destination_dir).resolve() if destination_dir else get_source_dir()
    source_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = source_dir.parent / f".{source_dir.name}.staging-{uuid.uuid4().hex}"
    staging_dir.mkdir()
    api = client or SellsyReadOnlyClient.from_env()
    experience_api = experience_client
    if experience_api is None and config["experience_configured"]:
        experience_api = SellsyReadOnlyClient.from_env(
            EXPERIENCE_CLIENT_ID_KEY,
            EXPERIENCE_CLIENT_SECRET_KEY,
        )
    logger = _configure_logger()
    started_at = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    file_results = [{"source": name, "status": "pending", "file_name": WORKBOOK_NAMES[name], "size_bytes": None, "records": 0} for name in SOURCE_NAMES]

    def notify(stage: str, processed: int = 0, total: int = 0) -> None:
        if progress_callback:
            progress_callback({"stage": stage, "files": [dict(item) for item in file_results], "processed": processed, "total": total})

    try:
        for index, source_name in enumerate(SOURCE_NAMES):
            file_results[index]["status"] = "running"
            notify(f"sellsy:list:{source_name}")
            normalized = _normalized_documents(
                api,
                source_name,
                workers=config["workers"],
                notify=notify,
                updated_since=updated_since,
            )
            primary_records = len(normalized)
            experience_records = 0
            if source_name == "orders" and experience_api is not None:
                notify("sellsy-experience:list:orders")
                experience_orders = _normalized_documents(
                    experience_api,
                    "orders",
                    workers=config["workers"],
                    notify=notify,
                    updated_since=updated_since,
                    stage_prefix="sellsy-experience",
                    experience_namespace=True,
                )
                experience_records = len(experience_orders)
                normalized.extend(experience_orders)
            workbook_path = staging_dir / WORKBOOK_NAMES[source_name]
            _write_workbook(workbook_path, normalized)
            file_results[index] = {
                "source": source_name, "status": "succeeded", "file_name": WORKBOOK_NAMES[source_name],
                "size_bytes": workbook_path.stat().st_size, "records": len(normalized),
                "primary_records": primary_records,
                "experience_records": experience_records,
            }
            logger.info(
                "Source Sellsy %s extraite par API : %s enregistrements "
                "(%s principal, %s Mandarine Expérience)",
                source_name,
                len(normalized),
                primary_records,
                experience_records,
            )
        notify("sellsy:publication")
        _publish_directory(staging_dir, source_dir)
        return {
            "file_name": "Instantané Sellsy API (3 catégories)", "file_path": str(source_dir),
            "file_size_bytes": sum(int(item["size_bytes"] or 0) for item in file_results),
            "rows": sum(int(item["records"]) for item in file_results),
            "duration_seconds": round(time.monotonic() - started_monotonic, 2),
            "started_at": started_at.isoformat(), "completed_at": datetime.now(timezone.utc).isoformat(),
            "auth_mode": config["auth_mode"], "files": file_results,
            "experience_configured": config["experience_configured"],
            "sync_mode": "incremental" if updated_since else "full",
            "updated_since": updated_since,
        }
    except (SellsyConfigurationError, SellsyImportError):
        raise
    except (SellsyReadError, Exception) as error:
        wrapped = SellsyImportError(f"Extraction Sellsy impossible : {_safe_message(error)}", files=file_results)
        wrapped.duration_seconds = round(time.monotonic() - started_monotonic, 2)
        raise wrapped from error
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        _close_file_handlers(logger)
