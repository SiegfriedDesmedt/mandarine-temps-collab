from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import math
import os
import re
import time
import unicodedata
import uuid

from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import openpyxl

from core.sellsy_scope import SELLSY_DOCUMENT_START_ISO, is_sellsy_document_in_scope
from core.snapshot_store import SOURCE_FILE_NAMES, active_source_files, active_source_provenance


APP_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_DIR.parent
OUTPUT_PATH = APP_DIR / "dist" / "data.json"

SOURCE_FILES = {key: Path(filename) for key, filename in SOURCE_FILE_NAMES.items()}


def _resolved_source_files() -> dict[str, Path]:
    return active_source_files()

TEXT_REPAIRS = {
    "Soci�t�": "Société", "soci�t�": "société", "Cr�ation": "Création",
    "R�gion": "Région", "r�gion": "région", "D�partement": "Département",
    "d�partement": "département", "D�veloppeur": "Développeur",
    "R�alisateur": "Réalisateur", "Op�rationnel": "Opérationnel",
    "G�n�rale": "Générale", "g�n�rique": "générique", "G�n�rique": "Générique",
    "Co�ts": "Coûts", "Co�t": "Coût", "Cong�s": "Congés",
    "justifi�es": "justifiées", "Mat�riaux": "Matériaux", "C�tes": "Côtes",
    "Num�ro": "Numéro", "N�": "N°", "Fr�d�ric": "Frédéric",
    "H�l�ne": "Hélène", "St�phanie": "Stéphanie", "J�r�my": "Jérémy",
    "Lo�c": "Loïc", "Beno�t": "Benoît", "S�ch�": "Séché",
    "M�tropole": "Métropole", "Sant�": "Santé", "Cybers�curit�": "Cybersécurité",
    "Am�lioration": "Amélioration",
}

FIXED_COST_CATEGORIES = {
    "COUT-ACQ": "Acquisition",
    "COUT-FRAIS": "Frais et déplacements",
    "COUT-INT": "Coûts internes",
    "COUT-MWIZ": "Licences MigrationWiz",
    "COUT-LIC": "Licences",
    "COUT-EXT": "Ressources externes",
}

# Technical IDs are deliberately scoped to the configured production database.
# ODOO_PSEUDO_EMPLOYEE_IDS can add comma-separated IDs without changing code.
PSEUDO_EMPLOYEE_IDS_BY_DATABASE = {
    "ODOO-prod2020": frozenset({"373", "388"}),
}
PSEUDO_EMPLOYEE_MARKERS = (
    "coutacquisition",
    "coutsinterne",
    "coutsfixe",
    "fraisdeplacement",
    "ressourceexterne",
    "licence",
)

# Interim global rule approved for the monthly timesheet control. Individual
# contracts and part-time schedules are deliberately out of scope until the
# Odoo source model exposes reliable employment data.
TIME_CONTROL_DAILY_HOURS = 7 + (40 / 60)
TIME_CONTROL_CONFORM_THRESHOLD = 95.0
TIME_CONTROL_WATCH_THRESHOLD = 85.0
TIME_CONTROL_OVERAGE_WATCH_THRESHOLD = 108.0
TIME_CONTROL_FIRST_FISCAL_START = date(2021, 7, 1)
TIME_CONTROL_ABSENCE_MARKERS = (
    "absence",
    "conge",
    "rtt",
    "ferie",
)


class ReportBuildError(RuntimeError):
    pass


def _runtime_dir() -> Path:
    return Path(os.environ.get("REPORTING_RUNTIME_DIR", APP_DIR / "runtime")).resolve()


def _process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextmanager
def report_build_lock(*, timeout_seconds: float = 0.0):
    """Prevent two processes from rebuilding the report at the same time."""

    runtime_dir = _runtime_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock_path = runtime_dir / "report-rebuild.lock"
    token = uuid.uuid4().hex
    deadline = time.monotonic() + max(0.0, timeout_seconds)

    while True:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                state = json.loads(lock_path.read_text(encoding="utf-8"))
                owner_pid = int(state.get("pid", 0))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                try:
                    lock_age = time.time() - lock_path.stat().st_mtime
                except OSError:
                    continue
                if lock_age < 5:
                    raise ReportBuildError(
                        "Une reconstruction est deja en cours d'initialisation."
                    )
                owner_pid = 0

            if not _process_is_running(owner_pid):
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise ReportBuildError(
                        f"Impossible de nettoyer le verrou de reconstruction : {error}"
                    ) from error
                continue

            if time.monotonic() >= deadline:
                raise ReportBuildError(
                    f"Une reconstruction est d\u00e9j\u00e0 en cours (processus {owner_pid})."
                )
            time.sleep(0.1)
            continue

        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                json.dump({"pid": os.getpid(), "token": token}, target)
            break
        except Exception:
            lock_path.unlink(missing_ok=True)
            raise

    try:
        yield lock_path
    finally:
        try:
            state = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        if state.get("token") == token:
            lock_path.unlink(missing_ok=True)


def source_fingerprints(source_files: dict[str, Path] | None = None) -> dict[str, dict[str, int | str]]:
    """Return content fingerprints without ever modifying a source file."""

    fingerprints = {}
    for source_id, path in (source_files or _resolved_source_files()).items():
        if not path.is_file():
            raise ReportBuildError(f"Source requise absente : {path.relative_to(PROJECT_ROOT)}")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        stat = path.stat()
        fingerprints[source_id] = {
            "path": str(path.resolve()),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": digest.hexdigest(),
        }
    return fingerprints


def clean_text(value, fallback: str = "Non renseigné") -> str:
    text = str(value or "").strip()
    for broken, repaired in TEXT_REPAIRS.items():
        text = text.replace(broken, repaired)
    text = text.replace("�", "é")
    return text or fallback


def as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(value or "").strip(), pattern).date()
        except ValueError:
            continue
    return None


def as_float(value) -> float:
    if value in (None, "", False, "False"):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\u00a0", "").replace(" ", "")
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    try:
        return float(re.sub(r"[^0-9.\-]", "", text))
    except ValueError:
        return 0.0


def round_metric(value: float, digits: int = 1) -> float:
    return round(value, digits)


def month_key(day: date) -> str:
    return day.strftime("%Y-%m")


def month_range(start: date, end: date) -> list[str]:
    values = []
    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    while cursor <= last:
        values.append(month_key(cursor))
        cursor = date(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)
    return values


def fiscal_period(as_of: date) -> tuple[date, date]:
    year = as_of.year if as_of.month >= 7 else as_of.year - 1
    return date(year, 7, 1), date(year + 1, 6, 30)


def month_bounds(month: str) -> tuple[date, date]:
    year, month_number = (int(value) for value in month.split("-", 1))
    start = date(year, month_number, 1)
    next_month = date(year + (month_number == 12), 1 if month_number == 12 else month_number + 1, 1)
    return start, date.fromordinal(next_month.toordinal() - 1)


def working_day_count(start: date, end: date) -> int:
    if end < start:
        return 0
    return sum(
        date.fromordinal(ordinal).weekday() < 5
        for ordinal in range(start.toordinal(), end.toordinal() + 1)
    )


def monthly_time_control(
    month: str,
    hours: float,
    *,
    as_of: date,
    first_activity: date | None,
    last_activity: date | None,
    active: bool,
) -> dict:
    start, end = month_bounds(month)
    if start > as_of:
        return {
            "expectedHours": None,
            "coverageRate": None,
            "missingHours": None,
            "status": "unevaluated",
            "evaluation": "future",
        }

    evaluation_start = max(start, first_activity) if first_activity else start
    evaluation_end = min(end, as_of)
    if not active and last_activity:
        evaluation_end = min(evaluation_end, last_activity)
    if evaluation_end < evaluation_start:
        return {
            "expectedHours": None,
            "coverageRate": None,
            "missingHours": None,
            "status": "unevaluated",
            "evaluation": "outside-presence",
        }

    expected = working_day_count(evaluation_start, evaluation_end) * TIME_CONTROL_DAILY_HOURS
    if expected <= 0:
        return {
            "expectedHours": 0.0,
            "coverageRate": None,
            "missingHours": None,
            "status": "unevaluated",
            "evaluation": "no-capacity",
        }

    coverage = hours / expected * 100
    if coverage < TIME_CONTROL_WATCH_THRESHOLD:
        status = "insufficient"
    elif coverage < TIME_CONTROL_CONFORM_THRESHOLD:
        status = "watch"
    elif coverage > TIME_CONTROL_OVERAGE_WATCH_THRESHOLD:
        status = "watch"
    else:
        status = "conform"
    return {
        "expectedHours": round_metric(expected),
        "coverageRate": round_metric(coverage),
        "missingHours": round_metric(max(expected - hours, 0.0)),
        "status": status,
        "evaluation": "elapsed" if end > as_of else "complete",
    }


def _is_absence_time(project_name: str, task_name: str) -> bool:
    identity = normalized_compact(f"{project_name} {task_name}")
    return any(marker in identity for marker in TIME_CONTROL_ABSENCE_MARKERS)


def technical_key(value) -> str:
    if value in (None, "", False):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def split_identifiers(value) -> list[str]:
    return [item.strip() for item in str(value or "").split("|") if item.strip()]


def parse_sellsy_references(value) -> list[str]:
    references = []
    for item in re.split(r"[|;,\r\n]+", str(value or "")):
        key = technical_key(item)
        if key and key not in references:
            references.append(key)
    return references


def normalized_compact(value) -> str:
    text = unicodedata.normalize("NFKD", clean_text(value, ""))
    return "".join(char for char in text if char.isalnum()).casefold()


def normalize_bu(value) -> str:
    label = clean_text(value)
    key = normalized_compact(label)
    mappings = (
        ("mandarineinterne", "Mandarine Interne"), ("dileaplogist", "Dileap Logistique"),
        ("dileapservice", "Dileap Services"), ("moocoffice", "MOOC Office 365"),
        ("dileapacademy", "Dileap Academy"), ("dileapcontent", "Dileap Content"),
    )
    for marker, result in mappings:
        if marker in key:
            return result
    return label.title() if label.isupper() else label


def _project_default_bu(name: str) -> str:
    upper = clean_text(name, "").upper()
    if "INTERNE" in upper or upper.startswith("MANDEXP"):
        return "Mandarine Interne"
    if upper.startswith("DL"):
        return "Dileap Logistique"
    return "Dileap Services" if upper else "BU non rattachée"


def _is_internal_project(name: str) -> bool:
    upper = clean_text(name, "").upper()
    return "INTERNE" in upper or upper.startswith("MANDEXP")


def _configured_pseudo_employee_ids() -> frozenset[str]:
    configured = set(PSEUDO_EMPLOYEE_IDS_BY_DATABASE.get("ODOO-prod2020", ()))
    configured.update(
        identifier.strip()
        for identifier in os.environ.get("ODOO_PSEUDO_EMPLOYEE_IDS", "").split(",")
        if identifier.strip()
    )
    return frozenset(configured)


def _is_pseudo_employee(employee_id: str, employee: dict) -> bool:
    """Identify historical non-human employees, preferring database-scoped IDs."""

    if str(employee_id or "") in _configured_pseudo_employee_ids():
        return True
    identity = normalized_compact(
        f"{employee.get('name', '')} {employee.get('department', '')}"
    )
    return bool(identity) and any(marker in identity for marker in PSEUDO_EMPLOYEE_MARKERS)


def _is_labor_employee(employee: dict, employee_id: str = "") -> bool:
    """Keep every identified employee in reporting, including legacy cost carriers."""

    identity = normalized_compact(
        f"{employee.get('name', '')} {employee.get('department', '')}"
    )
    return bool(identity)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ReportBuildError(f"Source requise absente : {path.relative_to(PROJECT_ROOT)}")
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source, delimiter=";"))


def _read_sellsy(path: Path, kind: str) -> list[dict]:
    if not path.exists():
        raise ReportBuildError(f"Source requise absente : {path.relative_to(PROJECT_ROOT)}")
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        headers = [clean_text(value, "") for value in next(sheet.iter_rows(values_only=True))]
        indexes = {normalized_compact(header): index for index, header in enumerate(headers)}
        expected = ["ID", "Numéro", "Date", "Client", "Objet", "Montant HT", "commercialmandarine", "bu-mbs", "cpmbs", "type-vente", "idplf", "project"]
        if kind == "order":
            expected.append("Client_id")
        missing = [header for header in expected if normalized_compact(header) not in indexes]
        if missing:
            raise ReportBuildError(f"Schéma Sellsy inattendu dans {path.name} : colonnes absentes {', '.join(missing)}")

        def value_at(values, header):
            index = indexes.get(normalized_compact(header))
            return values[index] if index is not None and index < len(values) else None

        rows = []
        for values in sheet.iter_rows(min_row=2, values_only=True):
            if not any(value not in (None, "") for value in values):
                continue
            rows.append({
                "kind": kind, "id": clean_text(value_at(values, "ID"), ""),
                "number": clean_text(value_at(values, "Numéro"), ""), "date": as_date(value_at(values, "Date")),
                "client_id": clean_text(value_at(values, "Client_id"), ""), "client": clean_text(value_at(values, "Client")),
                "object": clean_text(value_at(values, "Objet"), ""), "amount": as_float(value_at(values, "Montant HT")),
                "commercial": clean_text(value_at(values, "commercialmandarine"), ""), "bu": normalize_bu(value_at(values, "bu-mbs")),
                "owner": clean_text(value_at(values, "cpmbs"), ""), "sale_type": clean_text(value_at(values, "type-vente"), ""),
                "platform_id": clean_text(value_at(values, "idplf"), "0"), "project": clean_text(value_at(values, "project"), ""),
                "status": clean_text(value_at(values, "Statut"), ""),
                "order_id": clean_text(value_at(values, "Commande_ID"), ""),
                "order_number": clean_text(value_at(values, "Commande_NumÃ©ro"), ""),
                "delivery_date": as_date(value_at(values, "BL_Date")),
            })
        return rows
    finally:
        workbook.close()


def _source_rows(metadata: dict, source_files: dict[str, Path]) -> list[dict]:
    definitions = (
        ("Clients Odoo", "Odoo", "odoo_clients", "Revision Odoo/clients.csv"),
        ("Commandes Odoo", "Odoo", "odoo_orders", "Revision Odoo/commandes.csv"),
        ("Lignes de commandes Odoo", "Odoo", "odoo_order_lines", "Revision Odoo/lignes_commandes.csv"),
        ("Projets Odoo", "Odoo", "odoo_projects", "Revision Odoo/projets.csv"),
        ("Tâches Odoo", "Odoo", "odoo_tasks", "Revision Odoo/taches.csv"),
        ("Feuilles de temps Odoo", "Odoo", "odoo_times", "Revision Odoo/temps.csv"),
        ("Coûts fixes Odoo", "Odoo", "odoo_fixed_costs", "Revision Odoo/couts_fixes.csv"),
        ("Collaborateurs Odoo", "Odoo", "odoo_collaborators", "Revision Odoo/collaborateurs.csv"),
        ("Commandes Sellsy", "Sellsy", "sellsy_orders", "Revision Sellsy/orders.xlsx"),
        ("Factures Sellsy", "Sellsy", "sellsy_invoices", "Revision Sellsy/invoices.xlsx"),
        ("Avoirs Sellsy", "Sellsy", "sellsy_credit_notes", "Revision Sellsy/credit-notes.xlsx"),
    )
    return [{
        "name": name,
        "system": system,
        "file": str(source_files[key].resolve()),
        "logicalFile": path,
        "records": metadata[key]["records"],
        "lastDate": metadata[key].get("last_date"),
        "status": "success",
    } for name, system, key, path in definitions]


def _project_candidates(orders, lines, tasks, projects):
    candidates = defaultdict(set)
    lines_by_id = {row["id"]: row for row in lines}
    for order_id, order in orders.items():
        if order.get("project_id"):
            candidates[order_id].add(order["project_id"])
        candidates[order_id].update(split_identifiers(order.get("project_ids")))
    for line in lines:
        order_id = line.get("order_id", "")
        if not order_id:
            continue
        if line.get("project_id"):
            candidates[order_id].add(line["project_id"])
        task = tasks.get(line.get("task_id", ""), {})
        if task.get("project_id"):
            candidates[order_id].add(task["project_id"])
    for task in tasks.values():
        project_id = task.get("project_id", "")
        if not project_id:
            continue
        if task.get("sale_order_id"):
            candidates[task["sale_order_id"]].add(project_id)
        line = lines_by_id.get(task.get("sale_line_id", ""), {})
        if line.get("order_id"):
            candidates[line["order_id"]].add(project_id)
    for project_id, project in projects.items():
        if project.get("sale_order_id"):
            candidates[project["sale_order_id"]].add(project_id)
        line = lines_by_id.get(project.get("sale_line_id", ""), {})
        if line.get("order_id"):
            candidates[line["order_id"]].add(project_id)
    return candidates


def _document_assignment(document, project_key_map, today):
    if document.get("date") and document["date"] > today:
        return "EXCLUDED", "", "Document postérieur à la date du calcul"
    key = technical_key(document.get("project"))
    if not key:
        return "MISSING_REFERENCE", "", "Champ project Sellsy non renseigné"
    candidates = project_key_map.get(key, set())
    if not candidates:
        return "MISSING_ODOO", "", "Aucune commande rapprochée ne relie ce project Sellsy à Odoo"
    if len(candidates) > 1:
        return "AMBIGUOUS", "", "Le project Sellsy correspond à plusieurs projets Odoo"
    return "MATCHED", next(iter(candidates)), ""


def build_payload(*, as_of: date | None = None, source_files: dict[str, Path] | None = None) -> dict:
    today = as_of or date.today()
    sources = source_files or _resolved_source_files()
    period_start, fiscal_end = fiscal_period(today)
    period_end = min(today, fiscal_end)
    client_rows = _read_csv(sources["odoo_clients"])
    odoo_order_rows = _read_csv(sources["odoo_orders"])
    line_rows = _read_csv(sources["odoo_order_lines"])
    project_source = _read_csv(sources["odoo_projects"])
    task_rows = _read_csv(sources["odoo_tasks"])
    time_rows = _read_csv(sources["odoo_times"])
    fixed_cost_rows = _read_csv(sources["odoo_fixed_costs"])
    collaborator_source = _read_csv(sources["odoo_collaborators"])
    sellsy_orders = [
        item for item in _read_sellsy(sources["sellsy_orders"], "order")
        if is_sellsy_document_in_scope(item.get("date"))
    ]
    invoices = [
        item for item in _read_sellsy(sources["sellsy_invoices"], "invoice")
        if is_sellsy_document_in_scope(item.get("date"))
    ]
    credits = [
        item for item in _read_sellsy(sources["sellsy_credit_notes"], "credit-note")
        if is_sellsy_document_in_scope(item.get("date"))
    ]

    clients = {row["id"]: {"name": clean_text(row.get("name"))} for row in client_rows}
    employees, users, departments = {}, {}, {}
    for row in collaborator_source:
        if row.get("employee_id"):
            employees[row["employee_id"]] = {
                "id": row["employee_id"],
                "name": clean_text(row.get("employee_name")),
                "department": clean_text(row.get("department_name")),
                "active": str(row.get("active", "")).casefold() == "true",
            }
        if row.get("user_id"):
            users[row["user_id"]] = clean_text(row.get("user_name"))
        if row.get("department_id"):
            departments[row["department_id"]] = clean_text(row.get("department_name"))

    projects = {}
    for row in project_source:
        partner_id = row.get("partner_id", "")
        partner = clients.get(partner_id, {})
        final_id = partner_id
        projects[row["id"]] = {
            "id": row["id"], "name": clean_text(row.get("name")), "manager": employees.get(row.get("employee_id", ""), {}).get("name", "Non renseigné"),
            "partner_id": partner_id, "client_id": final_id, "client": clients.get(final_id, partner).get("name", "Client Odoo non renseigné"),
            "analytic_account_id": row.get("analytic_account_id", ""),
            "sale_line_id": row.get("sale_line_id", ""), "sale_order_id": row.get("sale_order_id", ""),
            "active": str(row.get("active", "")).casefold() == "true",
        }
    tasks = {row["id"]: {"id": row["id"], "name": clean_text(row.get("name")), "project_id": row.get("project_id", ""), "employee_id": row.get("employee_id", ""), "sale_line_id": row.get("sale_line_id", ""), "sale_order_id": row.get("sale_order_id", ""), "planned_hours": as_float(row.get("planned_hours"))} for row in task_rows}
    odoo_orders = {row["id"]: row for row in odoo_order_rows}
    candidates = _project_candidates(odoo_orders, line_rows, tasks, projects)
    odoo_orders_by_project = defaultdict(set)
    for order_id, project_ids in candidates.items():
        if order_id not in odoo_orders:
            continue
        for project_id in project_ids:
            odoo_orders_by_project[project_id].add(order_id)
    reference_index = defaultdict(set)
    for order_id, order in odoo_orders.items():
        refs = parse_sellsy_references(order.get("x_num_sellsy"))
        for ref in refs:
            reference_index[ref].add(order_id)

    projects_by_analytic_account = defaultdict(set)
    for project_id, project in projects.items():
        if project.get("analytic_account_id"):
            projects_by_analytic_account[project["analytic_account_id"]].add(project_id)

    fixed_cost_stats = defaultdict(
        lambda: {
            "signedAmount": 0.0,
            "cost": 0.0,
            "rows": 0,
            "categories": Counter(),
            "months": defaultdict(
                lambda: {"signedAmount": 0.0, "cost": 0.0, "categories": Counter()}
            ),
            "details": [],
        }
    )
    fixed_cost_linkage_anomaly_count = 0
    seen_fixed_cost_ids = set()
    fixed_cost_dates = []
    for row in fixed_cost_rows:
        row_id = str(row.get("id") or "")
        if not row_id or row_id in seen_fixed_cost_ids:
            continue
        seen_fixed_cost_ids.add(row_id)
        code = str(row.get("product_default_code") or "").strip().upper()
        if code not in FIXED_COST_CATEGORIES:
            continue
        day = as_date(row.get("date"))
        if not day:
            fixed_cost_linkage_anomaly_count += 1
            continue
        fixed_cost_dates.append(day)
        # A future-dated analytic entry is a forecast, not an incurred cost.
        # Keep its date available to source-quality metadata but exclude it
        # from every project cost and margin as of the report generation date.
        if day > today:
            continue
        direct_project_id = row.get("project_id", "")
        account_id = row.get("account_id", "")
        if direct_project_id and direct_project_id in projects:
            project_id = direct_project_id
        else:
            account_candidates = projects_by_analytic_account.get(account_id, set())
            project_id = next(iter(account_candidates)) if len(account_candidates) == 1 else ""
        if not project_id:
            fixed_cost_linkage_anomaly_count += 1
            continue
        signed_amount = as_float(row.get("amount"))
        cost = -signed_amount
        category = FIXED_COST_CATEGORIES[code]
        month = month_key(day)
        stats = fixed_cost_stats[project_id]
        stats["signedAmount"] += signed_amount
        stats["cost"] += cost
        stats["rows"] += 1
        stats["categories"][code] += cost
        stats["months"][month]["signedAmount"] += signed_amount
        stats["months"][month]["cost"] += cost
        stats["months"][month]["categories"][code] += cost
        stats["details"].append({
            "id": row_id,
            "date": day.isoformat(),
            "description": clean_text(row.get("name")),
            "productCode": code,
            "productName": clean_text(row.get("product_name")),
            "category": category,
            "quantity": round_metric(as_float(row.get("unit_amount"))),
            "signedAmount": round_metric(signed_amount),
            "cost": round_metric(cost),
            "tagIds": split_identifiers(row.get("tag_ids")),
        })

    project_stats = defaultdict(lambda: {"hours": 0.0, "cost": 0.0, "timeRows": 0, "firstDate": None, "lastDate": None, "months": defaultdict(lambda: {"hours": 0.0, "cost": 0.0})})
    task_stats = defaultdict(lambda: {"hours": 0.0, "cost": 0.0, "months": defaultdict(lambda: {"hours": 0.0, "cost": 0.0})})
    project_departments = defaultdict(lambda: defaultdict(lambda: {"hours": 0.0, "cost": 0.0, "months": defaultdict(lambda: {"hours": 0.0, "cost": 0.0})}))
    people = defaultdict(
        lambda: {
            "employee_id": "",
            "department": "Non renseigné",
            "active": False,
            "hours": 0.0,
            "cost": 0.0,
            "first_activity": None,
            "last_activity": None,
            "months": defaultdict(
                lambda: {
                    "hours": 0.0,
                    "cost": 0.0,
                    "client_hours": 0.0,
                    "internal_hours": 0.0,
                    "absence_hours": 0.0,
                    "unclassified_hours": 0.0,
                    "project_hours": Counter(),
                }
            ),
        }
    )
    for employee_id, employee in employees.items():
        if (
            employee.get("active")
            and _is_labor_employee(employee, employee_id)
            and not _is_pseudo_employee(employee_id, employee)
        ):
            person = people[employee["name"]]
            person["employee_id"] = employee_id
            person["department"] = employee.get("department") or "Non renseigné"
            person["active"] = True
    odoo_dates = []
    fictitious_cost_stats = defaultdict(
        lambda: {
            "hours": 0.0,
            "cost": 0.0,
            "rows": 0,
            "employees": set(),
        }
    )
    selected = non_time = missing_project = future_time = unvalued = missing_task = 0
    for row in time_rows:
        day = as_date(row.get("date"))
        if day:
            odoo_dates.append(day)
            future_time += day > today
        if not day or day > today:
            continue
        hours = as_float(row.get("unit_amount"))
        employee_id = row.get("employee_id", "")
        employee = employees.get(employee_id, {})
        is_pseudo_employee = bool(
            employee_id and _is_pseudo_employee(employee_id, employee)
        )
        if employee_id and hours != 0 and is_pseudo_employee:
            project_id = row.get("project_id", "")
            if project_id and project_id in projects:
                amount = as_float(row.get("amount"))
                stats = fictitious_cost_stats[project_id]
                stats["hours"] += hours
                stats["cost"] -= amount
                stats["rows"] += 1
                stats["employees"].add(employee.get("name") or f"Employé Odoo {employee_id}")
        if not employee_id or hours == 0 or not _is_labor_employee(employee, employee_id):
            non_time += 1
            continue
        in_period = period_start <= day <= period_end
        selected += in_period
        amount = as_float(row.get("amount"))
        cost = -amount
        unvalued += amount == 0
        project_id = row.get("project_id", "")
        task_id = row.get("task_id", "")
        person_name = employee.get("name") or users.get(row.get("user_id", ""), "Collaborateur inconnu")
        department = employee.get("department") or departments.get(row.get("department_id", ""), "Non renseigné")
        if not is_pseudo_employee:
            person = people[person_name]
            person["employee_id"] = employee_id
            person["department"] = department
            person["active"] = bool(employee.get("active"))
            person["hours"] += hours
            person["cost"] += cost
            person["first_activity"] = min(person["first_activity"], day) if person["first_activity"] else day
            person["last_activity"] = max(person["last_activity"], day) if person["last_activity"] else day
            person_month = person["months"][month_key(day)]
            person_month["hours"] += hours
            person_month["cost"] += cost
            project_name = projects.get(project_id, {}).get("name", "")
            task_name = tasks.get(task_id, {}).get("name", "")
            if _is_absence_time(project_name, task_name):
                person_month["absence_hours"] += hours
            elif project_name and _is_internal_project(project_name):
                person_month["internal_hours"] += hours
            elif project_name:
                person_month["client_hours"] += hours
            else:
                person_month["unclassified_hours"] += hours
            if project_id:
                person_month["project_hours"][project_id] += hours
        if not project_id or project_id not in projects:
            missing_project += in_period
            continue
        month = month_key(day)
        stats = project_stats[project_id]
        stats["timeRows"] += 1
        stats["firstDate"] = min(stats["firstDate"], day) if stats["firstDate"] else day
        stats["lastDate"] = max(stats["lastDate"], day) if stats["lastDate"] else day
        stats["hours"] += hours; stats["cost"] += cost; stats["months"][month]["hours"] += hours; stats["months"][month]["cost"] += cost
        if task_id and task_id in tasks:
            task_stat = task_stats[task_id]
            task_stat["hours"] += hours; task_stat["cost"] += cost; task_stat["months"][month]["hours"] += hours; task_stat["months"][month]["cost"] += cost
        else:
            missing_task += in_period
        dep = project_departments[project_id][department]
        dep["hours"] += hours; dep["cost"] += cost; dep["months"][month]["hours"] += hours; dep["months"][month]["cost"] += cost
    order_matches, project_orders = [], defaultdict(list)
    project_metadata = defaultdict(lambda: {"bus": Counter(), "platforms": Counter()})
    sellsy_orders_by_id = {}
    sellsy_project_map = defaultdict(set)
    for item in sellsy_orders:
        status, reason, project_id, odoo_id = "MATCHED", "", "", ""
        ref = technical_key(item.get("number"))
        if item.get("date") and item["date"] > today:
            status, reason = "EXCLUDED", "Commande postérieure à la date du calcul"
        elif not ref:
            status, reason = "MISSING_REFERENCE", "Numéro Sellsy absent"
        elif not reference_index.get(ref):
            status, reason = "MISSING_ODOO", "Aucune commande Odoo ne porte cette référence"
        elif len(reference_index[ref]) > 1:
            status, reason = "DUPLICATE_REFERENCE", "Référence présente sur plusieurs commandes Odoo"
        else:
            odoo_id = next(iter(reference_index[ref]))
            project_ids = candidates.get(odoo_id, set())
            if not project_ids:
                status, reason = "MISSING_PROJECT", "Commande Odoo sans projet technique"
            elif len(project_ids) > 1:
                status, reason = "AMBIGUOUS", "Commande Odoo liée à plusieurs projets"
            else:
                project_id = next(iter(project_ids))
                if project_id not in projects:
                    status, reason, project_id = "MISSING_PROJECT", "Projet Odoo introuvable", ""
                elif not projects[project_id]["client_id"]:
                    status, reason = "MISSING_CLIENT", "Projet Odoo sans client final"
        result = {"id": item["id"], "number": item["number"], "date": item["date"].isoformat() if item.get("date") else None, "commercialClient": item["client"], "sellsyProject": item["project"], "amount": round_metric(item["amount"]), "sellsyStatus": item.get("status") or "", "status": status, "reason": reason, "odooOrderId": odoo_id, "projectId": project_id}
        order_matches.append(result)
        sellsy_orders_by_id[str(item["id"])] = result
        if project_id and status in {"MATCHED", "MISSING_CLIENT"}:
            project_orders[project_id].append(result)
            weight = max(abs(item["amount"]), 1.0)
            project_metadata[project_id]["bus"][item["bu"]] += weight
            project_metadata[project_id]["platforms"][item["platform_id"]] += weight
            if technical_key(item.get("project")):
                sellsy_project_map[technical_key(item["project"])].add(project_id)

    def match_invoices(documents):
        by_project = defaultdict(list)
        for document in documents:
            order_id = str(document.get("order_id") or "")
            order = sellsy_orders_by_id.get(order_id)
            project_id = ""
            if document.get("date") and document["date"] > today:
                status, reason = "EXCLUDED", "Facture postÃ©rieure Ã  la date du calcul"
            elif not order_id:
                status, reason = "MISSING_REFERENCE", "Facture sans commande parente native Sellsy"
            elif not order:
                status, reason = "MISSING_ORDER", "Commande parente Sellsy absente de l'acquisition"
            elif order["status"] not in {"MATCHED", "MISSING_CLIENT"} or not order.get("projectId"):
                status, reason = "MISSING_ODOO", "La commande parente Sellsy n'est pas rapprochÃ©e Ã  un projet Odoo"
            else:
                status, reason, project_id = "MATCHED", "", order["projectId"]
            result = {"id": document["id"], "number": document["number"], "date": document["date"].isoformat() if document.get("date") else None, "deliveryDate": document["delivery_date"].isoformat() if document.get("delivery_date") else None, "commercialClient": document["client"], "sellsyProject": document["project"], "amount": round_metric(document["amount"]), "sellsyStatus": document.get("status") or "", "status": status, "reason": reason, "projectId": project_id, "sellsyOrderId": order_id, "sellsyOrderNumber": document.get("order_number") or (order.get("number") if order else "")}
            if status == "MATCHED":
                by_project[project_id].append(result)
        return by_project

    project_invoices = match_invoices(invoices)

    def match_credits(documents):
        by_project = defaultdict(list)
        for document in documents:
            status, project_id, reason = _document_assignment(document, sellsy_project_map, today)
            result = {"id": document["id"], "number": document["number"], "date": document["date"].isoformat() if document.get("date") else None, "commercialClient": document["client"], "sellsyProject": document["project"], "amount": round_metric(abs(document["amount"])), "sellsyStatus": document.get("status") or "", "status": status, "reason": reason, "projectId": project_id}
            if status == "MATCHED":
                by_project[project_id].append(result)
        return by_project

    project_credits = match_credits(credits)
    profiles = {}
    for project_id, project in projects.items():
        metadata = project_metadata[project_id]
        profiles[project_id] = {"bu": metadata["bus"].most_common(1)[0][0] if metadata["bus"] else _project_default_bu(project["name"]), "platform_id": metadata["platforms"].most_common(1)[0][0] if metadata["platforms"] else "0"}

    monthly_bu = defaultdict(lambda: {
        "hours": 0.0, "cost": 0.0, "laborCost": 0.0, "otherCosts": 0.0,
        "totalCost": 0.0, "invoiceAmount": 0.0, "creditAmount": 0.0,
        "revenue": 0.0, "orders": 0.0,
    })
    for project_id, stats in project_stats.items():
        bu = profiles.get(project_id, {}).get("bu", "BU non rattachée")
        for month, values in stats["months"].items():
            row = monthly_bu[(month, bu)]
            row["hours"] += values["hours"]
            row["cost"] += values["cost"]
            row["laborCost"] += values["cost"]
            row["totalCost"] += values["cost"]
    for project_id, stats in fixed_cost_stats.items():
        bu = profiles.get(project_id, {}).get("bu", "BU non rattachée")
        for month, values in stats["months"].items():
            row = monthly_bu[(month, bu)]
            row["otherCosts"] += values["cost"]
            row["totalCost"] += values["cost"]
    eligible_orders = [item for item in sellsy_orders if item["date"] and item["date"] <= today]
    eligible_invoices = [item for item in invoices if item["date"] and item["date"] <= today]
    eligible_credits = [item for item in credits if item["date"] and item["date"] <= today]
    inactive_statuses = {"cancelled", "canceled", "annule", "annulee"}
    for item in eligible_invoices:
        if normalized_compact(item.get("status")) not in inactive_statuses:
            row = monthly_bu[(month_key(item["date"]), item["bu"])]
            row["invoiceAmount"] += item["amount"]
            row["revenue"] += item["amount"]
    for item in eligible_credits:
        if normalized_compact(item.get("status")) not in inactive_statuses:
            row = monthly_bu[(month_key(item["date"]), item["bu"])]
            row["creditAmount"] += abs(item["amount"])
            row["revenue"] -= abs(item["amount"])
    for item in eligible_orders:
        order_status = normalized_compact(item.get("status"))
        if order_status not in inactive_statuses | {"draft"}:
            monthly_bu[(month_key(item["date"]), item["bu"])]["orders"] += item["amount"]
    monthly_rows = [{"month": month, "bu": bu, **{key: round_metric(value) for key, value in values.items()}} for (month, bu), values in sorted(monthly_bu.items())]

    invoice_status_rows = []
    for item in eligible_invoices:
        invoice_status_rows.append({
            "month": month_key(item["date"]),
            "bu": item["bu"],
            "status": item.get("status") or "Non renseigné",
            "amount": round_metric(item["amount"]),
            "count": 1,
        })

    collaborators = []
    for name, stats in people.items():
        months = []
        for month, values in sorted(stats["months"].items()):
            bus = Counter()
            for project_id, hours in values["project_hours"].items(): bus[profiles.get(project_id, {}).get("bu", "BU non rattachée")] += hours
            months.append({
                "month": month,
                "hours": round_metric(values["hours"]),
                "cost": round_metric(values["cost"]),
                "clientHours": round_metric(values["client_hours"]),
                "internalHours": round_metric(values["internal_hours"]),
                "absenceHours": round_metric(values["absence_hours"]),
                "unclassifiedHours": round_metric(values["unclassified_hours"]),
                "bu": bus.most_common(1)[0][0] if bus else "BU non rattachée",
            })

        time_control_months = []
        month_values = {row["month"]: row for row in months}
        for month in month_range(TIME_CONTROL_FIRST_FISCAL_START, fiscal_end):
            values = month_values.get(month, {})
            hours = float(values.get("hours", 0.0))
            time_control_months.append({
                "month": month,
                "hours": round_metric(hours),
                "clientHours": round_metric(float(values.get("clientHours", 0.0))),
                "internalHours": round_metric(float(values.get("internalHours", 0.0))),
                "absenceHours": round_metric(float(values.get("absenceHours", 0.0))),
                "unclassifiedHours": round_metric(float(values.get("unclassifiedHours", 0.0))),
                **monthly_time_control(
                    month,
                    hours,
                    as_of=today,
                    first_activity=stats["first_activity"],
                    last_activity=stats["last_activity"],
                    active=bool(stats["active"]),
                ),
            })
        collaborators.append({
            "id": stats["employee_id"],
            "name": name,
            "department": stats["department"],
            "active": bool(stats["active"]),
            "firstActivity": stats["first_activity"].isoformat() if stats["first_activity"] else None,
            "lastActivity": stats["last_activity"].isoformat() if stats["last_activity"] else None,
            "hours": round_metric(stats["hours"]),
            "cost": round_metric(stats["cost"]),
            "months": months,
            "timeControlMonths": time_control_months,
        })
    collaborators.sort(key=lambda item: item["name"].casefold())

    tasks_by_project = defaultdict(list)
    for task in tasks.values(): tasks_by_project[task["project_id"]].append(task)
    project_rows = []
    unlinked_project_count = 0
    period_months = set(month_range(period_start, period_end))
    all_project_ids = (
        set(projects)
        | set(project_stats)
        | set(project_orders)
        | set(project_invoices)
        | set(project_credits)
        | set(fixed_cost_stats)
    )
    for project_id in all_project_ids:
        project = projects.get(project_id, {"name": "Projet Odoo introuvable", "client": "Client Odoo non renseigné", "client_id": "", "manager": "Non renseigné", "active": False})
        stats = project_stats[project_id]
        orders = sorted(project_orders[project_id], key=lambda item: item.get("date") or "")
        project_invoice_rows = sorted(project_invoices[project_id], key=lambda item: item.get("date") or "")
        project_credit_rows = sorted(project_credits[project_id], key=lambda item: item.get("date") or "")
        fixed_stats = fixed_cost_stats[project_id]
        fictitious_stats = fictitious_cost_stats.get(project_id) or {
            "rows": 0, "hours": 0.0, "cost": 0.0, "employees": set()
        }
        ordered = sum(item["amount"] for item in orders); invoiced = sum(item["amount"] for item in project_invoice_rows); credited = sum(item["amount"] for item in project_credit_rows)
        net = invoiced - credited
        margin = net - stats["cost"]
        total_cost = stats["cost"] + fixed_stats["cost"]
        total_margin = net - total_cost
        internal = _is_internal_project(project["name"])
        task_values = []
        for task in sorted(tasks_by_project.get(project_id, []), key=lambda item: item["name"].casefold()):
            task_stat = task_stats[task["id"]]; planned = task["planned_hours"]; consumed = task_stat["hours"]
            rate = consumed / planned * 100 if planned > 0 else None
            status = "NO_BUDGET" if rate is None else "NORMAL" if rate < 90 else "WARNING" if rate <= 100 else "OVERRUN"
            task_values.append({"id": task["id"], "name": task["name"], "plannedHours": round_metric(planned), "budgetLabel": "Temps prévu actuel" if planned > 0 else "Budget non renseigné", "hours": round_metric(consumed), "cost": round_metric(task_stat["cost"]), "remainingHours": round_metric(planned - consumed) if planned > 0 else None, "varianceHours": round_metric(consumed - planned) if planned > 0 else None, "consumptionRate": round_metric(rate) if rate is not None else None, "status": status, "months": [{"month": month, "hours": round_metric(value["hours"]), "cost": round_metric(value["cost"])} for month, value in sorted(task_stat["months"].items())]})
        departments_values = [{"name": name, "hours": round_metric(values["hours"]), "cost": round_metric(values["cost"]), "months": [{"month": month, "hours": round_metric(value["hours"]), "cost": round_metric(value["cost"])} for month, value in sorted(values["months"].items())]} for name, values in sorted(project_departments[project_id].items())]
        order_months, invoice_months, credit_months = defaultdict(float), defaultdict(float), defaultdict(float)
        for item in orders:
            if item.get("date"): order_months[item["date"][:7]] += item["amount"]
        for item in project_invoice_rows:
            if item.get("date"): invoice_months[item["date"][:7]] += item["amount"]
        for item in project_credit_rows:
            if item.get("date"): credit_months[item["date"][:7]] += item["amount"]
        delivery_months = {
            item["deliveryDate"][:7]
            for item in project_invoice_rows
            if item.get("deliveryDate")
        }
        all_months = sorted(
            set(stats["months"])
            | set(order_months)
            | set(invoice_months)
            | set(credit_months)
            | set(fixed_stats["months"])
            | delivery_months
        )
        if stats["timeRows"] and not odoo_orders_by_project.get(project_id):
            unlinked_project_count += 1
        project_rows.append({
            "id": project_id,
            "name": project["name"],
            "clientId": project["client_id"],
            "client": project["client"],
            "manager": project["manager"],
            "bu": profiles.get(project_id, {}).get("bu", _project_default_bu(project["name"])),
            "active": project["active"],
            "internal": internal,
            "projectType": "Interne" if internal else "Client",
            "platformId": profiles.get(project_id, {}).get("platform_id", "0"),
            "hours": round_metric(stats["hours"]),
            "cost": round_metric(stats["cost"]),
            "fixedCosts": round_metric(fixed_stats["cost"]),
            "totalCost": round_metric(total_cost),
            "fixedCostSignedAmount": round_metric(fixed_stats["signedAmount"]),
            "fixedCostRows": fixed_stats["rows"],
            "fixedCostCategories": {
                code: round_metric(fixed_stats["categories"][code])
                for code in FIXED_COST_CATEGORIES
            },
            "fixedCostDetails": sorted(
                fixed_stats["details"], key=lambda item: (item["date"], item["id"])
            ),
            "odooCorrectionRequired": fictitious_stats["rows"] > 0,
            "fictitiousTimeRows": fictitious_stats["rows"],
            "fictitiousTimeHours": round_metric(fictitious_stats["hours"]),
            "fictitiousTimeCost": round_metric(fictitious_stats["cost"]),
            "fictitiousEmployees": sorted(fictitious_stats["employees"], key=str.casefold),
            "fictitiousReplacementExists": fixed_stats["rows"] > 0,
            "orderAmount": round_metric(ordered),
            "invoiceAmount": round_metric(invoiced),
            "creditAmount": round_metric(credited),
            "netRevenue": round_metric(net),
            "remainingToInvoice": round_metric(ordered - net),
            "laborMargin": round_metric(margin),
            "laborMarginRate": round_metric(margin / net * 100) if net > 0 else None,
            "totalMargin": round_metric(total_margin),
            "totalMarginRate": round_metric(total_margin / net * 100) if net > 0 else None,
            "revenue": round_metric(net),
            "margin": round_metric(margin),
            "marginRate": round_metric(margin / net * 100) if net > 0 else 0.0,
            "orderCount": len(orders),
            "invoiceCount": len(project_invoice_rows),
            "creditCount": len(project_credit_rows),
            "orders": orders,
            "invoices": project_invoice_rows,
            "creditNotes": project_credit_rows,
            "tasks": task_values,
            "departmentCosts": departments_values,
            "months": [{
                "month": month,
                "hours": round_metric(stats["months"].get(month, {}).get("hours", 0)),
                "cost": round_metric(stats["months"].get(month, {}).get("cost", 0)),
                "fixedCosts": round_metric(fixed_stats["months"].get(month, {}).get("cost", 0)),
                "fixedCostCategories": {
                    code: round_metric(
                        fixed_stats["months"].get(month, {}).get("categories", {}).get(code, 0)
                    )
                    for code in FIXED_COST_CATEGORIES
                },
                "orders": round_metric(order_months[month]),
                "invoiced": round_metric(invoice_months[month]),
                "credits": round_metric(credit_months[month]),
                "revenue": round_metric(invoice_months[month] - credit_months[month]),
                "bu": profiles.get(project_id, {}).get("bu", _project_default_bu(project["name"])),
            } for month in all_months],
        })
    project_rows.sort(key=lambda item: item["cost"], reverse=True)

    current_monthly = [row for row in monthly_rows if row["month"] in period_months]
    total_hours = sum(row["hours"] for row in current_monthly); total_cost = sum(row["totalCost"] for row in current_monthly); total_revenue = sum(row["revenue"] for row in current_monthly); total_orders = sum(row["orders"] for row in current_monthly)
    internal_hours = sum(sum(month["hours"] for month in project["months"] if month["month"] in period_months) for project in project_rows if _is_internal_project(project["name"]))

    def current(rows):
        return [item for item in rows if item.get("date") and period_start <= date.fromisoformat(item["date"]) <= period_end]
    current_orders = current(order_matches)
    order_anomalies = [item for item in current_orders if item["status"] != "MATCHED"]
    current_orders_missing_odoo = [item for item in current_orders if item["status"] == "MISSING_ODOO"]
    current_orders_missing_odoo_amount = sum(item["amount"] for item in current_orders_missing_odoo)
    unmatched_order_amount = sum(
        item["amount"]
        for item in current_orders
        if not (item["status"] in {"MATCHED", "MISSING_CLIENT"} and item["projectId"])
    )

    negative_margin_projects = [project for project in project_rows if project["netRevenue"] > 0 and project["laborMargin"] < 0]
    alerts = []
    if negative_margin_projects: alerts.append({"level": "critical", "title": f"{len(negative_margin_projects)} projets ont une marge sur main-d’œuvre négative", "detail": "Le coût valorisé des temps dépasse le CA net facturé rapproché.", "action": "Voir les projets", "target": "projects"})
    if current_orders_missing_odoo:
        missing_amount_label = f"{round_metric(current_orders_missing_odoo_amount):,.2f}".replace(",", " ").replace(".", ",")
        alerts.append({"level": "warning", "title": f"{len(current_orders_missing_odoo)} commandes Sellsy ne sont pas encore dans Odoo", "detail": f"Elles représentent {missing_amount_label} € HT sur l’exercice en cours et restent incluses dans la synthèse.", "action": "Voir les projets", "target": "projects"})
    if unvalued: alerts.append({"level": "warning", "title": f"{unvalued} imputations horaires sans coût valorisé", "detail": "Le champ amount vaut zéro ; la marge des projets concernés peut être incomplète.", "action": "Voir les projets", "target": "projects"})

    metadata = {"odoo_clients": {"records": len(client_rows)}, "odoo_orders": {"records": len(odoo_order_rows)}, "odoo_order_lines": {"records": len(line_rows)}, "odoo_projects": {"records": len(project_source)}, "odoo_tasks": {"records": len(task_rows)}, "odoo_times": {"records": len(time_rows), "last_date": max((day for day in odoo_dates if day <= today), default=None)}, "odoo_fixed_costs": {"records": len(fixed_cost_rows), "last_date": max((day for day in fixed_cost_dates if day <= today), default=None)}, "odoo_collaborators": {"records": len(collaborator_source)}, "sellsy_orders": {"records": len(sellsy_orders), "last_date": max((item["date"] for item in sellsy_orders if item["date"] and item["date"] <= today), default=None)}, "sellsy_invoices": {"records": len(invoices), "last_date": max((item["date"] for item in invoices if item["date"] and item["date"] <= today), default=None)}, "sellsy_credit_notes": {"records": len(credits), "last_date": max((item["date"] for item in credits if item["date"] and item["date"] <= today), default=None)}}
    for value in metadata.values():
        if isinstance(value.get("last_date"), date): value["last_date"] = value["last_date"].isoformat()
    dates = [day for day in odoo_dates + fixed_cost_dates if day <= today] + [item["date"] for item in eligible_orders + eligible_invoices + eligible_credits if item["date"]]
    data_through = max([period_start] + dates)
    starts = sorted({fiscal_period(day)[0] for day in dates} | {period_start})
    selectable = [start for start in starts if start >= date(2021, 7, 1)] or [period_start]
    periods = [{
        "value": f"{start.year}-{start.year + 1}",
        "label": f"Exercice {start.year}–{start.year + 1}",
        "start": start.isoformat(),
        "end": date(start.year + 1, 6, 30).isoformat(),
        "months": month_range(start, min(date(start.year + 1, 6, 30), today)),
        "fullMonths": month_range(start, date(start.year + 1, 6, 30)),
    } for start in selectable]
    reference_eligible_orders = [item for item in order_matches if item["status"] != "EXCLUDED"]
    referenced_order_count = sum(
        item["status"] not in {"MISSING_REFERENCE", "MISSING_ODOO"}
        for item in reference_eligible_orders
    )

    return {
        "meta": {"generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"), "periodStart": period_start.isoformat(), "periodEnd": fiscal_end.isoformat(), "dataThrough": data_through.isoformat(), "asOf": today.isoformat(), "currency": "EUR", "sellsyDocumentStartDate": SELLSY_DOCUMENT_START_ISO, "months": month_range(starts[0], data_through), "defaultPeriod": f"{period_start.year}-{fiscal_end.year}", "fiscalPeriods": periods, "periodLabel": f"Exercice {period_start.year}–{fiscal_end.year}", "reportSchemaVersion": 4, "rulesetVersion": 3, "businessRules": {"projectTypeField": "projectType", "totalCostIncludesFixedCosts": True, "marginBasis": "netRevenue-totalCost", "timesheetControl": {"method": "global-weekday-capacity", "dailyHours": round_metric(TIME_CONTROL_DAILY_HOURS, 2), "conformThreshold": TIME_CONTROL_CONFORM_THRESHOLD, "watchThreshold": TIME_CONTROL_WATCH_THRESHOLD, "overageWatchThreshold": TIME_CONTROL_OVERAGE_WATCH_THRESHOLD, "absencesCountAsFilledTime": True, "presenceDatesAreInferred": True}}, "provenance": active_source_provenance() if source_files is None else {"mode": "explicit-source-set"}},
        "summary": {"hours": round_metric(total_hours), "cost": round_metric(total_cost), "revenue": round_metric(total_revenue), "orders": round_metric(total_orders), "margin": round_metric(total_revenue - total_cost), "marginRate": round_metric((total_revenue - total_cost) / total_revenue * 100) if total_revenue > 0 else 0.0, "collaborators": len(collaborators), "projects": len(project_rows), "internalHours": round_metric(internal_hours), "internalShare": round_metric(internal_hours / total_hours * 100) if total_hours else 0.0},
        "monthlyBu": monthly_rows, "finance": {"invoiceStatuses": invoice_status_rows}, "collaborators": collaborators, "projects": project_rows, "sources": _source_rows(metadata, sources), "alerts": alerts,
        "controls": {
            "selectedTimeRows": selected,
            "nonTimeAnalyticRows": non_time,
            "unvaluedTimeRows": unvalued,
            "missingProjectRows": missing_project,
            "missingTaskRows": missing_task,
            "futureTimeRows": future_time,
            "unmatchedOrderCount": len(order_anomalies),
            "unmatchedOrderAmount": round_metric(unmatched_order_amount),
            "orderReferenceMatchRate": round_metric(referenced_order_count / len(reference_eligible_orders) * 100) if reference_eligible_orders else 100.0,
            "unlinkedProjectCount": unlinked_project_count,
            "fictitiousCostProjectCount": len(fictitious_cost_stats),
            "fixedCostLinkageAnomalyCount": fixed_cost_linkage_anomaly_count,
        },
    }


def validate_payload(payload: dict) -> None:
    """Reject incomplete or non-serializable report data before publication."""

    if not isinstance(payload, dict):
        raise ReportBuildError("Le rapport calcul\u00e9 n'est pas un objet JSON.")

    expected_types = {
        "meta": dict,
        "summary": dict,
        "monthlyBu": list,
        "collaborators": list,
        "projects": list,
        "sources": list,
        "alerts": list,
        "controls": dict,
    }
    for key, expected_type in expected_types.items():
        if not isinstance(payload.get(key), expected_type):
            raise ReportBuildError(f"Section de rapport invalide ou absente : {key}")

    for key in ("generatedAt", "periodStart", "periodEnd", "dataThrough", "fiscalPeriods"):
        if key not in payload["meta"]:
            raise ReportBuildError(f"M\u00e9tadonn\u00e9e de rapport absente : {key}")

    if len(payload["sources"]) != len(SOURCE_FILES):
        raise ReportBuildError(
            f"Le rapport d\u00e9crit {len(payload['sources'])} sources au lieu de {len(SOURCE_FILES)}."
        )

    for key, value in payload["summary"].items():
        if isinstance(value, float) and not math.isfinite(value):
            raise ReportBuildError(f"Indicateur non fini dans summary.{key}")

    try:
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ReportBuildError(f"Le rapport calcul\u00e9 n'est pas un JSON valide : {error}") from error


def write_payload(payload: dict, output_path: Path = OUTPUT_PATH) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
            encoding="utf-8",
        )
        os.replace(temp_path, output_path)
    finally:
        temp_path.unlink(missing_ok=True)


def rebuild_report(
    *, as_of: date | None = None, output_path: Path = OUTPUT_PATH,
    source_files: dict[str, Path] | None = None,
) -> dict:
    with report_build_lock():
        output_path.parent.mkdir(parents=True, exist_ok=True)
        for stale_temp in output_path.parent.glob(f".{output_path.name}.*.tmp"):
            stale_temp.unlink(missing_ok=True)

        resolved_sources = source_files or _resolved_source_files()
        sources_before = source_fingerprints(resolved_sources)
        payload = build_payload(as_of=as_of, source_files=resolved_sources)
        validate_payload(payload)
        if source_fingerprints(resolved_sources) != sources_before:
            raise ReportBuildError(
                "Une source a chang\u00e9 pendant la reconstruction ; l'ancienne publication est conserv\u00e9e."
            )
        write_payload(payload, output_path)
        return payload
