from __future__ import annotations

import csv
import sys
import tempfile
import unittest

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import openpyxl

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from core import synchronization_check
from core.sellsy_importer import _list_documents
from core.source_database import ODOO_TABLES, build_source_database, read_table
from reporting.backend import server


class SynchronizationPlanTests(unittest.TestCase):
    def _revision(self, *, schema_version: int, experience_configured: bool = False):
        return {
            "revisionId": "sellsy-r1",
            "path": str(APP_DIR),
            "manifestData": {
                "completedAt": "2026-11-15T10:00:00+00:00",
                "storage": {"schemaVersion": schema_version},
                "synchronization": {
                    "cursor": "2026-11-15T10:00:00+00:00",
                    "fullSyncAt": "2026-11-15T10:00:00+00:00",
                    "sellsyExperienceConfigured": experience_configured,
                },
            },
        }

    def test_storage_schema_upgrade_forces_full_reconciliation(self):
        with patch.object(
            server, "active_revision", return_value=self._revision(schema_version=1)
        ), patch.dict(
            server.os.environ,
            {"SOURCE_FULL_SYNC_INTERVAL_DAYS": "7"},
            clear=True,
        ):
            plan = server._synchronization_plan(
                "sellsy", now=datetime(2026, 11, 16, tzinfo=timezone.utc)
            )

        self.assertTrue(plan["full_sync"])
        self.assertTrue(plan["schema_upgrade_required"])

    def test_enabling_experience_sellsy_forces_historical_order_bootstrap(self):
        environment = {
            "SELLSY_EXPERIENCE_CLIENT_ID": "experience-client",
            "SELLSY_EXPERIENCE_CLIENT_SECRET": "experience-secret",
            "SOURCE_FULL_SYNC_INTERVAL_DAYS": "7",
        }
        with patch.object(
            server,
            "active_revision",
            return_value=self._revision(
                schema_version=server.STORAGE_SCHEMA_VERSION,
                experience_configured=False,
            ),
        ), patch.dict(server.os.environ, environment, clear=True):
            plan = server._synchronization_plan(
                "sellsy", now=datetime(2026, 11, 16, tzinfo=timezone.utc)
            )

        self.assertTrue(plan["full_sync"])
        self.assertTrue(plan["experience_bootstrap_required"])

    def test_manual_odoo_reconciliation_bypasses_incremental_cursor(self):
        with patch.object(
            server,
            "active_revision",
            return_value=self._revision(
                schema_version=server.STORAGE_SCHEMA_VERSION,
                experience_configured=True,
            ),
        ), patch.dict(
            server.os.environ,
            {"SOURCE_FULL_SYNC_INTERVAL_DAYS": "7"},
            clear=True,
        ):
            plan = server._synchronization_plan(
                "odoo",
                now=datetime(2026, 11, 16, tzinfo=timezone.utc),
                force_full=True,
            )

        self.assertTrue(plan["full_sync"])
        self.assertIsNone(plan["since"])
        self.assertTrue(plan["requested_full_reconciliation"])

    def test_manual_sellsy_reconciliation_bypasses_incremental_cursor(self):
        with patch.object(
            server,
            "active_revision",
            return_value=self._revision(
                schema_version=server.STORAGE_SCHEMA_VERSION,
                experience_configured=True,
            ),
        ), patch.dict(
            server.os.environ,
            {"SOURCE_FULL_SYNC_INTERVAL_DAYS": "7"},
            clear=True,
        ):
            plan = server._synchronization_plan(
                "sellsy",
                now=datetime(2026, 11, 16, tzinfo=timezone.utc),
                force_full=True,
            )

        self.assertTrue(plan["full_sync"])
        self.assertIsNone(plan["since"])
        self.assertTrue(plan["requested_full_reconciliation"])


class SourceDatabaseTests(unittest.TestCase):
    @staticmethod
    def _write_odoo_directory(root: Path, overrides: dict[str, list[dict]] | None = None) -> None:
        root.mkdir(parents=True, exist_ok=True)
        overrides = overrides or {}
        for table, (filename, primary_key, columns) in ODOO_TABLES.items():
            rows = overrides.get(table)
            if rows is None:
                rows = [{primary_key: "1"}]
            with (root / filename).open("w", encoding="utf-8-sig", newline="") as target:
                writer = csv.DictWriter(target, fieldnames=columns, delimiter=";")
                writer.writeheader()
                writer.writerows(rows)

    def test_initial_import_creates_valid_sqlite_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delta = root / "delta"
            self._write_odoo_directory(
                delta,
                {"odoo_clients": [{"id": "1", "name": "Client initial"}]},
            )
            result = build_source_database(
                "odoo", root / "odoo.sqlite.gz", delta, full_sync=True
            )

            self.assertEqual(result["format"], "sqlite-gzip")
            self.assertEqual(result["counts"]["odoo_clients"], 1)
            self.assertEqual(read_table(root / "odoo.sqlite.gz", "odoo_clients")[0]["name"], "Client initial")

    def test_incremental_import_is_idempotent_and_upserts_insertions_and_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_delta = root / "first"
            self._write_odoo_directory(
                first_delta,
                {"odoo_clients": [{"id": "1", "name": "Avant"}]},
            )
            base = root / "base"
            base.mkdir()
            build_source_database("odoo", base / "odoo.sqlite.gz", first_delta, full_sync=True)

            second_delta = root / "second"
            self._write_odoo_directory(
                second_delta,
                {"odoo_clients": [
                    {"id": "1", "name": "Après"},
                    {"id": "2", "name": "Nouveau"},
                ]},
            )
            destination = root / "next.sqlite.gz"
            result = build_source_database(
                "odoo",
                destination,
                second_delta,
                base_revision_dir=base,
                full_sync=False,
            )
            rows = read_table(destination, "odoo_clients")

            self.assertEqual(result["counts"]["odoo_clients"], 2)
            self.assertEqual({row["id"]: row["name"] for row in rows}, {"1": "Après", "2": "Nouveau"})

            repeated = root / "repeated.sqlite.gz"
            result = build_source_database(
                "odoo",
                repeated,
                second_delta,
                base_revision_dir=base,
                full_sync=False,
            )
            self.assertEqual(result["counts"]["odoo_clients"], 2)

    def test_full_reconciliation_removes_rows_missing_from_remote_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_delta = root / "base-delta"
            self._write_odoo_directory(
                base_delta,
                {"odoo_clients": [{"id": "1", "name": "Supprimé"}, {"id": "2", "name": "Conservé"}]},
            )
            base = root / "base"
            base.mkdir()
            build_source_database("odoo", base / "odoo.sqlite.gz", base_delta, full_sync=True)

            full_delta = root / "full-delta"
            self._write_odoo_directory(
                full_delta,
                {"odoo_clients": [{"id": "2", "name": "Conservé"}]},
            )
            destination = root / "reconciled.sqlite.gz"
            build_source_database(
                "odoo",
                destination,
                full_delta,
                base_revision_dir=base,
                full_sync=True,
            )

            self.assertEqual([row["id"] for row in read_table(destination, "odoo_clients")], ["2"])

    def test_legacy_revision_is_migrated_before_applying_delta(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            self._write_odoo_directory(
                legacy,
                {"odoo_clients": [{"id": "1", "name": "Historique"}]},
            )
            delta = root / "delta"
            self._write_odoo_directory(
                delta,
                {"odoo_clients": [{"id": "2", "name": "Delta"}]},
            )
            destination = root / "migrated.sqlite.gz"
            result = build_source_database(
                "odoo",
                destination,
                delta,
                base_revision_dir=legacy,
                full_sync=False,
            )

            self.assertTrue(result["migrated_legacy"])
            self.assertEqual({row["id"] for row in read_table(destination, "odoo_clients")}, {"1", "2"})


class SynchronizationCheckTests(unittest.TestCase):
    @staticmethod
    def _local_state(source: str) -> dict:
        prefix = "odoo" if source == "odoo" else "sellsy"
        return {
            "revisionId": f"{source}-revision",
            "lastSuccessfulSync": "2026-11-15T10:00:00+00:00",
            "counts": {f"{prefix}_clients" if source == "odoo" else "sellsy_orders": 2},
            "storageFormat": "sqlite",
        }

    def test_control_reports_success_divergence_and_api_error(self):
        with patch.object(synchronization_check, "_local_state", side_effect=self._local_state):
            success = synchronization_check.run_synchronization_check(
                odoo_probe=lambda: {"counts": {"odoo_clients": 2}},
                sellsy_probe=lambda: {"counts": {"sellsy_orders": 2}},
            )
            divergent = synchronization_check.run_synchronization_check(
                odoo_probe=lambda: {"counts": {"odoo_clients": 3}},
                sellsy_probe=lambda: {"counts": {"sellsy_orders": 2}},
            )
            failed = synchronization_check.run_synchronization_check(
                odoo_probe=lambda: (_ for _ in ()).throw(RuntimeError("API indisponible")),
                sellsy_probe=lambda: {"counts": {"sellsy_orders": 2}},
            )

        self.assertEqual(success["status"], "ok")
        self.assertEqual(divergent["status"], "divergence")
        self.assertEqual(failed["status"], "warning")
        self.assertEqual(failed["sources"]["odoo"]["status"], "error")

    def test_legacy_sellsy_workbooks_are_counted_without_dimension_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {}
            for key, filename in (
                ("sellsy_orders", "orders.xlsx"),
                ("sellsy_invoices", "invoices.xlsx"),
                ("sellsy_credit_notes", "credit-notes.xlsx"),
            ):
                path = root / filename
                workbook = openpyxl.Workbook(write_only=True)
                sheet = workbook.create_sheet("Données")
                sheet.append(["ID", "Numéro"])
                sheet.append([1, "A"])
                sheet.append([2, "B"])
                workbook.save(path)
                workbook.close()
                paths[key] = path
            with (
                patch.object(
                    synchronization_check,
                    "active_revision",
                    return_value={"revisionId": "legacy", "manifestData": {}},
                ),
                patch.object(synchronization_check, "active_source_files", return_value=paths),
            ):
                counts = synchronization_check._local_counts("sellsy")
            self.assertEqual(counts, {key: 2 for key in paths})

    def test_sellsy_control_uses_only_bounded_search_requests(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def search_endpoint(self, path, *, filters, query):
                self.calls.append((path, filters, query))
                return {"pagination": {"total": 2}, "data": [{"id": 2, "updated_at": "2026-11-16T08:00:00Z"}]}

        client = FakeClient()
        with patch.object(
            synchronization_check,
            "_local_state",
            return_value={
                "revisionId": "sellsy-r1",
                "lastSuccessfulSync": "2026-11-15T10:00:00+00:00",
                "counts": {"sellsy_orders": 2},
                "storageFormat": "sqlite",
            },
        ), patch.dict(
            synchronization_check.os.environ,
            {
                "SELLSY_EXPERIENCE_CLIENT_ID": "",
                "SELLSY_EXPERIENCE_CLIENT_SECRET": "",
            },
        ):
            result = synchronization_check.probe_sellsy(client)

        self.assertTrue(result["available"])
        self.assertEqual(len(client.calls), 6)
        self.assertTrue(all(path.endswith("/search") for path, _filters, _query in client.calls))
        self.assertTrue(all(query["limit"] == 1 for _path, _filters, query in client.calls))
        self.assertEqual(sum("updated" in filters for _path, filters, _query in client.calls), 3)

    def test_sellsy_control_adds_experience_order_counts(self):
        class FakeClient:
            def __init__(self, total):
                self.total = total
                self.calls = []

            def search_endpoint(self, path, *, filters, query):
                self.calls.append((path, filters, query))
                return {
                    "pagination": {"total": self.total},
                    "data": [{"id": 1, "updated_at": "2026-11-16T08:00:00Z"}],
                }

        primary = FakeClient(2)
        experience = FakeClient(3)
        with patch.object(
            synchronization_check,
            "_local_state",
            return_value={
                "revisionId": "sellsy-r1",
                "lastSuccessfulSync": "2026-11-15T10:00:00+00:00",
                "cursor": "2026-11-15T10:00:00+00:00",
                "counts": {"sellsy_orders": 5},
                "storageFormat": "sqlite",
            },
        ):
            result = synchronization_check.probe_sellsy(primary, experience)

        self.assertEqual(result["counts"]["sellsy_orders"], 5)
        self.assertEqual(result["changesSinceLocal"]["sellsy_orders"], 5)
        self.assertEqual(len(experience.calls), 2)
        self.assertTrue(all(path == "/orders/search" for path, _filters, _query in experience.calls))

    def test_sellsy_incremental_listing_uses_documented_updated_filter(self):
        class FakeClient:
            def __init__(self):
                self.filters = None

            def search_endpoint(self, _path, *, filters, query):
                self.filters = filters
                return {"data": [], "pagination": {"total": 0}}

        client = FakeClient()
        _list_documents(
            client,
            "orders",
            updated_since="2026-11-15T09:50:00+00:00",
        )
        self.assertEqual(
            client.filters["updated"]["start"],
            "2026-11-15T09:50:00+00:00",
        )

    def test_sync_check_route_requires_existing_admin_authorization(self):
        handler = object.__new__(server.ReportingHandler)
        handler.client_address = ("192.0.2.10", 12345)
        with patch.dict(server.os.environ, {"ADMIN_API_TOKEN": "test-secret"}):
            handler.headers = {}
            self.assertFalse(handler._authorized())
            handler.headers = {"Authorization": "Bearer test-secret"}
            self.assertTrue(handler._authorized())


if __name__ == "__main__":
    unittest.main()

