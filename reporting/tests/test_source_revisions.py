from __future__ import annotations

import json
import sys
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from core import source_revision_store as revisions


class SourceRevisionStoreTests(unittest.TestCase):
    def test_revision_is_validated_by_hash_then_promoted_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pointer = root / "active_sources.json"
            with (
                patch.object(revisions, "SOURCE_REVISIONS_DIR", root / "revisions"),
                patch.object(revisions, "ACTIVE_SOURCES_POINTER", pointer),
            ):
                revision_id, staging = revisions.create_staging_revision("odoo")
                (staging / "clients.csv").write_text("id;name\n1;Client\n", encoding="utf-8")
                descriptor = revisions.file_descriptor(staging / "clients.csv", relative_to=staging)
                revisions.write_revision_manifest(staging, {
                    "schemaVersion": 2,
                    "source": "odoo",
                    "revisionId": revision_id,
                    "status": "validated",
                    "completedAt": "2026-10-20T10:00:00+00:00",
                    "records": 1,
                    "files": [descriptor],
                })

                final, previous = revisions.promote_revision(
                    "odoo", revision_id, staging, ["clients.csv"]
                )

                self.assertIsNone(previous)
                self.assertTrue((final / "clients.csv").is_file())
                registry = json.loads(pointer.read_text(encoding="utf-8"))
                self.assertEqual(registry["odoo"]["revisionId"], revision_id)
                self.assertNotIn("sellsy", registry)

    def test_tampered_required_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory)
            source = staging / "orders.xlsx"
            source.write_bytes(b"before")
            descriptor = revisions.file_descriptor(source, relative_to=staging)
            revisions.write_revision_manifest(staging, {
                "schemaVersion": 2,
                "source": "sellsy",
                "status": "validated",
                "files": [descriptor],
            })
            source.write_bytes(b"after")

            with self.assertRaisesRegex(RuntimeError, "Empreinte invalide"):
                revisions.validate_revision("sellsy", staging, ["orders.xlsx"])

    def test_failed_candidate_keeps_active_revision_and_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pointer = root / "active_sources.json"
            revisions_root = root / "revisions"
            active_id = "odoo-active"
            active_dir = revisions_root / "odoo" / active_id
            active_dir.mkdir(parents=True)
            (active_dir / "odoo.sqlite").write_bytes(b"active")
            (active_dir / "manifest.json").write_text(json.dumps({
                "schemaVersion": 2,
                "source": "odoo",
                "status": "validated",
                "completedAt": "2026-11-15T10:00:00+00:00",
                "synchronization": {"cursor": "2026-11-15T09:55:00+00:00"},
            }), encoding="utf-8")
            original_registry = {
                "schemaVersion": 2,
                "odoo": {"revisionId": active_id, "activatedAt": "2026-11-15T10:00:00+00:00"},
            }
            pointer.write_text(json.dumps(original_registry), encoding="utf-8")

            with (
                patch.object(revisions, "SOURCE_REVISIONS_DIR", revisions_root),
                patch.object(revisions, "ACTIVE_SOURCES_POINTER", pointer),
            ):
                candidate_id, staging = revisions.create_staging_revision("odoo")
                candidate = staging / "odoo.sqlite"
                candidate.write_bytes(b"before")
                descriptor = revisions.file_descriptor(candidate, relative_to=staging)
                revisions.write_revision_manifest(staging, {
                    "schemaVersion": 2,
                    "source": "odoo",
                    "revisionId": candidate_id,
                    "status": "validated",
                    "completedAt": "2026-11-16T10:00:00+00:00",
                    "synchronization": {"cursor": "2026-11-16T09:55:00+00:00"},
                    "files": [descriptor],
                })
                candidate.write_bytes(b"tampered")

                with self.assertRaisesRegex(RuntimeError, "Empreinte invalide"):
                    revisions.promote_revision(
                        "odoo", candidate_id, staging, ["odoo.sqlite"]
                    )

            self.assertEqual(json.loads(pointer.read_text(encoding="utf-8")), original_registry)


if __name__ == "__main__":
    unittest.main()
