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

from datetime import datetime, timezone
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
from core.source_revision_store import (
    create_staging_revision,
    file_descriptor,
    promote_revision,
    write_revision_manifest,
)
from .source_freshness import collect_source_freshness


APP_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_DIR.parent
ENV_FILE = APP_DIR / ".env"
LEGACY_ENV_FILE = REPO_ROOT / ".env"
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
    "SELLSY_API_TIMEOUT_SECONDS",
    "SELLSY_API_WORKERS",
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
        "ODOO_PASSWORD",
        "SELLSY_CLIENT_SECRET",
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


def run_odoo_job(run_id: str) -> None:
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

    revision_id, staging_dir = create_staging_revision("odoo")
    try:
        result = export_odoo(progress, destination_dir=staging_dir)
        STORE.update(run_id, stage="validation:manifest", revision_id=revision_id)
        required = [
            "clients.csv", "commandes.csv", "lignes_commandes.csv", "projets.csv",
            "taches.csv", "temps.csv", "couts_fixes.csv", "collaborateurs.csv",
        ]
        files = [file_descriptor(path, relative_to=staging_dir) for path in sorted(staging_dir.iterdir()) if path.is_file()]
        write_revision_manifest(staging_dir, {
            "schemaVersion": 2,
            "source": "odoo",
            "revisionId": revision_id,
            "status": "validated",
            "startedAt": result["started_at"],
            "completedAt": result["completed_at"],
            "records": result["rows"],
            "files": files,
            "checks": {"requiredFiles": True, "readOnlyConnector": True},
        })
        final_dir, _previous = promote_revision("odoo", revision_id, staging_dir, required)
        total_size = sum(path.stat().st_size for path in final_dir.iterdir() if path.is_file())
        STORE.update(
            run_id,
            status="succeeded",
            stage="completed",
            completed_at=result["completed_at"],
            duration_seconds=result["duration_seconds"],
            rows=result["rows"],
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
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


def start_odoo_job() -> tuple[dict | None, str | None]:
    with JOB_LOCK:
        running = latest_running_job()
        if running:
            return None, running["id"]
        record = STORE.create("odoo_timesheets")
        thread = threading.Thread(
            target=run_odoo_job,
            args=(record["id"],),
            name=f"odoo-export-{record['id'][:8]}",
            daemon=True,
        )
        thread.start()
        return record, None


def run_sellsy_job(run_id: str) -> None:
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

    revision_id, staging_dir = create_staging_revision("sellsy")
    try:
        result = import_sellsy(progress, destination_dir=staging_dir)
        STORE.update(run_id, stage="validation:manifest", files=result["files"], revision_id=revision_id)
        required = ["orders.xlsx", "invoices.xlsx", "credit-notes.xlsx"]
        files = [file_descriptor(path, relative_to=staging_dir) for path in sorted(staging_dir.iterdir()) if path.is_file()]
        write_revision_manifest(staging_dir, {
            "schemaVersion": 2,
            "source": "sellsy",
            "revisionId": revision_id,
            "status": "validated",
            "startedAt": result["started_at"],
            "completedAt": result["completed_at"],
            "records": result["rows"],
            "files": files,
            "categories": result["files"],
            "checks": {"requiredFiles": True, "completePagination": True, "readOnlyConnector": True},
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
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


def start_sellsy_job() -> tuple[dict | None, str | None]:
    with JOB_LOCK:
        running = latest_running_job()
        if running:
            return None, running["id"]
        record = STORE.create("sellsy_sources")
        thread = threading.Thread(
            target=run_sellsy_job,
            args=(record["id"],),
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


def build_admin_status() -> dict:
    refresh_project_env()
    sources = collect_source_freshness(REPO_ROOT, APP_DIR)
    history = STORE.list(25)
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
            "/api/admin/integrations/sellsy-sources",
            "/api/admin/report/rebuild",
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
        if parsed.path == "/api/admin/integrations/odoo-timesheets":
            refresh_project_env()
            if not (os.environ.get("ODOO_LOGIN") and os.environ.get("ODOO_PASSWORD")):
                self._error(
                    HTTPStatus.BAD_REQUEST,
                    "Configuration Odoo incomplète.",
                )
                return
            record, running_id = start_odoo_job()
            if running_id:
                self._error(
                    HTTPStatus.CONFLICT,
                    "Une acquisition est déjà en cours",
                    running_job_id=running_id,
                )
                return
            self._json(record, HTTPStatus.ACCEPTED)
            return
        if parsed.path == "/api/admin/integrations/sellsy-sources":
            refresh_project_env()
            sellsy_state = sellsy_configuration_status()
            if not sellsy_state["configured"]:
                self._error(HTTPStatus.BAD_REQUEST, "Configuration Sellsy API incomplète.")
                return
            record, running_id = start_sellsy_job()
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
