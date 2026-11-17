from __future__ import annotations

import csv
import gzip
import shutil
import sqlite3
import tempfile
import uuid

from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

import openpyxl


STORAGE_SCHEMA_VERSION = 2
DATABASE_FILENAMES = {"odoo": "odoo.sqlite.gz", "sellsy": "sellsy.sqlite.gz"}
UNCOMPRESSED_DATABASE_FILENAMES = {"odoo": "odoo.sqlite", "sellsy": "sellsy.sqlite"}

ODOO_TABLES = {
    "odoo_clients": (
        "clients.csv",
        "id",
        ("id", "name", "active", "company_id", "create_date", "write_date"),
    ),
    "odoo_orders": (
        "commandes.csv",
        "id",
        (
            "id", "name", "x_num_sellsy", "partner_id", "date_order", "state",
            "amount_total", "client_order_ref", "analytic_account_id", "project_id",
            "project_ids", "order_line", "company_id", "create_date", "write_date",
        ),
    ),
    "odoo_order_lines": (
        "lignes_commandes.csv",
        "id",
        (
            "id", "order_id", "name", "product_id", "product_uom_qty", "product_uom",
            "price_unit", "discount", "price_subtotal", "price_tax", "price_total",
            "project_id", "task_id", "analytic_line_ids", "untaxed_amount_invoiced",
            "untaxed_amount_to_invoice", "state", "company_id", "create_date", "write_date",
        ),
    ),
    "odoo_projects": (
        "projets.csv",
        "id",
        (
            "id", "name", "employee_id", "partner_id", "x_bu_projet", "analytic_account_id",
            "sale_line_id", "sale_order_id", "date_start", "active", "company_id",
            "create_date", "write_date",
        ),
    ),
    "odoo_tasks": (
        "taches.csv",
        "id",
        (
            "id", "name", "project_id", "employee_id", "partner_id", "sale_line_id",
            "sale_order_id", "active", "planned_hours", "effective_hours",
            "remaining_hours", "progress", "date_deadline", "date_last_stage_update",
            "create_date", "write_date",
        ),
    ),
    "odoo_times": (
        "temps.csv",
        "id",
        (
            "id", "date", "name", "employee_id", "department_id", "project_id",
            "task_id", "so_line", "unit_amount", "amount", "create_date", "write_date",
        ),
    ),
    "odoo_fixed_costs": (
        "couts_fixes.csv",
        "id",
        (
            "id", "date", "account_id", "project_id", "product_id",
            "product_default_code", "product_name", "unit_amount", "amount", "name",
            "tag_ids", "company_id", "create_date", "write_date",
        ),
    ),
    "odoo_collaborators": (
        "collaborateurs.csv",
        "employee_id",
        (
            "employee_id", "employee_name", "user_id", "user_name", "department_id",
            "department_name", "active", "company_id", "create_date", "write_date",
        ),
    ),
}

SELLSY_COLUMNS = (
    "id", "number", "date", "client", "object", "amount", "commercial", "bu", "owner",
    "sale_type", "platform_id", "project", "client_id", "status", "order_id",
    "order_number", "delivery_date",
)
SELLSY_TABLES = {
    "sellsy_orders": ("orders.xlsx", "id", SELLSY_COLUMNS),
    "sellsy_invoices": ("invoices.xlsx", "id", SELLSY_COLUMNS),
    "sellsy_credit_notes": ("credit-notes.xlsx", "id", SELLSY_COLUMNS),
}
SOURCE_TABLES = {"odoo": ODOO_TABLES, "sellsy": SELLSY_TABLES}

SELLSY_HEADER_MAP = {
    "id": "id",
    "numero": "number",
    "date": "date",
    "client": "client",
    "objet": "object",
    "montant ht": "amount",
    "commercialmandarine": "commercial",
    "bu-mbs": "bu",
    "cpmbs": "owner",
    "type-vente": "sale_type",
    "idplf": "platform_id",
    "project": "project",
    "client_id": "client_id",
    "statut": "status",
    "commande_id": "order_id",
    "commande_numero": "order_number",
    "bl_date": "delivery_date",
}


class SourceDatabaseError(RuntimeError):
    pass


def _identifier(value: str) -> str:
    if not value.replace("_", "").isalnum():
        raise ValueError(f"Identifiant SQLite invalide : {value}")
    return f'"{value}"'


def _normalized_header(value) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", str(value or ""))
    return " ".join("".join(char for char in text if not unicodedata.combining(char)).casefold().split())


def database_path(revision_dir: Path, source: str) -> Path:
    return Path(revision_dir) / DATABASE_FILENAMES[source]


def existing_database_path(revision_dir: Path, source: str) -> Path | None:
    revision_dir = Path(revision_dir)
    for filename in (DATABASE_FILENAMES[source], UNCOMPRESSED_DATABASE_FILENAMES[source]):
        candidate = revision_dir / filename
        if candidate.is_file():
            return candidate
    return None


def is_sqlite_revision(revision_dir: Path, source: str) -> bool:
    return existing_database_path(revision_dir, source) is not None


def is_database_file(path: Path) -> bool:
    name = Path(path).name.casefold()
    return name.endswith(".sqlite") or name.endswith(".sqlite.gz")


@contextmanager
def materialized_database(path: Path):
    """Yield an ordinary SQLite file, expanding a compressed revision if needed."""

    path = Path(path)
    if not path.name.casefold().endswith(".gz"):
        yield path
        return
    temporary = Path(tempfile.gettempdir()) / f"mandarine-source-{uuid.uuid4().hex}.sqlite"
    try:
        with gzip.open(path, "rb") as source, temporary.open("wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)


def _compress_database(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as input_file, gzip.open(temporary, "wb", compresslevel=6) as output_file:
            shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _create_schema(connection: sqlite3.Connection, source: str) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS sync_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    for table, (_filename, primary_key, columns) in SOURCE_TABLES[source].items():
        definitions = [
            (
                f"{_identifier(column)} INTEGER NOT NULL"
                if column == primary_key
                else f"{_identifier(column)} TEXT NOT NULL DEFAULT ''"
            )
            for column in columns
        ]
        definitions.append(f"PRIMARY KEY ({_identifier(primary_key)})")
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {_identifier(table)} ({', '.join(definitions)}) WITHOUT ROWID"
        )
        existing_columns = {
            row[1]
            for row in connection.execute(f"PRAGMA table_info({_identifier(table)})")
        }
        for column in columns:
            if column not in existing_columns:
                connection.execute(
                    f"ALTER TABLE {_identifier(table)} ADD COLUMN {_identifier(column)} "
                    "TEXT NOT NULL DEFAULT ''"
                )


def _upsert_rows(
    connection: sqlite3.Connection,
    table: str,
    primary_key: str,
    columns: tuple[str, ...],
    rows: Iterable[dict],
) -> int:
    column_sql = ", ".join(_identifier(column) for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(
        f"{_identifier(column)} = excluded.{_identifier(column)}"
        for column in columns
        if column != primary_key
    )
    statement = (
        f"INSERT INTO {_identifier(table)} ({column_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT ({_identifier(primary_key)}) DO UPDATE SET {updates}"
    )
    prepared = []
    for row in rows:
        stable_id = str(row.get(primary_key) or "").strip()
        if not stable_id or not stable_id.lstrip("-").isdigit():
            raise SourceDatabaseError(f"Identifiant source absent pour {table}.{primary_key}")
        prepared.append(tuple(
            int(stable_id)
            if column == primary_key
            else "" if row.get(column) is None else str(row.get(column))
            for column in columns
        ))
    if prepared:
        connection.executemany(statement, prepared)
    return len(prepared)


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source, delimiter=";"))


def _xlsx_rows(path: Path) -> list[dict[str, str]]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        values = sheet.iter_rows(values_only=True)
        try:
            headers = next(values)
        except StopIteration:
            return []
        mapped = [SELLSY_HEADER_MAP.get(_normalized_header(header), "") for header in headers]
        if "id" not in mapped:
            raise SourceDatabaseError(f"Colonne ID absente du fichier Sellsy {path.name}")
        result = []
        for row_values in values:
            if not any(value not in (None, "") for value in row_values):
                continue
            row = {column: "" for column in SELLSY_COLUMNS}
            for index, column in enumerate(mapped):
                if column and index < len(row_values):
                    value = row_values[index]
                    row[column] = "" if value is None else str(value)
            result.append(row)
        return result
    finally:
        workbook.close()


def _import_directory(connection: sqlite3.Connection, source: str, directory: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table, (filename, primary_key, columns) in SOURCE_TABLES[source].items():
        path = Path(directory) / filename
        if not path.is_file():
            raise SourceDatabaseError(f"Source requise absente : {path}")
        rows = _csv_rows(path) if source == "odoo" else _xlsx_rows(path)
        counts[table] = _upsert_rows(connection, table, primary_key, columns, rows)
    return counts


def build_source_database(
    source: str,
    destination: Path,
    delta_directory: Path,
    *,
    base_revision_dir: Path | None = None,
    full_sync: bool,
    metadata: dict[str, str] | None = None,
) -> dict:
    """Build a validated source database without mutating the active revision."""

    if source not in SOURCE_TABLES:
        raise ValueError(f"Source inconnue : {source}")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    compressed = destination.name.casefold().endswith(".gz")
    working_database = (
        destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.sqlite")
        if compressed
        else destination
    )
    working_database.unlink(missing_ok=True)

    migrated_legacy = False
    base_database = existing_database_path(base_revision_dir, source) if base_revision_dir else None
    if not full_sync and base_database:
        with materialized_database(base_database) as materialized:
            shutil.copy2(materialized, working_database)

    connection = _connect(working_database)
    try:
        _create_schema(connection, source)
        connection.execute("BEGIN IMMEDIATE")
        if not full_sync and base_revision_dir and not base_database:
            _import_directory(connection, source, Path(base_revision_dir))
            migrated_legacy = True
        changed = _import_directory(connection, source, Path(delta_directory))
        values = {
            "storage_schema_version": str(STORAGE_SCHEMA_VERSION),
            "source": source,
            **(metadata or {}),
        }
        connection.executemany(
            "INSERT INTO sync_metadata(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            values.items(),
        )
        connection.commit()
        connection.execute("PRAGMA optimize")
        connection.execute("VACUUM")
        counts = table_counts(working_database, connection=connection)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SourceDatabaseError(f"Intégrité SQLite invalide : {integrity}")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    try:
        if compressed:
            _compress_database(working_database, destination)
    finally:
        if compressed:
            working_database.unlink(missing_ok=True)

    return {
        "path": str(destination),
        "format": "sqlite-gzip" if compressed else "sqlite",
        "schema_version": STORAGE_SCHEMA_VERSION,
        "full_sync": bool(full_sync),
        "migrated_legacy": migrated_legacy,
        "changed": changed,
        "counts": counts,
        "records": sum(counts.values()),
    }


def validate_source_database(path: Path, source: str) -> dict[str, int]:
    path = Path(path)
    if not path.is_file():
        raise SourceDatabaseError(f"Base SQLite absente : {path}")
    with materialized_database(path) as materialized:
        connection = _connect(materialized)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise SourceDatabaseError(f"Intégrité SQLite invalide : {integrity}")
            existing = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            missing = set(SOURCE_TABLES[source]) - existing
            if missing:
                raise SourceDatabaseError("Tables SQLite absentes : " + ", ".join(sorted(missing)))
            return table_counts(materialized, connection=connection)
        finally:
            connection.close()


def table_counts(path: Path, *, connection: sqlite3.Connection | None = None) -> dict[str, int]:
    if connection is None:
        with materialized_database(Path(path)) as materialized:
            owned_connection = _connect(materialized)
            try:
                return table_counts(materialized, connection=owned_connection)
            finally:
                owned_connection.close()
    source_row = connection.execute(
        "SELECT value FROM sync_metadata WHERE key = 'source'"
    ).fetchone()
    if not source_row or source_row[0] not in SOURCE_TABLES:
        raise SourceDatabaseError("Source SQLite non renseignée")
    source = source_row[0]
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {_identifier(table)}").fetchone()[0])
        for table in SOURCE_TABLES[source]
    }


def read_tables(path: Path, logical_tables: Iterable[str]) -> dict[str, list[dict[str, str]]]:
    requested = list(logical_tables)
    with materialized_database(Path(path)) as materialized:
        connection = _connect(materialized)
        try:
            result = {}
            for logical_table in requested:
                source = "odoo" if logical_table.startswith("odoo_") else "sellsy"
                if logical_table not in SOURCE_TABLES[source]:
                    raise ValueError(f"Table source inconnue : {logical_table}")
                _filename, primary_key, columns = SOURCE_TABLES[source][logical_table]
                existing_columns = {
                    row[1]
                    for row in connection.execute(
                        f"PRAGMA table_info({_identifier(logical_table)})"
                    )
                }
                available_columns = [column for column in columns if column in existing_columns]
                column_sql = ", ".join(_identifier(column) for column in available_columns)
                rows = connection.execute(
                    f"SELECT {column_sql} FROM {_identifier(logical_table)} "
                    f"ORDER BY {_identifier(primary_key)}"
                )
                result[logical_table] = [
                    {
                        column: "" if dict(row).get(column) is None else str(dict(row).get(column))
                        for column in columns
                    }
                    for row in rows
                ]
            return result
        finally:
            connection.close()


def read_table(path: Path, logical_table: str) -> list[dict[str, str]]:
    return read_tables(path, [logical_table])[logical_table]

