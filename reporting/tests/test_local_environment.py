from __future__ import annotations

import json
import io
import os
import sys
import tempfile
import unittest

from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_DIR.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(APP_DIR))

from backend.report_builder import (
    ReportBuildError,
    rebuild_report,
    report_build_lock,
    validate_payload,
    write_payload,
)
from tools import local_environment


def valid_payload(marker: str = "one") -> dict:
    return {
        "meta": {
            "generatedAt": marker,
            "periodStart": "2026-07-01",
            "periodEnd": "2027-06-30",
            "dataThrough": "2026-10-16",
            "fiscalPeriods": ["2026-2027"],
        },
        "summary": {"projects": 1, "hours": 2.0},
        "monthlyBu": [],
        "collaborators": [],
        "projects": [],
        "sources": [{} for _ in range(11)],
        "alerts": [],
        "controls": {"selectedTimeRows": 0},
    }


class ReliableLocalDeploymentTests(unittest.TestCase):
    def test_server_fingerprint_includes_shared_core_modules(self):
        self.assertIn(
            REPO_ROOT / "core" / "sellsy_client.py",
            local_environment.server_code_paths(),
        )

    def test_backend_code_change_invalidates_running_server_state(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"REPORTING_RUNTIME_DIR": directory}
        ), patch.object(local_environment, "server_code_fingerprint", return_value="current"):
            state_path = Path(directory) / "local-server.json"
            state_path.write_text(
                json.dumps({"code_fingerprint": "previous"}), encoding="utf-8"
            )
            self.assertFalse(local_environment.server_code_is_current())
            state_path.write_text(
                json.dumps({"code_fingerprint": "current"}), encoding="utf-8"
            )
            self.assertTrue(local_environment.server_code_is_current())

    def test_normal_startup_reuses_a_healthy_server(self):
        with (
            patch.object(local_environment, "validate_published_report"),
            patch.object(local_environment, "server_configuration", return_value=("127.0.0.1", 4174)),
            patch.object(local_environment, "is_healthy", return_value=True),
            patch.object(local_environment, "server_code_is_current", return_value=True),
            patch.object(local_environment, "owned_server_pid", return_value=1234),
            patch("subprocess.Popen") as popen,
        ):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(local_environment.start_local(), 1234)
            popen.assert_not_called()

    def test_deploy_verifies_rebuilds_once_then_restarts(self):
        calls = []
        payload = valid_payload()
        with (
            patch.object(local_environment, "verify_sources", side_effect=lambda: calls.append("verify")),
            patch.object(local_environment, "run_tests", side_effect=lambda: calls.append("tests")),
            patch.object(local_environment, "refresh_local", side_effect=lambda: (calls.append("refresh"), payload)[1]),
            patch.object(local_environment, "stop_local", side_effect=lambda: calls.append("stop")),
            patch.object(local_environment, "start_local", side_effect=lambda: calls.append("start")),
        ):
            with redirect_stdout(io.StringIO()):
                self.assertIs(local_environment.deploy_local(), payload)
        self.assertEqual(calls, ["verify", "tests", "refresh", "stop", "start"])

    def test_two_successive_deploys_each_rebuild_and_restart_once(self):
        with (
            patch.object(local_environment, "verify_sources") as verify,
            patch.object(local_environment, "run_tests") as tests,
            patch.object(local_environment, "refresh_local", return_value=valid_payload()) as refresh,
            patch.object(local_environment, "stop_local") as stop,
            patch.object(local_environment, "start_local") as start,
            redirect_stdout(io.StringIO()),
        ):
            local_environment.deploy_local()
            local_environment.deploy_local()
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(tests.call_count, 2)
        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(stop.call_count, 2)
        self.assertEqual(start.call_count, 2)

    def test_rebuild_publishes_a_complete_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "dist" / "data.json"
            state = {"source": {"sha256": "same"}}
            with (
                patch.dict(os.environ, {"REPORTING_RUNTIME_DIR": str(Path(directory) / "runtime")}),
                patch("backend.report_builder.build_payload", return_value=valid_payload()),
                patch("backend.report_builder.source_fingerprints", side_effect=[state, state]),
            ):
                rebuild_report(output_path=output)
            validate_payload(json.loads(output.read_text(encoding="utf-8")))

    def test_refresh_does_not_modify_source_fingerprints(self):
        source_state = {"eleven_sources": {"sha256": "unchanged"}}
        with (
            patch.object(local_environment, "verify_sources", side_effect=[source_state, source_state]),
            patch.object(local_environment, "rebuild_report", return_value=valid_payload()),
            patch.object(local_environment, "validate_published_report", return_value=valid_payload()),
        ):
            local_environment.refresh_local()

    def test_source_change_during_rebuild_preserves_previous_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "dist" / "data.json"
            write_payload(valid_payload("previous"), output)
            previous = output.read_bytes()
            with (
                patch.dict(os.environ, {"REPORTING_RUNTIME_DIR": str(Path(directory) / "runtime")}),
                patch("backend.report_builder.build_payload", return_value=valid_payload("new")),
                patch(
                    "backend.report_builder.source_fingerprints",
                    side_effect=[{"hash": "before"}, {"hash": "after"}],
                ),
            ):
                with self.assertRaises(ReportBuildError):
                    rebuild_report(output_path=output)
            self.assertEqual(output.read_bytes(), previous)

    def test_interrupted_atomic_write_preserves_previous_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "data.json"
            write_payload(valid_payload("previous"), output)
            previous = output.read_bytes()
            with patch("backend.report_builder.os.replace", side_effect=OSError("interruption")):
                with self.assertRaises(OSError):
                    write_payload(valid_payload("new"), output)
            self.assertEqual(output.read_bytes(), previous)
            self.assertEqual(list(output.parent.glob(".data.json.*.tmp")), [])

    def test_successive_rebuilds_leave_one_publication_and_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "dist" / "data.json"
            first_state = {"source": {"sha256": "first"}}
            updated_state = {"source": {"sha256": "updated"}}
            with (
                patch.dict(os.environ, {"REPORTING_RUNTIME_DIR": str(Path(directory) / "runtime")}),
                patch(
                    "backend.report_builder.build_payload",
                    side_effect=[valid_payload("first"), valid_payload("second")],
                ),
                patch(
                    "backend.report_builder.source_fingerprints",
                    side_effect=[first_state, first_state, updated_state, updated_state],
                ),
            ):
                rebuild_report(output_path=output)
                rebuild_report(output_path=output)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["meta"]["generatedAt"], "second")
            self.assertEqual(list(output.parent.glob(".data.json.*.tmp")), [])

    def test_simultaneous_rebuild_is_refused_and_stale_lock_is_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            with patch.dict(os.environ, {"REPORTING_RUNTIME_DIR": str(runtime)}):
                with report_build_lock():
                    with self.assertRaises(ReportBuildError):
                        with report_build_lock():
                            pass

                runtime.mkdir(parents=True, exist_ok=True)
                (runtime / "report-rebuild.lock").write_text(
                    json.dumps({"pid": 99999999, "token": "stale"}),
                    encoding="utf-8",
                )
                with report_build_lock():
                    self.assertTrue((runtime / "report-rebuild.lock").exists())
                self.assertFalse((runtime / "report-rebuild.lock").exists())


if __name__ == "__main__":
    unittest.main()
