from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest
import xmlrpc.client

import openpyxl

from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch


APP_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_DIR.parent
sys.path.insert(0, str(APP_DIR))

from core.env_loader import load_env_file
from core.integration_store import IntegrationStore
from core.odoo_exporter import (
    COLLABORATOR_FIELDS,
    COLLABORATOR_FILENAME,
    EXPECTED_FILENAMES,
    EXPORT_SPECS,
    FIXED_COST_FIELDS,
    FIXED_COST_FILENAME,
    OdooAccessError,
    OdooSchemaError,
    _publish_directory,
    build_ssl_context,
    export_odoo,
    format_decimal_fr,
    get_source_dir,
    relation_identifier,
)
from reporting.backend.report_builder import (
    SOURCE_FILES,
    _is_absence_time,
    _is_internal_project,
    _is_labor_employee,
    build_payload,
    fiscal_period,
    monthly_time_control,
    month_range,
    parse_sellsy_references,
    technical_key,
    working_day_count,
)
from core.sellsy_importer import get_configuration as get_sellsy_configuration, import_sellsy
from core.sellsy_client import _dual_stack_connection
from core.source_database import build_source_database, read_table
from reporting.backend.source_freshness import collect_source_freshness
from configure_odoo import save_env_values


class EnvLoaderTests(unittest.TestCase):
    def test_loads_root_env_without_overriding_existing_variables(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                'ODOO_LOGIN="utilisateur@example.test"\n'
                "ODOO_PASSWORD='mot#de=passe'\n"
                "REPORTING_PORT=4174 # commentaire\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"ODOO_LOGIN": "prioritaire", "ODOO_PASSWORD": "", "REPORTING_PORT": ""},
            ):
                loaded = load_env_file(env_path)
                self.assertEqual(os.environ["ODOO_LOGIN"], "prioritaire")
                self.assertEqual(os.environ["ODOO_PASSWORD"], "")
                self.assertEqual(os.environ["REPORTING_PORT"], "")
                self.assertNotIn("ODOO_LOGIN", loaded)

            with patch.dict(os.environ, {}, clear=True):
                loaded = load_env_file(env_path)
                self.assertEqual(os.environ["ODOO_LOGIN"], "utilisateur@example.test")
                self.assertEqual(os.environ["ODOO_PASSWORD"], "mot#de=passe")
                self.assertEqual(os.environ["REPORTING_PORT"], "4174")
                self.assertEqual(
                    loaded,
                    {"ODOO_LOGIN", "ODOO_PASSWORD", "REPORTING_PORT"},
                )

                env_path.write_text(
                    "ODOO_LOGIN=identifiant-modifie\n"
                    "ODOO_PASSWORD=mot-de-passe-modifie\n",
                    encoding="utf-8",
                )
                load_env_file(env_path, override=True)
                self.assertEqual(os.environ["ODOO_LOGIN"], "identifiant-modifie")
                self.assertEqual(os.environ["ODOO_PASSWORD"], "mot-de-passe-modifie")

    def test_credentials_are_saved_atomically_and_can_be_reloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "ODOO_LOGIN=ancien\nODOO_PASSWORD=ancien\nREPORTING_PORT=4174\n",
                encoding="utf-8",
            )
            save_env_values(
                env_path,
                {
                    "ODOO_LOGIN": "nouveau@example.test",
                    "ODOO_PASSWORD": "mot#de=passe=complexe",
                },
            )
            self.assertFalse(env_path.with_name(".env.tmp").exists())
            with patch.dict(os.environ, {}, clear=True):
                load_env_file(env_path)
                self.assertEqual(os.environ["ODOO_LOGIN"], "nouveau@example.test")
                self.assertEqual(os.environ["ODOO_PASSWORD"], "mot#de=passe=complexe")
                self.assertEqual(os.environ["REPORTING_PORT"], "4174")


class OdooExporterTests(unittest.TestCase):
    @staticmethod
    def _field_type(field: str) -> str:
        if field == "id":
            return "integer"
        if field == "x_num_sellsy":
            return "char"
        if field == "x_bu_projet":
            return "many2one"
        if field.endswith("_ids"):
            return "many2many"
        if field.endswith("_id") or field == "so_line":
            return "many2one"
        if field in {
            "unit_amount",
            "amount",
            "planned_hours",
            "effective_hours",
            "remaining_hours",
            "progress",
        }:
            return "float"
        if field in {"active", "share"}:
            return "boolean"
        return "char"

    @classmethod
    def _available_fields(cls, spec):
        return {
            field: {"type": cls._field_type(field)}
            for field in set(spec.fields) | set(spec.required_fields)
        }

    @staticmethod
    def _row_for_model(model: str) -> dict:
        base = {
            "id": 101,
            "name": "Enregistrement de test",
            "create_date": "2026-10-14 08:00:00",
            "write_date": "2026-10-14 09:00:00",
        }
        if model == "account.analytic.line":
            return {
                **base,
                "date": "2026-10-14",
                "user_id": [999, "Utilisateur fictif"],
                "employee_id": [8, "Collaborateur"],
                "department_id": [20, "Production"],
                "project_id": [12, "Projet"],
                "task_id": [101, "Tâche"],
                "so_line": [501, "Ligne"],
                "unit_amount": 1.5,
                "amount": -1234.5,
                "account_id": [6786, "S00756 - FFESSM"],
                "product_id": [700, "[COUT-ACQ] Coût acquisition projet"],
                "tag_ids": [1],
                "company_id": [1, "Mandarine Academy"],
            }
        if model == "product.product":
            return {
                **base,
                "id": 700,
                "default_code": "COUT-ACQ",
                "name": "Coût acquisition projet",
                "display_name": "[COUT-ACQ] Coût acquisition projet",
            }
        if model == "project.project":
            return {**base, "user_id": [9, "Responsable"], "partner_id": [20, "Client"], "x_bu_projet": [3, "MANDARINE INTERNE"], "sale_line_id": [501, "Ligne"], "sale_order_id": [601, "Commande"]}
        if model == "project.task":
            return {
                **base,
                "project_id": [12, "Projet"],
                "stage_id": [4, "En cours"],
                "user_id": [9, "Responsable"],
                "planned_hours": 10,
                "effective_hours": 7.25,
                "remaining_hours": 2.75,
                "sale_line_id": [501, "Ligne de commande"],
                "sale_order_id": [601, "Commande"],
            }
        if model == "sale.order":
            return {**base, "id": 601, "x_num_sellsy": "BDC-TEST-001", "partner_id": [20, "Client"], "date_order": "2026-10-14 08:00:00", "state": "sale", "project_id": [12, "Projet"], "project_ids": [12], "order_line": [501]}
        if model == "sale.order.line":
            return {**base, "id": 501, "order_id": [601, "Commande"], "project_id": [12, "Projet"], "task_id": [101, "Tâche"], "price_subtotal": 1000}
        if model == "res.partner":
            return {
                **base,
                "company_type": "company",
                "active": True,
                "company_id": [1, "Mandarine"],
            }
        if model == "hr.employee":
            return {
                **base, "id": 8,
                "user_id": [9, "Utilisateur"],
                "department_id": [20, "Production"],
                "active": True,
            }
        if model == "hr.department":
            return {**base, "id": 20, "parent_id": False, "manager_id": [8, "Manager"], "active": True}
        if model == "res.users":
            return {**base, "id": 9, "login": "compte.test", "active": False, "share": False}
        raise AssertionError(f"Modèle inattendu : {model}")

    def _mock_execute_kw(self, models):
        specs_by_model = {spec.model: spec for spec in EXPORT_SPECS}

        def execute_kw(_db, _uid, _password, model, method, _args, _kwargs):
            if method == "fields_get":
                if model in specs_by_model:
                    available = self._available_fields(specs_by_model[model])
                    if model == "account.analytic.line":
                        for field in FIXED_COST_FIELDS:
                            if field not in {"product_default_code", "product_name"}:
                                available[field] = {"type": self._field_type(field)}
                    if model in {"project.project", "project.task"}:
                        available.pop("employee_id", None)
                        available["user_id"] = {"type": "many2one"}
                    return available
                rows = self._row_for_model(model)
                return {field: {"type": self._field_type(field)} for field in rows}
            if method == "search_read":
                return [self._row_for_model(model)]
            self.fail(f"Appel XML-RPC inattendu : {model}.{method}")

        models.execute_kw.side_effect = execute_kw

    def test_ssl_context_keeps_certificate_verification_enabled(self):
        context = build_ssl_context()
        self.assertEqual(context.verify_mode.name, "CERT_REQUIRED")
        self.assertTrue(context.check_hostname)

    def test_source_directory_is_relative_to_application_root(self):
        self.assertEqual(get_source_dir(), (REPO_ROOT / "data" / "exports" / "odoo").resolve())
        self.assertNotEqual(get_source_dir(), Path(get_source_dir().anchor) / "data" / "exports" / "odoo")

    def test_relations_export_only_identifiers_and_empty_relations_stay_empty(self):
        self.assertEqual(relation_identifier([12, "Projet"]), 12)
        self.assertEqual(relation_identifier((7, "Tâche")), 7)
        self.assertEqual(relation_identifier(False), "")
        self.assertEqual(relation_identifier(None), "")
        self.assertEqual(relation_identifier([]), "")

    def test_decimal_format_uses_comma_two_decimals_and_preserves_sign(self):
        self.assertEqual(format_decimal_fr(1.5), "1,50")
        self.assertEqual(format_decimal_fr(Decimal("2.345")), "2,35")
        self.assertEqual(format_decimal_fr(-1234.5), "-1234,50")
        self.assertEqual(format_decimal_fr(0), "0,00")
        self.assertEqual(format_decimal_fr(False), "")
        self.assertEqual(format_decimal_fr(None), "")

    def test_timesheet_export_uses_relations_without_derived_sellsy_reference(self):
        timesheet_spec = next(spec for spec in EXPORT_SPECS if spec.filename == "temps.csv")
        self.assertIn("so_line", timesheet_spec.fields)
        self.assertNotIn("user_id", timesheet_spec.fields)
        self.assertNotIn("x_num_sellsy", timesheet_spec.fields)

    def test_project_and_task_exports_replace_users_with_employees(self):
        project_spec = next(spec for spec in EXPORT_SPECS if spec.filename == "projets.csv")
        task_spec = next(spec for spec in EXPORT_SPECS if spec.filename == "taches.csv")
        self.assertIn("employee_id", project_spec.fields)
        self.assertNotIn("user_id", project_spec.fields)
        for removed in ("date", "date_end", "stage_id", "state"):
            self.assertNotIn(removed, project_spec.fields)
        self.assertIn("employee_id", task_spec.fields)
        self.assertNotIn("user_id", task_spec.fields)
        for removed in ("priority", "date_assign", "stage_id"):
            self.assertNotIn(removed, task_spec.fields)

    def test_missing_credentials_fail_before_network_access(self):
        with patch.dict(os.environ, {"ODOO_LOGIN": "", "ODOO_PASSWORD": ""}):
            with self.assertRaisesRegex(RuntimeError, "ODOO_LOGIN"):
                export_odoo()

    def test_exports_eight_fixed_files_with_pagination_and_archived_context(self):
        common = Mock()
        common.authenticate.return_value = 42
        models = Mock()
        self._mock_execute_kw(models)

        with tempfile.TemporaryDirectory() as directory:
            source_dir = Path(directory) / "sources" / "Odoo"
            environment = {
                "ODOO_LOGIN": "test@example.test",
                "ODOO_PASSWORD": "secret",
                "REPORTING_RUNTIME_DIR": str(Path(directory) / "runtime"),
            }
            with patch.dict(os.environ, environment), patch(
                "core.odoo_exporter.get_source_dir",
                return_value=source_dir,
            ), patch(
                "core.odoo_exporter.xmlrpc.client.ServerProxy",
                side_effect=[common, models],
            ):
                result = export_odoo()

            self.assertEqual(result["rows"], 8)
            self.assertEqual({item.name for item in source_dir.iterdir()}, EXPECTED_FILENAMES)
            self.assertFalse(any(source_dir.parent.glob(".Odoo.staging-*")))
            self.assertFalse(any(source_dir.parent.glob(".Odoo.backup-*")))

            for spec in EXPORT_SPECS:
                path = source_dir / spec.filename
                self.assertEqual(path.read_bytes()[:3], b"\xef\xbb\xbf")
                with path.open("r", encoding="utf-8-sig", newline="") as file:
                    reader = csv.DictReader(file, delimiter=";")
                    row = next(reader)
                    self.assertEqual(reader.fieldnames, list(spec.fields))
                if spec.filename == "temps.csv":
                    self.assertNotIn("user_id", reader.fieldnames)
                    self.assertEqual(row["employee_id"], "8")
                    self.assertEqual(row["project_id"], "12")
                    self.assertEqual(row["task_id"], "101")
                    self.assertEqual(row["so_line"], "501")
                    self.assertEqual(row["unit_amount"], "1,50")
                    self.assertEqual(row["amount"], "-1234,50")
                if spec.filename in {"projets.csv", "taches.csv"}:
                    self.assertNotIn("user_id", reader.fieldnames)
                    self.assertEqual(row["employee_id"], "8")
                if spec.filename == "projets.csv":
                    self.assertEqual(row["x_bu_projet"], "MANDARINE INTERNE")
                if spec.filename == "clients.csv":
                    self.assertEqual(
                        reader.fieldnames,
                        ["id", "name", "active", "company_id", "create_date", "write_date"],
                    )
                if spec.filename == "commandes.csv":
                    self.assertEqual(row["date_order"], "14/10/2026")
                    self.assertEqual(row["state"], "sale")
            with (source_dir / COLLABORATOR_FILENAME).open("r", encoding="utf-8-sig", newline="") as file:
                reader = csv.DictReader(file, delimiter=";")
                collaborators = list(reader)
                self.assertEqual(reader.fieldnames, list(COLLABORATOR_FIELDS))
                self.assertNotIn("record_type", reader.fieldnames)
                self.assertEqual(len(collaborators), 1)
                collaborator = collaborators[0]
                self.assertEqual(collaborator["employee_id"], "8")
                self.assertEqual(collaborator["user_id"], "9")
                self.assertEqual(collaborator["department_id"], "20")
            with (source_dir / FIXED_COST_FILENAME).open("r", encoding="utf-8-sig", newline="") as file:
                reader = csv.DictReader(file, delimiter=";")
                fixed_costs = list(reader)
                self.assertEqual(reader.fieldnames, list(FIXED_COST_FIELDS))
                self.assertEqual(len(fixed_costs), 1)
                self.assertEqual(fixed_costs[0]["product_default_code"], "COUT-ACQ")
                self.assertEqual(fixed_costs[0]["account_id"], "6786")
                self.assertEqual(fixed_costs[0]["amount"], "-1234,50")

            search_calls = [
                call
                for call in models.execute_kw.call_args_list
                if call.args[4] == "search_read"
            ]
            exported_models = {spec.model for spec in EXPORT_SPECS}
            export_search_calls = [call for call in search_calls if call.args[3] in exported_models]
            self.assertEqual(len(search_calls), 11)
            for spec in EXPORT_SPECS:
                expected_domain = [[["id", ">", 0], *[list(condition) for condition in spec.domain]]]
                matching = [
                    call for call in export_search_calls
                    if call.args[3] == spec.model and call.args[5] == expected_domain
                ]
                self.assertTrue(matching, spec.filename)
                call = matching[0]
                self.assertEqual(call.args[6]["order"], "id asc")
                self.assertEqual(call.args[6]["context"], {"active_test": False})

    def test_failed_export_preserves_previous_sources(self):
        common = Mock()
        common.authenticate.return_value = 42
        models = Mock()
        first_spec = EXPORT_SPECS[0]
        missing_amount = self._available_fields(first_spec)
        missing_amount.pop("amount")

        def execute_kw(_db, _uid, _password, model, method, _args, _kwargs):
            if method == "fields_get":
                if model == first_spec.model:
                    return missing_amount
                return {
                    field: {"type": self._field_type(field)}
                    for field in self._row_for_model(model)
                }
            return []

        models.execute_kw.side_effect = execute_kw

        with tempfile.TemporaryDirectory() as directory:
            source_dir = Path(directory) / "sources" / "Odoo"
            source_dir.mkdir(parents=True)
            previous_content = b"publication precedente"
            for filename in EXPECTED_FILENAMES:
                (source_dir / filename).write_bytes(previous_content)

            with patch.dict(
                os.environ,
                {
                    "ODOO_LOGIN": "test@example.test",
                    "ODOO_PASSWORD": "secret",
                    "REPORTING_RUNTIME_DIR": str(Path(directory) / "runtime"),
                },
            ), patch(
                "core.odoo_exporter.get_source_dir",
                return_value=source_dir,
            ), patch(
                "core.odoo_exporter.xmlrpc.client.ServerProxy",
                side_effect=[common, models],
            ):
                with self.assertRaisesRegex(OdooSchemaError, "amount"):
                    export_odoo()

            self.assertTrue(
                all((source_dir / filename).read_bytes() == previous_content for filename in EXPECTED_FILENAMES)
            )
            self.assertFalse(any(source_dir.parent.glob(".Odoo.staging-*")))

    def test_publication_falls_back_when_windows_locks_the_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            source_dir = parent / "Odoo"
            staging_dir = parent / ".Odoo.staging-test"
            source_dir.mkdir()
            staging_dir.mkdir()
            for filename in EXPECTED_FILENAMES:
                (source_dir / filename).write_text("ancien", encoding="utf-8")
                (staging_dir / filename).write_text("nouveau", encoding="utf-8")

            with patch.object(Path, "replace", side_effect=PermissionError("dossier ouvert")):
                _publish_directory(staging_dir, source_dir, Mock())

            self.assertTrue(
                all(
                    (source_dir / filename).read_text(encoding="utf-8") == "nouveau"
                    for filename in EXPECTED_FILENAMES
                )
            )
            self.assertFalse(any(parent.glob(".Odoo.rollback-*")))

    def test_access_error_is_explicit(self):
        common = Mock()
        common.authenticate.return_value = 42
        models = Mock()
        models.execute_kw.side_effect = xmlrpc.client.Fault(
            1,
            "odoo.exceptions.AccessError: You are not allowed to access this document",
        )
        with tempfile.TemporaryDirectory() as directory:
            source_dir = Path(directory) / "sources" / "Odoo"
            with patch.dict(
                os.environ,
                {
                    "ODOO_LOGIN": "test@example.test",
                    "ODOO_PASSWORD": "secret",
                    "REPORTING_RUNTIME_DIR": str(Path(directory) / "runtime"),
                },
            ), patch(
                "core.odoo_exporter.get_source_dir",
                return_value=source_dir,
            ), patch(
                "core.odoo_exporter.xmlrpc.client.ServerProxy",
                side_effect=[common, models],
            ):
                with self.assertRaisesRegex(OdooAccessError, "Droits d'accès"):
                    export_odoo()

    def test_progress_storage_failure_does_not_cancel_export(self):
        common = Mock()
        common.authenticate.return_value = 42
        models = Mock()
        specs_by_model = {spec.model: spec for spec in EXPORT_SPECS}

        def execute_kw(_db, _uid, _password, model, method, _args, _kwargs):
            if method == "fields_get":
                if model in specs_by_model:
                    available = self._available_fields(specs_by_model[model])
                    if model == "account.analytic.line":
                        for field in FIXED_COST_FIELDS:
                            if field not in {"product_default_code", "product_name"}:
                                available[field] = {"type": self._field_type(field)}
                    if model in {"project.project", "project.task"}:
                        available.pop("employee_id", None)
                        available["user_id"] = {"type": "many2one"}
                    return available
                return {field: {"type": self._field_type(field)} for field in self._row_for_model(model)}
            return []

        models.execute_kw.side_effect = execute_kw
        progress = Mock(side_effect=PermissionError("fichier temporairement verrouillé"))
        with tempfile.TemporaryDirectory() as directory:
            source_dir = Path(directory) / "sources" / "Odoo"
            with patch.dict(
                os.environ,
                {
                    "ODOO_LOGIN": "test@example.test",
                    "ODOO_PASSWORD": "secret",
                    "REPORTING_RUNTIME_DIR": str(Path(directory) / "runtime"),
                },
            ), patch(
                "core.odoo_exporter.get_source_dir",
                return_value=source_dir,
            ), patch(
                "core.odoo_exporter.xmlrpc.client.ServerProxy",
                side_effect=[common, models],
            ):
                result = export_odoo(progress)

        self.assertEqual(result["rows"], 0)
        self.assertGreaterEqual(progress.call_count, 8)


class SellsyNetworkTests(unittest.TestCase):
    def test_connection_falls_back_to_ipv6_when_ipv4_is_blocked(self):
        ipv4_socket = Mock()
        ipv4_socket.connect.side_effect = PermissionError("IPv4 blocked")
        ipv6_socket = Mock()

        addresses = [
            (2, 1, 6, "", ("192.0.2.1", 443)),
            (23, 1, 6, "", ("2001:db8::1", 443, 0, 0)),
        ]
        with patch("core.sellsy_client.socket.getaddrinfo", return_value=addresses), patch(
            "core.sellsy_client.socket.socket", side_effect=[ipv4_socket, ipv6_socket]
        ):
            connection = _dual_stack_connection(("login.sellsy.com", 443), timeout=10)

        self.assertIs(connection, ipv6_socket)
        ipv4_socket.close.assert_called_once_with()
        ipv6_socket.connect.assert_called_once_with(("2001:db8::1", 443, 0, 0))


class SellsyApiImporterTests(unittest.TestCase):
    ENV = {"SELLSY_CLIENT_ID": "client", "SELLSY_CLIENT_SECRET": "secret", "SELLSY_API_WORKERS": "2"}

    class FakeClient:
        def __init__(self):
            self.paths = []
            self.searches = []

        def read_endpoint(self, path, *, query=None):
            self.paths.append(path)
            if path.endswith("/custom-fields"):
                return {"data": [{"code": "project", "type": "simple-text", "value": "Projet API"}]}
            raise AssertionError(f"Lecture inattendue hors recherche : {path}")

        def search_endpoint(self, path, *, filters, query=None):
            self.searches.append({"path": path, "filters": filters, "query": query})
            source = path.strip("/").removesuffix("/search")
            current = {
                "id": {"orders": 1, "invoices": 2, "credit-notes": 3}[source],
                "number": f"DOC-{source}", "date": "2026-10-01", "company_name": "Client API",
                "subject": "Objet", "amounts": {"total_excl_tax": "100.00"},
                "related": [{"type": "company", "id": 42}], "status": "accepted",
            }
            if source == "invoices":
                current["parent"] = {"type": "order", "id": 1, "number": "DOC-orders"}
            return {
                "data": [
                    current,
                    {
                        "id": {"orders": 11, "invoices": 12, "credit-notes": 13}[source],
                        "number": f"OLD-{source}", "date": "2020-12-31", "company_name": "Ancien client",
                    },
                ],
                "pagination": {"total": 2, "count": 2, "offset": None},
            }

        def get_order(self, order_id):
            return {
                "id": order_id, "number": "DOC-orders", "date": "2026-10-01",
                "company_name": "Client API", "subject": "Objet",
                "amounts": {"total_excl_tax": "100.00"},
                "related": [{"type": "company", "id": 42}], "status": "accepted",
                "rows": [{"reference": "SKU-1", "name": "Produit", "quantity": 1, "unit_amount": 100}],
            }

        def get_invoice(self, invoice_id):
            return {
                "id": invoice_id, "number": "DOC-invoices", "date": "2026-10-01",
                "company_name": "Client API", "subject": "Objet",
                "amounts": {"total_excl_tax": "100.00"},
                "related": [{"type": "company", "id": 42}], "status": "accepted",
                "parent": {"type": "order", "id": 1, "number": "DOC-orders"},
            }

        def v1_read(self, method, params):
            return {"infos": {"nbpages": 1}, "result": {}}

    def test_configuration_uses_only_sellsy_api_credentials(self):
        with patch.dict(os.environ, self.ENV, clear=True):
            config = get_sellsy_configuration()
        self.assertEqual(config["auth_mode"], "oauth2_client_credentials")
        self.assertEqual(config["workers"], 2)
        self.assertFalse(config["experience_configured"])

    def test_experience_credentials_must_be_complete(self):
        with patch.dict(
            os.environ,
            {**self.ENV, "SELLSY_EXPERIENCE_CLIENT_ID": "experience-client"},
            clear=True,
        ):
            with self.assertRaisesRegex(Exception, "SELLSY_EXPERIENCE_CLIENT_SECRET"):
                get_sellsy_configuration()

    def test_experience_orders_are_merged_in_a_distinct_identifier_namespace(self):
        class ExperienceClient(self.FakeClient):
            def search_endpoint(self, path, *, filters, query=None):
                self.searches.append({"path": path, "filters": filters, "query": query})
                self.asserted_path = path
                return {
                    "data": [{
                        "id": 1,
                        "number": "BDCEXP0006",
                        "date": "2026-10-01",
                        "company_name": "Client Expérience",
                        "subject": "Projet expérience",
                        "amounts": {"total_excl_tax": "500.00"},
                        "related": [{"type": "company", "id": 99}],
                        "status": "accepted",
                    }],
                    "pagination": {"total": 1, "count": 1, "offset": None},
                }

        environment = {
            **self.ENV,
            "SELLSY_EXPERIENCE_CLIENT_ID": "experience-client",
            "SELLSY_EXPERIENCE_CLIENT_SECRET": "experience-secret",
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, environment, clear=True
        ):
            destination = Path(directory) / "sellsy"
            experience = ExperienceClient()
            result = import_sellsy(
                destination_dir=destination,
                client=self.FakeClient(),
                experience_client=experience,
            )
            workbook = openpyxl.load_workbook(
                destination / "orders.xlsx", read_only=True, data_only=True
            )
            try:
                values = list(workbook.active.iter_rows(values_only=True))
                headers = list(values[0])
                identifiers = {str(row[headers.index("ID")]) for row in values[1:]}
                numbers = {str(row[headers.index("Numéro")]) for row in values[1:]}
            finally:
                workbook.close()

            storage = Path(directory) / "sellsy.sqlite.gz"
            build_source_database(
                "sellsy", storage, destination, full_sync=True
            )
            stored_identifiers = {
                row["id"] for row in read_table(storage, "sellsy_orders")
            }

        self.assertEqual(identifiers, {"1", "-1"})
        self.assertEqual(stored_identifiers, {"1", "-1"})
        self.assertIn("BDCEXP0006", numbers)
        self.assertEqual(experience.asserted_path, "/orders/search")
        orders_result = next(item for item in result["files"] if item["source"] == "orders")
        self.assertEqual(orders_result["experience_records"], 1)

    def test_three_api_categories_are_published_as_workbooks_only(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, self.ENV, clear=True):
            destination = Path(directory) / "sellsy"
            client = self.FakeClient()
            result = import_sellsy(destination_dir=destination, client=client)
            self.assertEqual(result["rows"], 3)
            self.assertTrue(all(item["status"] == "succeeded" for item in result["files"]))
            self.assertEqual(
                {item["path"] for item in client.searches},
                {"/orders/search", "/invoices/search", "/credit-notes/search"},
            )
            self.assertTrue(all(item["filters"] == {"date": {"start": "2021-01-01"}} for item in client.searches))
            self.assertFalse(any("/11/" in path or "/12/" in path or "/13/" in path for path in client.paths))
            for name in ("orders", "invoices", "credit-notes"):
                self.assertTrue((destination / f"{name}.xlsx").is_file())
                self.assertFalse((destination / f"{name}.json.gz").exists())
            self.assertFalse((destination / "orders-details.json.gz").exists())
            workbook = openpyxl.load_workbook(destination / "invoices.xlsx", read_only=True, data_only=True)
            try:
                values = list(workbook.active.iter_rows(values_only=True))
                headers = list(values[0])
                self.assertIn("Commande_ID", headers)
                self.assertIn("Commande_Numero", headers)
                self.assertEqual(values[1][headers.index("Commande_ID")], "1")
                self.assertEqual(values[1][headers.index("Commande_Numero")], "DOC-orders")
            finally:
                workbook.close()

    def test_invoice_linked_through_delivery_is_mapped_to_native_order(self):
        class DeliveryClient(self.FakeClient):
            def search_endpoint(self, path, *, filters, query=None):
                payload = super().search_endpoint(path, filters=filters, query=query)
                if path == "/invoices/search":
                    payload["data"][0]["parent"] = {"type": "delivery", "id": 10}
                return payload

            def v1_read(self, method, params):
                self.v1_call = (method, params)
                return {
                    "infos": {"nbpages": 1},
                    "result": {"10": {"docid": "10", "parentid": "1", "displayedDate": "2026-09-30"}},
                }

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, self.ENV, clear=True):
            destination = Path(directory) / "sellsy"
            client = DeliveryClient()
            import_sellsy(destination_dir=destination, client=client)
            workbook = openpyxl.load_workbook(destination / "invoices.xlsx", read_only=True, data_only=True)
            try:
                values = list(workbook.active.iter_rows(values_only=True))
                headers = list(values[0])
                self.assertEqual(values[1][headers.index("Commande_ID")], "1")
            finally:
                workbook.close()
            self.assertEqual(client.v1_call[0], "Document.getList")


class IntegrationStoreTests(unittest.TestCase):
    def test_create_update_and_recover(self):
        with tempfile.TemporaryDirectory() as directory:
            store = IntegrationStore(Path(directory) / "history.json")
            record = store.create("odoo_timesheets")
            self.assertEqual(record["status"], "queued")
            updated = store.update(record["id"], status="running", rows=1000)
            self.assertEqual(updated["rows"], 1000)
            self.assertEqual(store.fail_incomplete(), 1)
            recovered = store.get(record["id"])
            self.assertEqual(recovered["status"], "failed")
            self.assertEqual(recovered["stage"], "interrupted")

    def test_update_never_persists_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            store = IntegrationStore(Path(directory) / "history.json")
            record = store.create("sellsy_sources")
            updated = store.update(
                record["id"],
                status="running",
                client_secret="client-secret",
                storage_key="storage-secret",
            )
            serialized = (Path(directory) / "history.json").read_text(encoding="utf-8")
            self.assertNotIn("client-secret", serialized)
            self.assertNotIn("storage-secret", serialized)
            self.assertNotIn("client_secret", updated)


class SourceFreshnessTests(unittest.TestCase):
    def test_expected_sources_are_profiled(self):
        sources = collect_source_freshness(REPO_ROOT, APP_DIR)
        identifiers = {source["id"] for source in sources}
        self.assertEqual(
            identifiers,
            {
                "sellsy_api_invoices",
                "sellsy_api_orders",
                "sellsy_api_credit_notes",
                "odoo_timesheets_api",
                "odoo_fixed_costs_api",
                "odoo_projects_api",
                "odoo_clients_api",
                "odoo_tasks_api",
                "odoo_orders_api",
                "odoo_order_lines_api",
                "odoo_collaborators_api",
            },
        )


class ReportBuilderTests(unittest.TestCase):
    @staticmethod
    def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=fieldnames, delimiter=";")
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _write_sellsy(path: Path, headers: list[str], rows: list[list]) -> None:
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        workbook.save(path)
        workbook.close()

    def test_fiscal_period_starts_in_july(self):
        self.assertEqual(
            fiscal_period(date(2026, 10, 14)),
            (date(2026, 7, 1), date(2027, 6, 30)),
        )
        self.assertEqual(
            fiscal_period(date(2026, 3, 1)),
            (date(2025, 7, 1), date(2026, 6, 30)),
        )

    def test_month_range_spans_calendar_years(self):
        self.assertEqual(
            month_range(date(2026, 11, 1), date(2027, 2, 28)),
            ["2026-11", "2026-12", "2027-01", "2027-02"],
        )

    def test_monthly_time_control_uses_7h40_weekdays_and_thresholds(self):
        self.assertEqual(working_day_count(date(2026, 9, 1), date(2026, 9, 30)), 22)
        conform = monthly_time_control(
            "2026-09",
            161,
            as_of=date(2026, 10, 14),
            first_activity=date(2020, 1, 1),
            last_activity=None,
            active=True,
        )
        watch = monthly_time_control(
            "2026-09",
            150,
            as_of=date(2026, 10, 14),
            first_activity=date(2020, 1, 1),
            last_activity=None,
            active=True,
        )
        insufficient = monthly_time_control(
            "2026-09",
            100,
            as_of=date(2026, 10, 14),
            first_activity=date(2020, 1, 1),
            last_activity=None,
            active=True,
        )
        self.assertEqual(conform["expectedHours"], 168.7)
        self.assertEqual(conform["status"], "conform")
        self.assertEqual(watch["status"], "watch")
        self.assertEqual(insufficient["status"], "insufficient")

    def test_monthly_time_control_prorates_current_month_and_ignores_future(self):
        current = monthly_time_control(
            "2026-10",
            76.7,
            as_of=date(2026, 10, 14),
            first_activity=date(2020, 1, 1),
            last_activity=None,
            active=True,
        )
        future = monthly_time_control(
            "2026-11",
            0,
            as_of=date(2026, 10, 14),
            first_activity=date(2020, 1, 1),
            last_activity=None,
            active=True,
        )
        self.assertEqual(current["expectedHours"], 76.7)
        self.assertEqual(current["evaluation"], "elapsed")
        self.assertEqual(future["status"], "unevaluated")
        self.assertEqual(future["evaluation"], "future")

    def test_monthly_time_control_prorates_arrival_and_departure_dates(self):
        arrival = monthly_time_control(
            "2026-09",
            92,
            as_of=date(2026, 10, 14),
            first_activity=date(2026, 9, 15),
            last_activity=None,
            active=True,
        )
        departure = monthly_time_control(
            "2026-09",
            107.4,
            as_of=date(2026, 10, 14),
            first_activity=date(2020, 1, 1),
            last_activity=date(2026, 9, 18),
            active=False,
        )
        self.assertEqual(arrival["expectedHours"], 92.0)
        self.assertEqual(arrival["status"], "conform")
        self.assertEqual(departure["expectedHours"], 107.3)
        self.assertEqual(departure["status"], "conform")

    def test_monthly_time_control_handles_zero_capacity_and_overtime(self):
        zero_capacity = monthly_time_control(
            "2026-10",
            0,
            as_of=date(2026, 10, 3),
            first_activity=date(2026, 10, 3),
            last_activity=None,
            active=True,
        )
        high_but_conform = monthly_time_control(
            "2026-09",
            182,
            as_of=date(2026, 10, 14),
            first_activity=date(2020, 1, 1),
            last_activity=None,
            active=True,
        )
        overtime = monthly_time_control(
            "2026-09",
            183,
            as_of=date(2026, 10, 14),
            first_activity=date(2020, 1, 1),
            last_activity=None,
            active=True,
        )
        self.assertEqual(zero_capacity["expectedHours"], 0.0)
        self.assertEqual(zero_capacity["status"], "unevaluated")
        self.assertEqual(high_but_conform["status"], "conform")
        self.assertLessEqual(high_but_conform["coverageRate"], 108)
        self.assertEqual(overtime["status"], "watch")
        self.assertGreater(overtime["coverageRate"], 108)

    def test_absence_time_classification_uses_existing_labels(self):
        self.assertTrue(_is_absence_time("Mandarine Interne", "Congés payés"))
        self.assertTrue(_is_absence_time("ABSENCE / RTT", ""))
        self.assertFalse(_is_absence_time("Client Alpha", "Production"))

    def test_internal_project_classification_uses_odoo_business_fields(self):
        self.assertTrue(_is_internal_project({"name": "PROJET INTERNE", "client": "Client", "x_bu_projet": ""}))
        self.assertTrue(_is_internal_project({"name": "Projet", "client": "Mandarine Academy", "x_bu_projet": ""}))
        self.assertTrue(_is_internal_project({"name": "Projet", "client": "Client", "x_bu_projet": "MANDARINE INTERNE"}))
        self.assertFalse(_is_internal_project({"name": "MANDEXP - Client", "client": "Client externe", "x_bu_projet": "Mandarine Expérience"}))

    def test_sellsy_references_are_exact_text_keys_and_support_explicit_lists(self):
        self.assertEqual(parse_sellsy_references(" BDC-001 | 00042 "), ["bdc-001", "00042"])
        self.assertEqual(technical_key(" 00042 "), "00042")

    def test_legacy_pseudo_employees_are_recognized_but_retained_in_reporting(self):
        self.assertTrue(_is_labor_employee({"name": "Alice Martin", "department": "Production"}))
        for employee in (
            {"name": "Coût Acquisition (30% du CA)", "department": "Vente"},
            {"name": "Frais (déplacements/Hotels/Autres)", "department": "Coûts Fixes"},
            {"name": "Cout Licence", "department": "Coût Licence"},
            {"name": "Ressource Externe 100", "department": "Ressource Externe"},
        ):
            with self.subTest(employee=employee["name"]):
                self.assertTrue(_is_labor_employee(employee))

    def test_payload_links_clients_and_orders_with_technical_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {key: root / Path(path).name for key, path in SOURCE_FILES.items()}
            self._write_csv(paths["odoo_projects"], ["id", "name", "employee_id", "partner_id", "analytic_account_id", "active", "sale_line_id", "sale_order_id"], [
                {"id": "p1", "name": "Projet Odoo final", "employee_id": "e1", "partner_id": "c1", "analytic_account_id": "aa1", "active": "true", "sale_line_id": "l1", "sale_order_id": "so1"},
                {"id": "p2", "name": "INTERNE ARCHIVE", "employee_id": "e1", "partner_id": "c1", "analytic_account_id": "aa2", "active": "false", "sale_line_id": "", "sale_order_id": ""},
            ])
            self._write_csv(paths["odoo_clients"], ["id", "name", "active", "company_id", "create_date", "write_date"], [
                {"id": "c1", "name": "Client Odoo Alpha", "active": "true", "company_id": "1", "create_date": "2020-01-01", "write_date": "2026-01-01"},
            ])
            self._write_csv(paths["odoo_collaborators"], ["employee_id", "employee_name", "user_id", "user_name", "department_id", "department_name"], [
                {"employee_id": "e1", "employee_name": "Collaborateur", "user_id": "u1", "user_name": "Responsable", "department_id": "d1", "department_name": "Production"},
                {"employee_id": "388", "employee_name": "Coût Acquisition (30% du CA)", "user_id": "", "user_name": "", "department_id": "d2", "department_name": "Coût Acquisition - Vente"},
            ])
            self._write_csv(paths["odoo_orders"], ["id", "name", "x_num_sellsy", "partner_id", "date_order", "state", "project_id", "project_ids"], [
                {"id": "so1", "name": "S001", "x_num_sellsy": "BDC-001 | BDC-OLD", "partner_id": "reseller", "date_order": "2026-09-01", "state": "sale", "project_id": "p1", "project_ids": "p1"},
            ])
            self._write_csv(paths["odoo_order_lines"], ["id", "order_id", "name", "price_subtotal", "project_id", "task_id"], [
                {"id": "l1", "order_id": "so1", "name": "Prestation", "price_subtotal": "1000,00", "project_id": "p1", "task_id": "task1"},
            ])
            self._write_csv(paths["odoo_tasks"], ["id", "name", "project_id", "employee_id", "planned_hours", "sale_line_id", "sale_order_id"], [
                {"id": "task1", "name": "Production", "project_id": "p1", "employee_id": "e1", "planned_hours": "5,00", "sale_line_id": "l1", "sale_order_id": "so1"},
            ])
            self._write_csv(
                paths["odoo_times"],
                ["id", "date", "employee_id", "department_id", "project_id", "task_id", "so_line", "unit_amount", "amount"],
                [{
                    "id": "t1", "date": "2026-10-01", "employee_id": "e1",
                    "department_id": "d1", "project_id": "p1", "task_id": "task1", "so_line": "l1", "unit_amount": "8,00", "amount": "-800,00",
                }, {
                    "id": "t2", "date": "2026-10-02", "employee_id": "e1",
                    "department_id": "d1", "project_id": "p1", "task_id": "task1", "so_line": "l1", "unit_amount": "2,00", "amount": "-200,00",
                }, {
                    "id": "t3", "date": "2026-10-03", "employee_id": "e1",
                    "department_id": "d1", "project_id": "p1", "task_id": "task1", "so_line": "l1", "unit_amount": "-1,00", "amount": "100,00",
                }, {
                    "id": "t4", "date": "2020-08-15", "employee_id": "e1",
                    "department_id": "d1", "project_id": "p2", "task_id": "", "so_line": "", "unit_amount": "1,00", "amount": "-10,00",
                }, {
                    "id": "t5", "date": "2020-08-16", "employee_id": "388",
                    "department_id": "d2", "project_id": "p1", "task_id": "task1", "so_line": "l1", "unit_amount": "50,00", "amount": "-50,00",
                }],
            )
            self._write_csv(
                paths["odoo_fixed_costs"],
                ["id", "date", "account_id", "project_id", "product_id", "product_default_code", "product_name", "unit_amount", "amount", "name", "tag_ids", "company_id", "create_date", "write_date"],
                [
                    {"id": "f1", "date": "2026-10-01", "account_id": "aa1", "project_id": "", "product_id": "700", "product_default_code": "COUT-ACQ", "product_name": "Acquisition", "unit_amount": "0,00", "amount": "-132,00", "name": "Acquisition", "tag_ids": "1", "company_id": "1"},
                    {"id": "f2", "date": "2026-10-02", "account_id": "aa1", "project_id": "p1", "product_id": "701", "product_default_code": "COUT-INT", "product_name": "Coûts internes", "unit_amount": "0,00", "amount": "-66,00", "name": "Interne", "tag_ids": "2", "company_id": "1"},
                    {"id": "f3", "date": "2026-10-03", "account_id": "aa1", "project_id": "", "product_id": "702", "product_default_code": "COUT-LIC", "product_name": "Licences", "unit_amount": "0,00", "amount": "-9,00", "name": "Licence", "tag_ids": "", "company_id": "1"},
                    {"id": "f4", "date": "2026-10-04", "account_id": "aa1", "project_id": "", "product_id": "700", "product_default_code": "COUT-ACQ", "product_name": "Acquisition", "unit_amount": "0,00", "amount": "7,00", "name": "Régularisation", "tag_ids": "", "company_id": "1"},
                    {"id": "f5", "date": "2020-08-10", "account_id": "aa1", "project_id": "", "product_id": "700", "product_default_code": "COUT-ACQ", "product_name": "Acquisition", "unit_amount": "0,00", "amount": "-30,00", "name": "Historique", "tag_ids": "", "company_id": "1"},
                    {"id": "f6", "date": "2026-10-05", "account_id": "aa1", "project_id": "", "product_id": "999", "product_default_code": "AUTRE", "product_name": "Autre", "unit_amount": "0,00", "amount": "-100,00", "name": "Hors périmètre", "tag_ids": "", "company_id": "1"},
                ],
            )
            common_headers = [
                "ID", "Numéro", "Date", "Client", "Objet", "Montant HT",
                "commercialmandarine", "bu-mbs", "cpmbs", "type-vente", "idplf", "project",
            ]
            order_headers = ["ID", "Numéro", "Date", "Client_id", *common_headers[3:]]
            invoice_headers = [*common_headers, "Commande_ID", "Commande_Numéro"]
            self._write_sellsy(paths["sellsy_orders"], order_headers, [
                ["o1", "BDC-001", "2026-10-02", "10", "Revendeur Sellsy", "", 1000, "", "Dileap Services", "", "", 0, "Projet commercial exact"],
                ["o2", "BDC-002", "2026-10-03", "20", "Client sans commande Odoo", "", 500, "", "Dileap Services", "", "", 0, "Projet absent"],
                ["o3", "BDC-OLD", "2025-06-15", "10", "Revendeur historique", "", 1200, "", "Dileap Services", "", "", 0, "Projet commercial exact"],
                ["o4", "BDC-PRE-2021", "2020-12-31", "20", "Ancien client", "", 900, "", "Dileap Services", "", "", 0, "Projet absent"],
            ])
            self._write_sellsy(paths["sellsy_invoices"], invoice_headers, [
                ["i1", "FAC-001", "2026-10-04", "Revendeur Sellsy", "", 600, "", "Dileap Services", "", "", 0, "Projet volontairement différent", "o1", "BDC-001"],
                ["i2", "FAC-HIST", "2020-08-04", "Client historique", "", 100, "", "Dileap Services", "", "", 0, "", "", ""],
            ])
            self._write_sellsy(paths["sellsy_credit_notes"], common_headers, [
                ["a1", "A-001", "2026-10-05", "Revendeur Sellsy", "", 100, "", "Dileap Services", "", "", 0, "Projet commercial exact"],
                ["a2", "A-PRE-2021", "2020-12-31", "Ancien client", "", 50, "", "Dileap Services", "", "", 0, ""],
            ])

            payload = build_payload(as_of=date(2026, 10, 14), source_files=paths)

            self.assertEqual(payload["meta"]["sellsyDocumentStartDate"], "2021-01-01")
            self.assertEqual(payload["meta"]["reportSchemaVersion"], 4)
            self.assertEqual(payload["meta"]["businessRules"]["timesheetControl"]["dailyHours"], 7.67)
            self.assertEqual(payload["meta"]["rulesetVersion"], 4)
            self.assertEqual(
                payload["meta"]["businessRules"]["timesheetControl"]["overageWatchThreshold"],
                108.0,
            )
            current_month = next(
                row for row in payload["monthlyBu"]
                if row["month"] == "2026-10" and row["bu"] == "Dileap Services"
            )
            self.assertEqual(current_month["orders"], 1500.0)
            self.assertEqual(current_month["invoiceAmount"], 600.0)
            self.assertEqual(current_month["creditAmount"], 100.0)
            self.assertEqual(current_month["revenue"], 500.0)
            self.assertEqual(current_month["laborCost"], 900.0)
            self.assertEqual(current_month["otherCosts"], 200.0)
            self.assertEqual(current_month["totalCost"], 1100.0)
            self.assertEqual(len(payload["finance"]["invoiceStatuses"]), 1)
            sellsy_source_counts = {
                source["name"]: source["records"]
                for source in payload["sources"]
                if source["system"] == "Sellsy"
            }
            self.assertEqual(sellsy_source_counts, {
                "Commandes Sellsy": 3,
                "Factures Sellsy": 1,
                "Avoirs Sellsy": 1,
            })
            self.assertNotIn("quality", payload)
            self.assertEqual(payload["controls"]["unmatchedOrderCount"], 1)
            self.assertEqual(payload["controls"]["orderReferenceMatchRate"], 66.7)
            self.assertEqual(payload["controls"]["fictitiousCostProjectCount"], 1)
            self.assertEqual(payload["controls"]["fixedCostLinkageAnomalyCount"], 0)
            self.assertEqual(payload["controls"]["unlinkedProjectCount"], 1)
            self.assertNotIn("unlinkedProjects", payload["controls"])
            self.assertNotIn("fictitiousCostProjects", payload["controls"])
            self.assertEqual(payload["projects"][0]["client"], "Client Odoo Alpha")
            self.assertEqual(payload["projects"][0]["orderCount"], 2)
            self.assertEqual(payload["projects"][0]["orderAmount"], 2200.0)
            self.assertEqual(payload["projects"][0]["invoiceAmount"], 600.0)
            self.assertEqual(payload["projects"][0]["creditAmount"], 100.0)
            self.assertEqual(payload["projects"][0]["netRevenue"], 500.0)
            self.assertEqual(payload["projects"][0]["laborMargin"], -450.0)
            self.assertTrue(payload["projects"][0]["odooCorrectionRequired"])
            self.assertEqual(payload["projects"][0]["fictitiousTimeCost"], 50.0)
            self.assertEqual(payload["projects"][0]["fixedCosts"], 230.0)
            current_fixed_costs = sum(
                month["fixedCosts"]
                for month in payload["projects"][0]["months"]
                if month["month"] in {"2026-07", "2026-08", "2026-09", "2026-10"}
            )
            self.assertEqual(current_fixed_costs, 200.0)
            self.assertEqual(
                payload["projects"][0]["fixedCostCategories"],
                {
                    "COUT-ACQ": 155.0, "COUT-FRAIS": 0, "COUT-INT": 66.0,
                    "COUT-MWIZ": 0, "COUT-LIC": 9.0, "COUT-EXT": 0,
                },
            )
            self.assertEqual(payload["projects"][0]["tasks"][0]["hours"], 59.0)
            self.assertEqual(payload["projects"][0]["tasks"][0]["plannedHours"], 5.0)
            self.assertEqual(payload["projects"][0]["tasks"][0]["status"], "OVERRUN")
            department_costs = {
                row["name"]: row["cost"]
                for row in payload["projects"][0]["departmentCosts"]
            }
            self.assertEqual(department_costs["Production"], 900.0)
            self.assertIn(50.0, department_costs.values())
            self.assertEqual(
                [period["value"] for period in payload["meta"]["fiscalPeriods"]],
                ["2024-2025", "2026-2027"],
            )
            current_period = payload["meta"]["fiscalPeriods"][-1]
            self.assertEqual(len(current_period["fullMonths"]), 12)
            self.assertEqual(current_period["fullMonths"][0], "2026-07")
            self.assertEqual(current_period["fullMonths"][-1], "2027-06")
            collaborator = next(row for row in payload["collaborators"] if row["name"] == "Collaborateur")
            october = next(row for row in collaborator["timeControlMonths"] if row["month"] == "2026-10")
            self.assertEqual(october["hours"], 9.0)
            self.assertEqual(october["status"], "insufficient")
            current_months = {
                row["month"]: row for row in collaborator["timeControlMonths"]
                if row["month"] in current_period["fullMonths"]
            }
            self.assertEqual(len(current_months), 12)
            self.assertEqual(sum(row["hours"] for row in current_months.values()), 9.0)
            self.assertEqual(current_months["2026-10"]["clientHours"], 9.0)
            self.assertEqual(payload["meta"]["months"][0], "2020-07")


if __name__ == "__main__":
    unittest.main()
