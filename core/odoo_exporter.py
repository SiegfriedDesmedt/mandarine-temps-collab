from __future__ import annotations

import csv
import logging
import os
import shutil
import socket
import ssl
import time
import uuid
import xmlrpc.client

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Callable


ODOO_URL = "https://erp.mandarine.academy"
ODOO_DB = "ODOO-prod2020"
BATCH_SIZE = 1000
ODOO_READ_METHODS = frozenset({"fields_get", "search_read", "read", "search_count", "search"})

ProgressCallback = Callable[[dict], None]


@dataclass(frozen=True)
class ExportSpec:
    filename: str
    model: str
    fields: tuple[str, ...]
    required_fields: frozenset[str]
    domain: tuple[tuple[str, str, object], ...] = ()


EXPORT_SPECS = (
    ExportSpec(
        "temps.csv",
        "account.analytic.line",
        (
            "id",
            "date",
            "name",
            "employee_id",
            "department_id",
            "project_id",
            "task_id",
            "so_line",
            "unit_amount",
            "amount",
            "create_date",
            "write_date",
        ),
        frozenset(
            {
                "id",
                "date",
                "name",
                "employee_id",
                "department_id",
                "project_id",
                "task_id",
                "so_line",
                "unit_amount",
                "amount",
                "create_date",
                "write_date",
            }
        ),
    ),
    ExportSpec(
        "projets.csv",
        "project.project",
        (
            "id",
            "name",
            "employee_id",
            "partner_id",
            "analytic_account_id",
            "sale_line_id",
            "sale_order_id",
            "date_start",
            "active",
            "company_id",
            "create_date",
            "write_date",
        ),
        frozenset({"id", "name"}),
    ),
    ExportSpec(
        "clients.csv",
        "res.partner",
        (
            "id",
            "name",
            "active",
            "company_id",
            "create_date",
            "write_date",
        ),
        frozenset({"id", "name", "company_type"}),
        (("company_type", "=", "company"),),
    ),
    ExportSpec(
        "commandes.csv",
        "sale.order",
        (
            "id",
            "name",
            "x_num_sellsy",
            "partner_id",
            "date_order",
            "state",
            "amount_total",
            "client_order_ref",
            "analytic_account_id",
            "project_id",
            "project_ids",
            "order_line",
            "company_id",
            "create_date",
            "write_date",
        ),
        frozenset({"id", "name", "x_num_sellsy", "partner_id", "date_order", "state"}),
        (("state", "=", "sale"),),
    ),
    ExportSpec(
        "lignes_commandes.csv",
        "sale.order.line",
        (
            "id",
            "order_id",
            "name",
            "product_id",
            "product_uom_qty",
            "product_uom",
            "price_unit",
            "discount",
            "price_subtotal",
            "price_tax",
            "price_total",
            "project_id",
            "task_id",
            "analytic_line_ids",
            "untaxed_amount_invoiced",
            "untaxed_amount_to_invoice",
            "state",
            "company_id",
            "create_date",
            "write_date",
        ),
        frozenset({"id", "order_id", "name", "price_subtotal", "project_id", "task_id"}),
    ),
    ExportSpec(
        "taches.csv",
        "project.task",
        (
            "id",
            "name",
            "project_id",
            "employee_id",
            "partner_id",
            "sale_line_id",
            "sale_order_id",
            "active",
            "planned_hours",
            "effective_hours",
            "remaining_hours",
            "progress",
            "date_deadline",
            "date_last_stage_update",
            "create_date",
            "write_date",
        ),
        frozenset({"id", "name", "project_id"}),
    ),
)

DERIVED_EMPLOYEE_MODELS = frozenset({"project.project", "project.task"})

COLLABORATOR_FILENAME = "collaborateurs.csv"
COLLABORATOR_FIELDS = (
    "employee_id",
    "employee_name",
    "user_id",
    "user_name",
    "department_id",
    "department_name",
    "active",
    "company_id",
    "create_date",
    "write_date",
)
FIXED_COST_PRODUCT_CODES = (
    "COUT-ACQ", "COUT-FRAIS", "COUT-INT", "COUT-MWIZ", "COUT-LIC", "COUT-EXT",
)
FIXED_COST_FILENAME = "couts_fixes.csv"
FIXED_COST_FIELDS = (
    "id",
    "date",
    "account_id",
    "project_id",
    "product_id",
    "product_default_code",
    "product_name",
    "unit_amount",
    "amount",
    "name",
    "tag_ids",
    "company_id",
    "create_date",
    "write_date",
)
EXPECTED_FILENAMES = frozenset(
    {spec.filename for spec in EXPORT_SPECS}
    | {COLLABORATOR_FILENAME, FIXED_COST_FILENAME}
)


class OdooExportError(RuntimeError):
    """Base error for a controlled Odoo export failure."""


class OdooAccessError(OdooExportError):
    """Raised when the XML-RPC user cannot read a required model or field."""


class OdooSchemaError(OdooExportError):
    """Raised when a required Odoo field is not available."""


def get_source_dir() -> Path:
    """Return the manual-export area inside the reporting data directory."""

    application_root = Path(__file__).resolve().parents[1]
    return (application_root / "data" / "exports" / "odoo").resolve()


def build_ssl_context() -> ssl.SSLContext:
    """Build a verified TLS context that also trusts the Windows stores."""

    context = ssl.create_default_context()
    if os.name != "nt" or not hasattr(ssl, "enum_certificates"):
        return context

    certificates: list[str] = []
    for store_name in ("ROOT", "CA"):
        try:
            store_certificates = ssl.enum_certificates(store_name)
        except OSError:
            continue
        for certificate, encoding, _trust in store_certificates:
            if encoding == "x509_asn":
                certificates.append(ssl.DER_cert_to_PEM_cert(certificate))

    if certificates:
        context.load_verify_locations(cadata="\n".join(certificates))
    return context


class TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    """HTTPS XML-RPC transport with a bounded socket timeout."""

    def __init__(self, timeout: float, context: ssl.SSLContext):
        super().__init__(context=context)
        self.timeout = timeout

    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = self.timeout
        return connection


def relation_identifier(value) -> int | str:
    """Return only the technical identifier from an Odoo relation."""

    if value is False or value is None or value == "":
        return ""
    if isinstance(value, (list, tuple)):
        if not value or value[0] is False or value[0] is None:
            return ""
        return value[0]
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return ""


def format_decimal_fr(value) -> str:
    """Format an Odoo numeric value with two decimals and a comma separator."""

    if value is False or value is None or value == "":
        return ""
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise OdooExportError(f"Valeur décimale Odoo invalide : {value!r}") from error
    if not decimal_value.is_finite():
        raise OdooExportError(f"Valeur décimale Odoo non finie : {value!r}")
    rounded = decimal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{rounded:.2f}".replace(".", ",")


def _configure_logger() -> logging.Logger:
    app_dir = Path(__file__).resolve().parents[1]
    log_dir = Path(os.environ.get("REPORTING_RUNTIME_DIR", app_dir / "runtime")).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = (log_dir / "odoo_export.log").resolve()
    logger = logging.getLogger("mandarine.odoo_export")
    logger.setLevel(logging.INFO)
    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).resolve() != log_path:
            logger.removeHandler(handler)
            handler.close()
    if not any(
        isinstance(handler, logging.FileHandler)
        and Path(handler.baseFilename).resolve() == log_path
        for handler in logger.handlers
    ):
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def _close_file_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler):
            logger.removeHandler(handler)
            handler.close()


def _looks_like_access_error(message: str) -> bool:
    normalized = message.casefold()
    return any(
        marker in normalized
        for marker in (
            "accesserror",
            "access denied",
            "access rights",
            "not allowed",
            "permission denied",
            "droits d'accès",
            "accès refusé",
        )
    )


def _execute_kw(models, uid, password, model: str, method: str, args: list, kwargs=None):
    if method not in ODOO_READ_METHODS:
        raise OdooAccessError(
            f"Méthode Odoo interdite par la politique de lecture seule : {model}.{method}"
        )
    try:
        return models.execute_kw(
            ODOO_DB,
            uid,
            password,
            model,
            method,
            args,
            kwargs or {},
        )
    except xmlrpc.client.Fault as error:
        detail = error.faultString or str(error)
        if _looks_like_access_error(detail):
            raise OdooAccessError(
                f"Droits d'accès insuffisants pour lire {model} ({method})"
            ) from error
        raise OdooExportError(f"Erreur Odoo sur {model}.{method} : {detail}") from error
    except xmlrpc.client.ProtocolError as error:
        if error.errcode in {401, 403}:
            raise OdooAccessError(
                f"Droits d'accès insuffisants pour lire {model} ({method})"
            ) from error
        raise OdooExportError(
            f"Erreur HTTP Odoo {error.errcode} sur {model}.{method}"
        ) from error


def _available_fields(models, uid, password, spec: ExportSpec, logger: logging.Logger) -> dict:
    available = _execute_kw(
        models,
        uid,
        password,
        spec.model,
        "fields_get",
        [],
        {"attributes": ["string", "type"]},
    )
    if not isinstance(available, dict):
        raise OdooSchemaError(f"Réponse fields_get invalide pour {spec.model}")

    missing_required = sorted(spec.required_fields - set(available))
    if missing_required:
        raise OdooSchemaError(
            f"Champs Odoo requis manquants sur {spec.model} : {', '.join(missing_required)}"
        )

    unavailable_optional = [
        field
        for field in spec.fields
        if field not in available
        and field not in spec.required_fields
        and not (
            field == "employee_id" and spec.model in DERIVED_EMPLOYEE_MODELS
        )
    ]
    if unavailable_optional:
        logger.warning(
            "%s : champs optionnels indisponibles : %s",
            spec.model,
            ", ".join(unavailable_optional),
        )
    return available


def _serialize_value(value, field_type: str | None) -> str | int:
    if field_type == "many2one":
        return relation_identifier(value)
    if field_type in {"many2many", "one2many"}:
        if not value:
            return ""
        return "|".join(str(identifier) for identifier in value)
    if field_type in {"float", "monetary"}:
        return format_decimal_fr(value)
    if field_type == "boolean":
        return "true" if bool(value) else "false"
    if value is False or value is None:
        return ""
    return value


def _format_date_fr(value) -> str:
    """Format an Odoo date/datetime as dd/mm/yyyy for the business export."""

    if value is False or value is None or value == "":
        return ""
    text = str(value)
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError as error:
        raise OdooExportError(f"Date Odoo invalide : {text}") from error


def _export_model(
    models,
    uid,
    password,
    spec: ExportSpec,
    staging_dir: Path,
    batch_size: int,
    logger: logging.Logger,
    notify_progress: ProgressCallback,
    prior_rows: int,
    employee_id_by_user: dict[int, int],
) -> dict:
    available = _available_fields(models, uid, password, spec, logger)
    requested_fields = [field for field in spec.fields if field in available]
    derives_employee_id = spec.model in DERIVED_EMPLOYEE_MODELS
    if derives_employee_id:
        if "user_id" not in available:
            raise OdooSchemaError(
                f"Champ Odoo requis pour la liaison employé manquant sur {spec.model} : user_id"
            )
        if "user_id" not in requested_fields:
            requested_fields.append("user_id")
    output_path = staging_dir / spec.filename
    total = 0
    last_id = 0

    with output_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(spec.fields), delimiter=";")
        writer.writeheader()

        while True:
            rows = _execute_kw(
                models,
                uid,
                password,
                spec.model,
                "search_read",
                [[["id", ">", last_id], *[list(condition) for condition in spec.domain]]],
                {
                    "fields": requested_fields,
                    "limit": batch_size,
                    "order": "id asc",
                    "context": {"active_test": False},
                },
            )
            if not isinstance(rows, list):
                raise OdooExportError(f"Réponse Odoo invalide pour {spec.model}")
            if not rows:
                break

            for row in rows:
                output_row = {}
                for field in spec.fields:
                    if field == "employee_id" and derives_employee_id:
                        user_id = relation_identifier(row.get("user_id"))
                        output_row[field] = (
                            employee_id_by_user.get(int(user_id), "") if user_id else ""
                        )
                    else:
                        output_row[field] = (
                            _serialize_value(
                                row.get(field), available.get(field, {}).get("type")
                            )
                            if field in available
                            else ""
                        )
                if spec.filename == "commandes.csv":
                    output_row["date_order"] = _format_date_fr(output_row["date_order"])
                writer.writerow(output_row)

            total += len(rows)
            last_id = int(rows[-1]["id"])
            notify_progress(
                {
                    "stage": f"extraction:{spec.filename}",
                    "rows": prior_rows + total,
                    "last_odoo_id": last_id,
                    "file_name": spec.filename,
                }
            )
            if len(rows) < batch_size:
                break

    logger.info("%s : %s lignes", spec.filename, total)
    return {
        "file_name": spec.filename,
        "model": spec.model,
        "rows": total,
        "last_id": last_id,
        "size_bytes": output_path.stat().st_size,
    }


def _read_model_rows(
    models,
    uid,
    password,
    model: str,
    fields: tuple[str, ...],
    required_fields: frozenset[str],
    batch_size: int,
) -> tuple[list[dict], dict]:
    available = _execute_kw(
        models,
        uid,
        password,
        model,
        "fields_get",
        [],
        {"attributes": ["string", "type"]},
    )
    if not isinstance(available, dict):
        raise OdooSchemaError(f"Réponse fields_get invalide pour {model}")
    missing = sorted(required_fields - set(available))
    if missing:
        raise OdooSchemaError(
            f"Champs Odoo requis manquants sur {model} : {', '.join(missing)}"
        )

    requested = [field for field in fields if field in available]
    rows: list[dict] = []
    last_id = 0
    while True:
        batch = _execute_kw(
            models,
            uid,
            password,
            model,
            "search_read",
            [[["id", ">", last_id]]],
            {
                "fields": requested,
                "limit": batch_size,
                "order": "id asc",
                "context": {"active_test": False},
            },
        )
        if not isinstance(batch, list):
            raise OdooExportError(f"Réponse Odoo invalide pour {model}")
        if not batch:
            break
        rows.extend(batch)
        last_id = int(batch[-1]["id"])
        if len(batch) < batch_size:
            break
    return rows, available


def _export_collaborators(
    models,
    uid,
    password,
    staging_dir: Path,
    batch_size: int,
    logger: logging.Logger,
    notify_progress: ProgressCallback,
    prior_rows: int,
) -> tuple[dict, dict[int, int]]:
    employee_fields = (
        "id", "name", "user_id", "department_id", "active", "company_id",
        "create_date", "write_date",
    )
    user_fields = ("id", "name")
    department_fields = ("id", "name")
    employees, employee_schema = _read_model_rows(
        models, uid, password, "hr.employee", employee_fields,
        frozenset({"id", "name", "user_id", "department_id"}), batch_size,
    )
    users, user_schema = _read_model_rows(
        models, uid, password, "res.users", user_fields,
        frozenset({"id", "name"}), batch_size,
    )
    departments, department_schema = _read_model_rows(
        models, uid, password, "hr.department", department_fields,
        frozenset({"id", "name"}), batch_size,
    )

    users_by_id = {int(row["id"]): row for row in users}
    departments_by_id = {int(row["id"]): row for row in departments}
    employees_by_user: dict[int, list[int]] = {}
    output_rows: list[dict] = []

    for employee in employees:
        user_id = relation_identifier(employee.get("user_id"))
        if user_id:
            employees_by_user.setdefault(int(user_id), []).append(int(employee["id"]))
        department_id = relation_identifier(employee.get("department_id"))
        user = users_by_id.get(int(user_id), {}) if user_id else {}
        department = departments_by_id.get(int(department_id), {}) if department_id else {}
        output_rows.append({
            "employee_id": employee["id"],
            "employee_name": employee.get("name") or "",
            "user_id": user_id,
            "user_name": user.get("name") or "",
            "department_id": department_id,
            "department_name": department.get("name") or "",
            "active": _serialize_value(employee.get("active"), employee_schema.get("active", {}).get("type")),
            "company_id": _serialize_value(employee.get("company_id"), employee_schema.get("company_id", {}).get("type")),
            "create_date": employee.get("create_date") or "",
            "write_date": employee.get("write_date") or "",
        })

    output_path = staging_dir / COLLABORATOR_FILENAME
    with output_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(COLLABORATOR_FIELDS), delimiter=";")
        writer.writeheader()
        writer.writerows(output_rows)

    notify_progress({
        "stage": f"extraction:{COLLABORATOR_FILENAME}",
        "rows": prior_rows + len(output_rows),
        "file_name": COLLABORATOR_FILENAME,
    })
    logger.info("%s : %s lignes", COLLABORATOR_FILENAME, len(output_rows))
    employee_id_by_user = {
        user_id: employee_ids[0]
        for user_id, employee_ids in employees_by_user.items()
        if len(employee_ids) == 1
    }
    ambiguous_users = sorted(
        user_id
        for user_id, employee_ids in employees_by_user.items()
        if len(employee_ids) > 1
    )
    if ambiguous_users:
        logger.warning(
            "Liaison employé ignorée pour %s utilisateurs rattachés à plusieurs employés",
            len(ambiguous_users),
        )

    result = {
        "file_name": COLLABORATOR_FILENAME,
        "model": "hr.employee (jointures res.users et hr.department)",
        "rows": len(output_rows),
        "last_id": max((int(row["id"]) for row in employees), default=0),
        "size_bytes": output_path.stat().st_size,
    }
    return result, employee_id_by_user


def _export_fixed_costs(
    models,
    uid,
    password,
    staging_dir: Path,
    batch_size: int,
    logger: logging.Logger,
    notify_progress: ProgressCallback,
    prior_rows: int,
) -> dict:
    """Export the three fixed-cost product families without altering Odoo."""

    products, _product_schema = _read_model_rows(
        models,
        uid,
        password,
        "product.product",
        ("id", "default_code", "name", "display_name"),
        frozenset({"id", "default_code", "name"}),
        batch_size,
    )
    products_by_id = {
        int(product["id"]): {
            "code": str(product.get("default_code") or "").strip().upper(),
            "name": product.get("name") or product.get("display_name") or "",
        }
        for product in products
        if str(product.get("default_code") or "").strip().upper()
        in FIXED_COST_PRODUCT_CODES
    }

    analytic_fields = (
        "id", "date", "account_id", "project_id", "product_id", "unit_amount",
        "amount", "name", "tag_ids", "company_id", "create_date", "write_date",
    )
    analytic_spec = ExportSpec(
        FIXED_COST_FILENAME,
        "account.analytic.line",
        analytic_fields,
        frozenset({"id", "date", "account_id", "product_id", "unit_amount", "amount"}),
    )
    available = _available_fields(models, uid, password, analytic_spec, logger)
    requested_fields = [field for field in analytic_fields if field in available]
    output_path = staging_dir / FIXED_COST_FILENAME
    total = 0
    last_id = 0

    with output_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(FIXED_COST_FIELDS), delimiter=";")
        writer.writeheader()
        if products_by_id:
            while True:
                rows = _execute_kw(
                    models,
                    uid,
                    password,
                    "account.analytic.line",
                    "search_read",
                    [[
                        ["id", ">", last_id],
                        ["product_id", "in", sorted(products_by_id)],
                    ]],
                    {
                        "fields": requested_fields,
                        "limit": batch_size,
                        "order": "id asc",
                        "context": {"active_test": False},
                    },
                )
                if not isinstance(rows, list):
                    raise OdooExportError("Réponse Odoo invalide pour account.analytic.line")
                if not rows:
                    break
                for row in rows:
                    product_id = relation_identifier(row.get("product_id"))
                    product = products_by_id.get(int(product_id), {}) if product_id else {}
                    output = {
                        field: _serialize_value(
                            row.get(field), available.get(field, {}).get("type")
                        )
                        for field in analytic_fields
                    }
                    output["product_default_code"] = product.get("code", "")
                    output["product_name"] = product.get("name", "")
                    writer.writerow(output)
                total += len(rows)
                last_id = int(rows[-1]["id"])
                notify_progress({
                    "stage": f"extraction:{FIXED_COST_FILENAME}",
                    "rows": prior_rows + total,
                    "last_odoo_id": last_id,
                    "file_name": FIXED_COST_FILENAME,
                })
                if len(rows) < batch_size:
                    break

    logger.info("%s : %s lignes", FIXED_COST_FILENAME, total)
    return {
        "file_name": FIXED_COST_FILENAME,
        "model": "account.analytic.line (six produits techniques COUT-*)",
        "rows": total,
        "last_id": last_id,
        "size_bytes": output_path.stat().st_size,
    }


def _verify_staged_exports(staging_dir: Path, results: list[dict]) -> None:
    result_names = {result["file_name"] for result in results}
    actual_names = {path.name for path in staging_dir.iterdir() if path.is_file()}
    if result_names != EXPECTED_FILENAMES or actual_names != EXPECTED_FILENAMES:
        raise OdooExportError("Les huit fichiers Odoo attendus n'ont pas tous été générés")

    expected_headers = {spec.filename: list(spec.fields) for spec in EXPORT_SPECS}
    expected_headers[COLLABORATOR_FILENAME] = list(COLLABORATOR_FIELDS)
    expected_headers[FIXED_COST_FILENAME] = list(FIXED_COST_FIELDS)
    for filename, expected_header in expected_headers.items():
        path = staging_dir / filename
        if path.read_bytes()[:3] != b"\xef\xbb\xbf":
            raise OdooExportError(f"BOM UTF-8 absent dans {filename}")
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            header = next(csv.reader(file, delimiter=";"), None)
        if header != expected_header:
            raise OdooExportError(f"En-tête CSV invalide dans {filename}")


def _publish_directory(staging_dir: Path, source_dir: Path, logger: logging.Logger) -> None:
    """Publish all eight files together and restore the previous set on failure."""

    backup_dir = source_dir.parent / f".{source_dir.name}.backup-{uuid.uuid4().hex}"
    previous_exists = source_dir.exists()
    try:
        if previous_exists:
            source_dir.replace(backup_dir)
        staging_dir.replace(source_dir)
    except PermissionError as error:
        if backup_dir.exists():
            if source_dir.exists():
                shutil.rmtree(source_dir)
            backup_dir.replace(source_dir)
        logger.warning(
            "Renommage atomique du dossier indisponible (%s) ; "
            "publication transactionnelle fichier par fichier",
            error,
        )
        _publish_files_with_rollback(staging_dir, source_dir)
    except Exception:
        if backup_dir.exists():
            if source_dir.exists():
                shutil.rmtree(source_dir)
            backup_dir.replace(source_dir)
        raise
    else:
        if backup_dir.exists():
            try:
                shutil.rmtree(backup_dir)
            except OSError as error:
                logger.warning("Ancienne publication non supprimée : %s", error)


def _publish_files_with_rollback(staging_dir: Path, source_dir: Path) -> None:
    """Windows fallback when an open directory handle prevents folder renaming."""

    source_dir.mkdir(parents=True, exist_ok=True)
    rollback_dir = source_dir.parent / f".{source_dir.name}.rollback-{uuid.uuid4().hex}"
    rollback_dir.mkdir()
    temporary_paths: list[Path] = []
    try:
        for filename in EXPECTED_FILENAMES:
            current_path = source_dir / filename
            if current_path.exists():
                shutil.copy2(current_path, rollback_dir / filename)

        for filename in sorted(EXPECTED_FILENAMES):
            temporary_path = source_dir / f".{filename}.{uuid.uuid4().hex}.tmp"
            temporary_paths.append(temporary_path)
            shutil.copy2(staging_dir / filename, temporary_path)
            os.replace(temporary_path, source_dir / filename)
    except Exception:
        for backup_path in rollback_dir.iterdir():
            restore_path = source_dir / f".{backup_path.name}.{uuid.uuid4().hex}.restore"
            shutil.copy2(backup_path, restore_path)
            os.replace(restore_path, source_dir / backup_path.name)
        raise
    finally:
        for temporary_path in temporary_paths:
            temporary_path.unlink(missing_ok=True)
        shutil.rmtree(rollback_dir, ignore_errors=True)


def export_odoo(
    progress_callback: ProgressCallback | None = None,
    *,
    destination_dir: Path | None = None,
) -> dict:
    """Export the eight reporting source files and publish them as one set."""

    login = os.environ.get("ODOO_LOGIN", "").strip()
    password = os.environ.get("ODOO_PASSWORD", "")
    if not login or not password:
        raise RuntimeError("Variables ODOO_LOGIN et ODOO_PASSWORD manquantes")

    batch_size = int(os.environ.get("ODOO_BATCH_SIZE", str(BATCH_SIZE)))
    timeout = float(os.environ.get("ODOO_TIMEOUT_SECONDS", "60"))
    if batch_size < 1 or batch_size > 10_000:
        raise RuntimeError("ODOO_BATCH_SIZE doit être compris entre 1 et 10000")

    source_dir = Path(destination_dir).resolve() if destination_dir else get_source_dir()
    source_dir.parent.mkdir(parents=True, exist_ok=True)
    source_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = source_dir.parent / f".{source_dir.name}.staging-{uuid.uuid4().hex}"
    staging_dir.mkdir()
    logger = _configure_logger()

    def notify_progress(update: dict) -> None:
        if not progress_callback:
            return
        try:
            progress_callback(update)
        except Exception as error:
            logger.warning("Mise à jour de progression ignorée : %s", error)

    start_monotonic = time.monotonic()
    started_at = datetime.now(timezone.utc)
    transport = TimeoutSafeTransport(timeout, build_ssl_context())

    try:
        notify_progress({"stage": "authentication", "rows": 0})
        try:
            common = xmlrpc.client.ServerProxy(
                f"{ODOO_URL}/xmlrpc/2/common",
                transport=transport,
                allow_none=True,
            )
            uid = common.authenticate(ODOO_DB, login, password, {})
        except (OSError, socket.timeout, xmlrpc.client.Error) as error:
            raise OdooExportError(f"Connexion Odoo impossible: {error}") from error
        if not uid:
            raise OdooAccessError("Authentification Odoo refusée")

        models = xmlrpc.client.ServerProxy(
            f"{ODOO_URL}/xmlrpc/2/object",
            transport=transport,
            allow_none=True,
        )

        notify_progress(
            {
                "stage": f"validation:{COLLABORATOR_FILENAME}",
                "rows": 0,
                "file_name": COLLABORATOR_FILENAME,
            }
        )
        collaborator_result, employee_id_by_user = _export_collaborators(
            models,
            uid,
            password,
            staging_dir,
            batch_size,
            logger,
            notify_progress,
            0,
        )

        results = []
        cumulative_rows = collaborator_result["rows"]
        for spec in EXPORT_SPECS:
            notify_progress(
                {
                    "stage": f"validation:{spec.filename}",
                    "rows": cumulative_rows,
                    "file_name": spec.filename,
                }
            )
            result = _export_model(
                models,
                uid,
                password,
                spec,
                staging_dir,
                batch_size,
                logger,
                notify_progress,
                cumulative_rows,
                employee_id_by_user,
            )
            results.append(result)
            cumulative_rows += result["rows"]

        notify_progress({
            "stage": f"validation:{FIXED_COST_FILENAME}",
            "rows": cumulative_rows,
            "file_name": FIXED_COST_FILENAME,
        })
        fixed_cost_result = _export_fixed_costs(
            models,
            uid,
            password,
            staging_dir,
            batch_size,
            logger,
            notify_progress,
            cumulative_rows,
        )
        results.append(fixed_cost_result)
        cumulative_rows += fixed_cost_result["rows"]

        results.append(collaborator_result)

        _verify_staged_exports(staging_dir, results)
        notify_progress({"stage": "publication", "rows": cumulative_rows})
        _publish_directory(staging_dir, source_dir, logger)

        duration = round(time.monotonic() - start_monotonic, 2)
        completed_at = datetime.now(timezone.utc)
        total_size = sum((source_dir / result["file_name"]).stat().st_size for result in results)
        logger.info(
            "Publication Odoo terminée : %s fichiers, %s lignes en %s secondes",
            len(results),
            cumulative_rows,
            duration,
        )
        notify_progress({"stage": "completed", "rows": cumulative_rows})

        times_result = next(result for result in results if result["file_name"] == "temps.csv")
        return {
            "file_name": "8 fichiers Odoo",
            "file_path": str(source_dir),
            "file_size_bytes": total_size,
            "rows": cumulative_rows,
            "last_odoo_id": times_result["last_id"],
            "duration_seconds": duration,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "files": results,
        }
    except Exception:
        logger.exception("Échec de l'export Odoo ; publication précédente conservée")
        raise
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        _close_file_handlers(logger)
