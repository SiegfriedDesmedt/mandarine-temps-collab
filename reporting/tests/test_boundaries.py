from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ApplicationBoundaryTests(unittest.TestCase):
    def test_reporting_server_has_no_unification_import_or_route(self):
        source = (ROOT / "reporting" / "backend" / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("sellsy_odoo_sync", source)
        self.assertNotIn("sellsy-odoo-simulation", source)
        self.assertNotIn("ODOO_QUOTATION_", source)
        self.assertNotIn("create_single_quotation", source)

    def test_reporting_example_has_no_write_configuration(self):
        source = (ROOT / "reporting" / ".env.example").read_text(encoding="utf-8")
        self.assertNotIn("ODOO_QUOTATION_", source)

    def test_unification_application_is_not_packaged_with_reporting(self):
        self.assertFalse((ROOT / "unification").exists())
        self.assertFalse((ROOT / "reporting-app").exists())

    def test_reporting_frontend_exposes_only_three_reports_and_admin(self):
        index = (ROOT / "reporting" / "dist" / "index.html").read_text(encoding="utf-8")
        app = (ROOT / "reporting" / "dist" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "reporting" / "dist" / "styles.css").read_text(encoding="utf-8")

        self.assertEqual(index.count('class="nav-item'), 4)
        for view in ("overview", "collaborators", "projects", "admin"):
            self.assertIn(f'data-view="{view}"', index)
        for removed_token in (
            'data-view="quality"',
            'data-view="sellsy-sync"',
            'data-go-view="quality"',
            "renderQuality",
            "sellsy-odoo-simulation",
            "createOdooQuotation",
            "state.data.quality",
        ):
            self.assertNotIn(removed_token, index + app)
        for removed_style in (".quality-score", ".simulation-kpis", ".quotation-confirm-dialog"):
            self.assertNotIn(removed_style, styles)

    def test_report_contract_keeps_compact_controls_without_quality_view_data(self):
        source = (ROOT / "reporting" / "backend" / "report_builder.py").read_text(encoding="utf-8")
        self.assertIn('"controls": {', source)
        self.assertNotIn('"quality": {', source)
        self.assertNotIn('"unlinkedProjects"', source)
        self.assertNotIn('"fictitiousCostProjects"', source)

    def test_admin_exposes_manual_odoo_full_reconciliation(self):
        backend = (ROOT / "reporting" / "backend" / "server.py").read_text(encoding="utf-8")
        frontend = (ROOT / "reporting" / "dist" / "app.js").read_text(encoding="utf-8")

        route = "/api/admin/integrations/odoo-full-reconciliation"
        self.assertIn(route, backend)
        self.assertIn(route, frontend)
        self.assertIn('id="run-odoo-full-reconciliation"', frontend)

    def test_admin_exposes_manual_sellsy_full_reconciliation(self):
        backend = (ROOT / "reporting" / "backend" / "server.py").read_text(encoding="utf-8")
        frontend = (ROOT / "reporting" / "dist" / "app.js").read_text(encoding="utf-8")

        route = "/api/admin/integrations/sellsy-full-reconciliation"
        self.assertIn(route, backend)
        self.assertIn(route, frontend)
        self.assertIn('id="run-sellsy-full-reconciliation"', frontend)

    def test_unused_topbar_actions_are_removed(self):
        index = (ROOT / "reporting" / "dist" / "index.html").read_text(encoding="utf-8")
        app = (ROOT / "reporting" / "dist" / "app.js").read_text(encoding="utf-8")

        self.assertNotIn('id="export-button"', index)
        self.assertNotIn('aria-label="Notifications"', index)
        self.assertNotIn("function exportCsv", app)
