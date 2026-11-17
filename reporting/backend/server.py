from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import shutil
import socket
import threading
import time
import traceback

from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core.env_loader import load_env_file
from core.integration_store import IntegrationStore
from core.odoo_exporter import export_odoo, get_source_dir
from .report_builder import OUTPUT_PATH as REPORT_OUTPUT_PATH, rebuild_report
from core.sellsy_importer import (
    SOURCE_NAMES,
    configuration_status as sellsy_configuration_status,
    get_source_dir as get_sellsy_source_dir,
    import_sellsy,
)
from core.snapshot_store import active_source_provenance
from core.synchronization_check import run_synchronization_check
from core.source_database import (
    DATABASE_FILENAMES,
    STORAGE_SCHEMA_VERSION,
    build_source_database,
    validate_source_database,
)
from core.source_revision_store import (
    active_revision,
    create_staging_revision,
    file_descriptor,
    promote_revision,
    write_revision_manifest,
)
from .source_freshness import collect_source_freshness


APP_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_DIR.parent
ENV_FILE = REPO_ROOT / ".env"
LEGACY_ENV_FILE = APP_DIR / ".env"
PROJECT_CONFIG_KEYS = {
    "ODOO_LOGIN",
    "ODOO_PASSWORD",
    "ODOO_BATCH_SIZE",
    "ODOO_TIMEOUT_SECONDS",
    "REPORTING_HOST",
    "REPORTING_PORT",
    "REPORTING_RUNTIME_DIR",
    "ADMIN_API_TOKEN",
    "SELLSY_CLIENT_ID",
    "SELLSY_CLIENT_SECRET",
    "SELLSY_EXPERIENCE_CLIENT_ID",
    "SELLSY_EXPERIENCE_CLIENT_SECRET",
    "SELLSY_API_TIMEOUT_SECONDS",
    "SELLSY_API_WORKERS",
    "SOURCE_FULL_SYNC_INTERVAL_DAYS",
    "SOURCE_SYNC_OVERLAP_MINUTES",
}
EXTERNAL_CONFIG = {
    key: os.environ[key]
    for key in PROJECT_CONFIG_KEYS
    if os.environ.get(key)
}
load_env_file(ENV_FILE)
DIST_DIR = APP_DIR / "dist"
RUNTIME_DIR = Path(os.environ.get("REPORTING_RUNTIME_DIR", APP_DIR / "runtime")).resolve()
STORE = IntegrationStore(RUNTIME_DIR / "integration_history.json")
JOB_LOCK = threading.RLock()
LOGGER = logging.getLogger("mandarine.reporting")


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """Prevent two reporting backends from sharing the same local port."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def refresh_project_env() -> None:
    """Reload local configuration while preserving explicit process overrides."""

    load_env_file(ENV_FILE if ENV_FILE.is_file() else LEGACY_ENV_FILE, override=True)
    os.environ.update(EXTERNAL_CONFIG)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_error(error: Exception) -> str:
    message = str(error)
    for key in (
        "ODOO_LOGIN",
        "ODOO_PASSWORD",
        "SELLSY_CLIENT_ID",
        "SELLSY_CLIENT_SECRET",
        "SELLSY_EXPERIENCE_CLIENT_ID",
        "SELLSY_EXPERIENCE_CLIENT_SECRET",
        "ADMIN_API_TOKEN",
    ):
        secret = os.environ.get(key, "")
        if secret:
            message = message.replace(secret, "***")
    return message[:800]


def latest_running_job(integration: str | None = None) -> dict | None:
    return next(
        (
            record
            for record in STORE.list(200)
            if record.get("status") in {"queued", "running"}
            and (integration is None or record.get("integration") == integration)
        ),
        None,
    )


def _synchronization_plan(
    source: str,
    *,
    now: datetime | None = None,
    force_full: bool = False,
) -> dict:
    now = now or datetime.now(timezone.utc)
    interval_days = max(1, int(os.environ.get("SOURCE_FULL_SYNC_INTERVAL_DAYS", "7")))
    overlap_minutes = max(0, int(os.environ.get("SOURCE_SYNC_OVERLAP_MINUTES", "10")))
    revision = active_revision(source)
    if not revision:
        return {
            "full_sync": True,
            "since": None,
            "cursor": None,
            "full_sync_at": None,
            "overlap_minutes": overlap_minutes,
            "base_revision_id": None,
            "base_revision_dir": None,
            "schema_upgrade_required": False,
            "experience_bootstrap_required": False,
            "requested_full_reconciliation": bool(force_full),
        }

    manifest = revision["manifestData"]
    synchronization = manifest.get("synchronization") or {}
    cursor_text = synchronization.get("cursor") or manifest.get("completedAt")
    full_sync_text = synchronization.get("fullSyncAt") or manifest.get("completedAt")
    try:
        cursor = datetime.fromisoformat(str(cursor_text).replace("Z", "+00:00"))
        full_sync_at = datetime.fromisoformat(str(full_sync_text).replace("Z", "+00:00"))
        if cursor.tzinfo is None:
            cursor = cursor.replace(tzinfo=timezone.utc)
        if full_sync_at.tzinfo is None:
            full_sync_at = full_sync_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        cursor = None
        full_sync_at = None
    try:
        stored_schema_version = int(
            (manifest.get("storage") or {}).get("schemaVersion") or 0
        )
    except (TypeError, ValueError):
        stored_schema_version = 0
    schema_upgrade_required = stored_schema_version < STORAGE_SCHEMA_VERSION
    experience_enabled = bool(
        os.environ.get("SELLSY_EXPERIENCE_CLIENT_ID", "").strip()
        and os.environ.get("SELLSY_EXPERIENCE_CLIENT_SECRET", "").strip()
    )
    experience_bootstrap_required = bool(
        source == "sellsy"
        and experience_enabled
        and not synchronization.get("sellsyExperienceConfigured")
    )
    full_sync = bool(
        force_full
        or schema_upgrade_required
        or experience_bootstrap_required
        or not cursor
        or not full_sync_at
        or now - full_sync_at >= timedelta(days=interval_days)
    )
    since = None if full_sync else cursor - timedelta(minutes=overlap_minutes)
    return {
        "full_sync": full_sync,
        "since": since,
        "cursor": cursor,
        "full_sync_at": full_sync_at,
        "overlap_minutes": overlap_minutes,
        "base_revision_id": revision["revisionId"],
        "base_revision_dir": Path(revision["path"]),
        "schema_upgrade_required": schema_upgrade_required,
        "experience_bootstrap_required": experience_bootstrap_required,
        "requested_full_reconciliation": bool(force_full),
    }


def _sync_manifest(result: dict, plan: dict) -> dict:
    cursor = result["started_at"]
    synchronization = {
        "mode": "full" if plan["full_sync"] else "incremental",
        "cursor": cursor,
        "fullSyncAt": cursor if plan["full_sync"] else plan["full_sync_at"].isoformat(),
        "overlapMinutes": plan["overlap_minutes"],
        "baseRevisionId": plan["base_revision_id"],
        "changedRecords": result["rows"],
        "deletionStrategy": "full-reconciliation-periodic",
        "requestedFullReconciliation": bool(
            plan.get("requested_full_reconciliation")
        ),
    }
    if "experience_configured" in result:
        synchronization["sellsyExperienceConfigured"] = bool(
            result["experience_configured"]
        )
    return synchronization


def run_odoo_job(run_id: str, force_full: bool = False) -> None:
    refresh_project_env()
    started_at = utc_now()
    STORE.update(run_id, status="running", stage="authentication", started_at=started_at)

    def progress(update: dict) -> None:
        STORE.update(
            run_id,
            stage=update.get("stage", "extraction"),
            rows=int(update.get("rows", 0)),
            last_odoo_id=update.get("last_odoo_id"),
        )

    staging_dir = None
    try:
        plan = _synchronization_plan("odoo", force_full=force_full)
        revision_id, staging_dir = create_staging_revision("odoo")
        delta_dir = staging_dir / "delta"
        modified_since = (
            plan["since"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            if plan["since"]
            else None
        )
        result = export_odoo(
            progress,
            destination_dir=delta_dir,
            modified_since=modified_since,
        )
        STORE.update(run_id, stage="storage:sqlite", changed_rows=result["rows"])
        database_file = staging_dir / DATABASE_FILENAMES["odoo"]
        database = build_source_database(
            "odoo",
            database_file,
            delta_dir,
            base_revision_dir=plan["base_revision_dir"],
            full_sync=plan["full_sync"],
            metadata={
                "revision_id": revision_id,
                "sync_cursor": result["started_at"],
                "sync_mode": "full" if plan["full_sync"] else "incremental",
            },
        )
        validate_source_database(database_file, "odoo")
        shutil.rmtree(delta_dir, ignore_errors=True)
        STORE.update(run_id, stage="validation:manifest", revision_id=revision_id)
        required = [DATABASE_FILENAMES["odoo"]]
        files = [file_descriptor(path, relative_to=staging_dir) for path in sorted(staging_dir.iterdir()) if path.is_file()]
        write_revision_manifest(staging_dir, {
            "schemaVersion": 2,
            "source": "odoo",
            "revisionId": revision_id,
            "status": "validated",
            "startedAt": result["started_at"],
            "completedAt": result["completed_at"],
            "records": database["records"],
            "files": files,
            "storage": {
                "format": database["format"],
                "schemaVersion": database["schema_version"],
                "database": DATABASE_FILENAMES["odoo"],
                "tables": database["counts"],
                "migratedLegacyRevision": database["migrated_legacy"],
            },
            "synchronization": _sync_manifest(result, plan),
            "checks": {
                "requiredFiles": True,
                "sqliteIntegrity": True,
                "readOnlyConnector": True,
            },
        })
        final_dir, _previous = promote_revision("odoo", revision_id, staging_dir, required)
        total_size = sum(path.stat().st_size for path in final_dir.iterdir() if path.is_file())
        STORE.update(
            run_id,
            status="succeeded",
            stage="completed",
            completed_at=result["completed_at"],
            duration_seconds=result["duration_seconds"],
            rows=database["records"],
            changed_rows=result["rows"],
            sync_mode="full" if plan["full_sync"] else "incremental",
            last_odoo_id=result["last_odoo_id"],
            file_name=f"Révision Odoo {revision_id}",
            file_size_bytes=total_size,
            revision_id=revision_id,
            report_generated_at=None,
            error=None,
        )
    except Exception as error:
        LOGGER.error("Odoo integration failed: %s", safe_error(error))
        LOGGER.debug("Odoo integration traceback:\n%s", traceback.format_exc())
        STORE.update(
            run_id,
            status="failed",
            stage="failed",
            completed_at=utc_now(),
            error=safe_error(error),
        )
    finally:
        if staging_dir and staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


def start_odoo_job(*, force_full: bool = False) -> tuple[dict | None, str | None]:
    with JOB_LOCK:
        running = latest_running_job()
        if running:
            return None, running["id"]
        record = STORE.create("odoo_timesheets")
        record = STORE.update(
            record["id"],
            requested_sync_mode="full" if force_full else "automatic",
        ) or record
        thread = threading.Thread(
            target=run_odoo_job,
            args=(record["id"], force_full),
            name=f"odoo-export-{record['id'][:8]}",
            daemon=True,
        )
        thread.start()
        return record, None


def run_sellsy_job(run_id: str, force_full: bool = False) -> None:
    refresh_project_env()
    started_at = utc_now()
    pending_files = [
        {"source": source_name, "status": "pending", "file_name": None, "size_bytes": None}
        for source_name in SOURCE_NAMES
    ]
    STORE.update(
        run_id,
        status="running",
        stage="configuration",
        started_at=started_at,
        files=pending_files,
    )

    def progress(update: dict) -> None:
        STORE.update(
            run_id,
            stage=update.get("stage", "download"),
            files=update.get("files", pending_files),
            processed=int(update.get("processed", 0)),
            total=int(update.get("total", 0)),
        )

    staging_dir = None
    try:
        plan = _synchronization_plan("sellsy", force_full=force_full)
        revision_id, staging_dir = create_staging_revision("sellsy")
        delta_dir = staging_dir / "delta"
        updated_since = plan["since"].isoformat() if plan["since"] else None
        result = import_sellsy(
            progress,
            destination_dir=delta_dir,
            updated_since=updated_since,
        )
        STORE.update(run_id, stage="storage:sqlite", files=result["files"], changed_rows=result["rows"])
        database_file = staging_dir / DATABASE_FILENAMES["sellsy"]
        database = build_source_database(
            "sellsy",
            database_file,
            delta_dir,
            base_revision_dir=plan["base_revision_dir"],
            full_sync=plan["full_sync"],
            metadata={
                "revision_id": revision_id,
                "sync_cursor": result["started_at"],
                "sync_mode": "full" if plan["full_sync"] else "incremental",
            },
        )
        validate_source_database(database_file, "sellsy")
        shutil.rmtree(delta_dir, ignore_errors=True)
        STORE.update(run_id, stage="validation:manifest", files=result["files"], revision_id=revision_id)
        required = [DATABASE_FILENAMES["sellsy"]]
        files = [file_descriptor(path, relative_to=staging_dir) for path in sorted(staging_dir.iterdir()) if path.is_file()]
        write_revision_manifest(staging_dir, {
            "schemaVersion": 2,
            "source": "sellsy",
            "revisionId": revision_id,
            "status": "validated",
            "startedAt": result["started_at"],
            "completedAt": result["completed_at"],
            "records": database["records"],
            "files": files,
            "categories": result["files"],
            "storage": {
                "format": database["format"],
                "schemaVersion": database["schema_version"],
                "database": DATABASE_FILENAMES["sellsy"],
                "tables": database["counts"],
                "migratedLegacyRevision": database["migrated_legacy"],
            },
            "synchronization": _sync_manifest(result, plan),
            "checks": {
                "requiredFiles": True,
                "sqliteIntegrity": True,
                "completePagination": True,
                "readOnlyConnector": True,
            },
        })
        final_dir, _previous = promote_revision("sellsy", revision_id, staging_dir, required)
        total_size = sum(path.stat().st_size for path in final_dir.iterdir() if path.is_file())
        STORE.update(
            run_id,
            status="succeeded",
            stage="completed",
            completed_at=result["completed_at"],
            duration_seconds=result["duration_seconds"],
            file_name=f"Révision Sellsy {revision_id}",
            file_size_bytes=total_size,
            files=result["files"],
            rows=database["records"],
            changed_rows=result["rows"],
            sync_mode="full" if plan["full_sync"] else "incremental",
            auth_mode=result["auth_mode"],
            revision_id=revision_id,
            report_generated_at=None,
            error=None,
        )
    except Exception as error:
        LOGGER.error("Sellsy integration failed: %s", safe_error(error))
        STORE.update(
            run_id,
            status="failed",
            stage="failed",
            completed_at=utc_now(),
            duration_seconds=getattr(error, "duration_seconds", None),
            files=getattr(error, "files", pending_files),
            error=safe_error(error),
        )
    finally:
        if staging_dir and staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


def start_sellsy_job(*, force_full: bool = False) -> tuple[dict | None, str | None]:
    with JOB_LOCK:
        running = latest_running_job()
        if running:
            return None, running["id"]
        record = STORE.create("sellsy_sources")
        record = STORE.update(
            record["id"],
            requested_sync_mode="full" if force_full else "automatic",
        ) or record
        thread = threading.Thread(
            target=run_sellsy_job,
            args=(record["id"], force_full),
            name=f"sellsy-import-{record['id'][:8]}",
            daemon=True,
        )
        thread.start()
        return record, None


def run_report_job(run_id: str) -> None:
    started_at = utc_now()
    STORE.update(run_id, status="running", stage="reporting", started_at=started_at)
    started_monotonic = time.monotonic()
    try:
        report = rebuild_report()
        duration = round(time.monotonic() - started_monotonic, 2)
        STORE.update(
            run_id,
            status="succeeded",
            stage="completed",
            completed_at=utc_now(),
            duration_seconds=duration,
            rows=report["controls"]["selectedTimeRows"],
            file_name="data.json",
            file_size_bytes=REPORT_OUTPUT_PATH.stat().st_size,
            report_generated_at=report["meta"]["generatedAt"],
            error=None,
        )
    except Exception as error:
        LOGGER.error("Reporting rebuild failed: %s", safe_error(error))
        STORE.update(
            run_id,
            status="failed",
            stage="failed",
            completed_at=utc_now(),
            duration_seconds=round(time.monotonic() - started_monotonic, 2),
            error=safe_error(error),
        )


def start_report_job() -> tuple[dict | None, str | None]:
    with JOB_LOCK:
        running = latest_running_job()
        if running:
            return None, running["id"]
        record = STORE.create("report_rebuild")
        thread = threading.Thread(
            target=run_report_job,
            args=(record["id"],),
            name=f"report-rebuild-{record['id'][:8]}",
            daemon=True,
        )
        thread.start()
        return record, None


def run_sync_check_job(run_id: str) -> None:
    refresh_project_env()
    started_at = utc_now()
    STORE.update(run_id, status="running", stage="connectivity", started_at=started_at)
    started_monotonic = time.monotonic()
    try:
        result = run_synchronization_check()
        for source_result in result.get("sources", {}).values():
            if source_result.get("error"):
                source_result["error"] = safe_error(RuntimeError(source_result["error"]))
        divergence_count = sum(
            len(item.get("divergences", []))
            for item in result.get("sources", {}).values()
        )
        STORE.update(
            run_id,
            status="succeeded",
            stage="completed",
            completed_at=utc_now(),
            duration_seconds=round(time.monotonic() - started_monotonic, 2),
            rows=divergence_count,
            file_name="Contrôle Odoo et Sellsy",
            result=result,
            error=None,
        )
    except Exception as error:
        LOGGER.error("Synchronization check failed: %s", safe_error(error))
        STORE.update(
            run_id,
            status="failed",
            stage="failed",
            completed_at=utc_now(),
            duration_seconds=round(time.monotonic() - started_monotonic, 2),
            error=safe_error(error),
        )


def start_sync_check_job() -> tuple[dict | None, str | None]:
    with JOB_LOCK:
        running = latest_running_job()
        if running:
            return None, running["id"]
        record = STORE.create("sync_check")
        thread = threading.Thread(
            target=run_sync_check_job,
            args=(record["id"],),
            name=f"sync-check-{record['id'][:8]}",
            daemon=True,
        )
        thread.start()
        return record, None


def build_admin_status() -> dict:
    refresh_project_env()
    sources = collect_source_freshness(REPO_ROOT, APP_DIR)
    history = STORE.list(200)
    sellsy_config = sellsy_configuration_status()
    counts = {
        "fresh": sum(1 for source in sources if source["status"] == "fresh"),
        "warning": sum(1 for source in sources if source["status"] == "warning"),
        "stale": sum(1 for source in sources if source["status"] == "stale"),
        "missing": sum(1 for source in sources if source["status"] == "missing"),
    }
    return {
        "generated_at": utc_now(),
        "backend": {
            "status": "online",
            "odoo_credentials_configured": bool(
                os.environ.get("ODOO_LOGIN") and os.environ.get("ODOO_PASSWORD")
            ),
            "export_directory": str(
                get_source_dir()
            ),
            "sellsy_configured": sellsy_config["configured"],
            "sellsy_auth_mode": sellsy_config["auth_mode"],
            "sellsy_experience_configured": bool(
                sellsy_config.get("experience_configured")
            ),
            "sellsy_configuration_error": sellsy_config["error"],
            "sellsy_import_directory": str(get_sellsy_source_dir()),
            "running_job_id": latest_running_job().get("id") if latest_running_job() else None,
            "running_odoo_job_id": (
                latest_running_job("odoo_timesheets").get("id")
                if latest_running_job("odoo_timesheets")
                else None
            ),
            "running_sellsy_job_id": (
                latest_running_job("sellsy_sources").get("id")
                if latest_running_job("sellsy_sources")
                else None
            ),
            "active_sources": active_source_provenance(),
        },
        "freshness_counts": counts,
        "sources": sources,
        "history": history,
    }


class ReportingHandler(SimpleHTTPRequestHandler):
    server_version = "MandarineReporting/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIST_DIR), **kwargs)

    def log_message(self, format, *args):
        LOGGER.info("%s - %s", self.address_string(), format % args)

    def _json(self, payload: dict | list, status=HTTPStatus.OK):
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def _error(self, status: HTTPStatus, message: str, **extra):
        self._json({"error": message, **extra}, status)

    def _request_json(self, *, maximum_bytes: int = 16384) -> dict:
        length = min(int(self.headers.get("Content-Length", "0") or 0), maximum_bytes)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("Le corps JSON doit être un objet.")
        return payload

    def _authorized(self) -> bool:
        expected = os.environ.get("ADMIN_API_TOKEN", "")
        if not expected:
            return self.client_address[0] in {"127.0.0.1", "::1"}
        authorization = self.headers.get("Authorization", "")
        supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
        supplied = supplied or self.headers.get("X-Admin-Token", "")
        return bool(supplied) and hmac.compare_digest(supplied, expected)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            payload = {"status": "ok", "time": utc_now()}
            instance_token = os.environ.get("REPORTING_INSTANCE_TOKEN")
            if instance_token:
                payload["instance"] = instance_token
            self._json(payload)
            return
        if parsed.path == "/api/admin/status":
            if not self._authorized():
                self._error(HTTPStatus.UNAUTHORIZED, "Accès administrateur requis")
                return
            self._json(build_admin_status())
            return
        if parsed.path == "/api/admin/integrations":
            if not self._authorized():
                self._error(HTTPStatus.UNAUTHORIZED, "Accès administrateur requis")
                return
            query = parse_qs(parsed.query)
            try:
                limit = int(query.get("limit", ["50"])[0])
            except ValueError:
                limit = 50
            self._json({"history": STORE.list(limit)})
            return
        if parsed.path.startswith("/api/admin/integrations/"):
            if not self._authorized():
                self._error(HTTPStatus.UNAUTHORIZED, "Accès administrateur requis")
                return
            run_id = parsed.path.rsplit("/", 1)[-1]
            record = STORE.get(run_id)
            if not record:
                self._error(HTTPStatus.NOT_FOUND, "Exécution introuvable")
                return
            self._json(record)
            return
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._authorized():
            self._error(HTTPStatus.UNAUTHORIZED, "Accès administrateur requis")
            return

        supported_paths = {
            "/api/admin/integrations/odoo-timesheets",
            "/api/admin/integrations/odoo-full-reconciliation",
            "/api/admin/integrations/sellsy-sources",
            "/api/admin/integrations/sellsy-full-reconciliation",
            "/api/admin/report/rebuild",
            "/api/admin/integrations/sync-check",
        }
        if parsed.path not in supported_paths:
            self._error(HTTPStatus.NOT_FOUND, "Route inconnue")
            return
        if parsed.path == "/api/admin/report/rebuild":
            record, running_id = start_report_job()
            if running_id:
                self._error(
                    HTTPStatus.CONFLICT,
                    "Une actualisation est déjà en cours",
                    running_job_id=running_id,
                )
                return
            self._json(record, HTTPStatus.ACCEPTED)
            return
        if parsed.path == "/api/admin/integrations/sync-check":
            record, running_id = start_sync_check_job()
            if running_id:
                self._error(
                    HTTPStatus.CONFLICT,
                    "Une opération d'administration est déjà en cours",
                    running_job_id=running_id,
                )
                return
            self._json(record, HTTPStatus.ACCEPTED)
            return
        if parsed.path in {
            "/api/admin/integrations/odoo-timesheets",
            "/api/admin/integrations/odoo-full-reconciliation",
        }:
            refresh_project_env()
            if not (os.environ.get("ODOO_LOGIN") and os.environ.get("ODOO_PASSWORD")):
                self._error(
                    HTTPStatus.BAD_REQUEST,
                    "Configuration Odoo incomplète.",
                )
                return
            force_full = parsed.path.endswith("/odoo-full-reconciliation")
            record, running_id = start_odoo_job(force_full=force_full)
            if running_id:
                self._error(
                    HTTPStatus.CONFLICT,
                    "Une acquisition est déjà en cours",
                    running_job_id=running_id,
                )
                return
            self._json(record, HTTPStatus.ACCEPTED)
            return
        if parsed.path in {
            "/api/admin/integrations/sellsy-sources",
            "/api/admin/integrations/sellsy-full-reconciliation",
        }:
            refresh_project_env()
            sellsy_state = sellsy_configuration_status()
            if not sellsy_state["configured"]:
                self._error(HTTPStatus.BAD_REQUEST, "Configuration Sellsy API incomplète.")
                return
            force_full = parsed.path.endswith("/sellsy-full-reconciliation")
            record, running_id = start_sellsy_job(force_full=force_full)
            if running_id:
                self._error(
                    HTTPStatus.CONFLICT,
                    "Une acquisition est déjà en cours",
                    running_job_id=running_id,
                )
                return
            self._json(record, HTTPStatus.ACCEPTED)
            return
    def end_headers(self):
        if self.path.endswith("data.json"):
            self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        super().end_headers()


def main() -> None:
    parser = argparse.ArgumentParser(description="Mandarine reporting backend")
    parser.add_argument("--host", default=os.environ.get("REPORTING_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("REPORTING_PORT", "4174")))
    args = parser.parse_args()

    if args.host not in {"127.0.0.1", "::1", "localhost"} and not os.environ.get(
        "ADMIN_API_TOKEN"
    ):
        raise SystemExit(
            "ADMIN_API_TOKEN est obligatoire lorsque le serveur écoute hors de la machine locale."
        )

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    recovered = STORE.fail_incomplete()
    if recovered:
        LOGGER.warning("%s intégration(s) interrompue(s) ont été clôturées", recovered)
    server = ExclusiveThreadingHTTPServer((args.host, args.port), ReportingHandler)
    LOGGER.info("Reporting disponible sur http://%s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Arrêt du serveur")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
