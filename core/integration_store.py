from __future__ import annotations

import json
import os
import threading
import time
import uuid

from datetime import datetime, timezone
from pathlib import Path


class IntegrationStore:
    """Small JSON-backed job history with atomic writes."""

    def __init__(self, path: Path, max_records: int = 200):
        self.path = path
        self.max_records = max_records
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read_unlocked(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        return data if isinstance(data, list) else []

    def _write_unlocked(self, records: list[dict]) -> None:
        temp_path = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(
                json.dumps(records[: self.max_records], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            for attempt in range(8):
                try:
                    os.replace(temp_path, self.path)
                    return
                except PermissionError:
                    if attempt == 7:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            temp_path.unlink(missing_ok=True)

    def list(self, limit: int = 50) -> list[dict]:
        with self.lock:
            return self._read_unlocked()[: max(1, min(limit, self.max_records))]

    def get(self, run_id: str) -> dict | None:
        with self.lock:
            return next(
                (record for record in self._read_unlocked() if record.get("id") == run_id),
                None,
            )

    def create(self, integration: str, requested_by: str = "admin") -> dict:
        now = datetime.now(timezone.utc).isoformat()
        record = {
            "id": uuid.uuid4().hex,
            "integration": integration,
            "requested_by": requested_by,
            "status": "queued",
            "stage": "queued",
            "rows": 0,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "duration_seconds": None,
            "file_name": None,
            "file_size_bytes": None,
            "last_odoo_id": None,
            "error": None,
        }
        with self.lock:
            records = self._read_unlocked()
            records.insert(0, record)
            self._write_unlocked(records)
        return record

    def update(self, run_id: str, **changes) -> dict | None:
        for sensitive_key in (
            "password",
            "login",
            "client_secret",
            "storage_key",
        ):
            changes.pop(sensitive_key, None)
        with self.lock:
            records = self._read_unlocked()
            updated = None
            for record in records:
                if record.get("id") == run_id:
                    record.update(changes)
                    updated = dict(record)
                    break
            if updated is not None:
                self._write_unlocked(records)
            return updated

    def fail_incomplete(self) -> int:
        """Close jobs interrupted by a previous backend shutdown."""
        now = datetime.now(timezone.utc).isoformat()
        changed = 0
        with self.lock:
            records = self._read_unlocked()
            for record in records:
                if record.get("status") in {"queued", "running"}:
                    record.update(
                        {
                            "status": "failed",
                            "stage": "interrupted",
                            "completed_at": now,
                            "error": "Exécution interrompue par l’arrêt du backend",
                        }
                    )
                    changed += 1
            if changed:
                self._write_unlocked(records)
        return changed
