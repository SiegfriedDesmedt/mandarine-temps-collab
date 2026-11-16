from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid

from datetime import datetime, timezone
from pathlib import Path

from .paths import DATA_DIR


SOURCE_REVISIONS_DIR = DATA_DIR / "source-revisions"
ACTIVE_SOURCES_POINTER = DATA_DIR / "active_sources.json"
SOURCE_NAMES = frozenset({"odoo", "sellsy"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def file_descriptor(path: Path, *, relative_to: Path | None = None) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return {
        "path": str(path.relative_to(relative_to) if relative_to else path.name).replace("\\", "/"),
        "sizeBytes": stat.st_size,
        "sha256": digest.hexdigest(),
    }


def load_active_sources() -> dict | None:
    try:
        payload = json.loads(ACTIVE_SOURCES_POINTER.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Le registre des sources actives est illisible") from error
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 2:
        raise RuntimeError("Version invalide du registre des sources actives")
    return payload


def active_revision(source: str) -> dict | None:
    if source not in SOURCE_NAMES:
        raise ValueError(f"Source inconnue : {source}")
    registry = load_active_sources()
    if not registry:
        return None
    record = registry.get(source)
    if not isinstance(record, dict):
        return None
    revision_id = str(record.get("revisionId") or "")
    path = SOURCE_REVISIONS_DIR / source / revision_id
    if not revision_id or not path.is_dir():
        raise RuntimeError(f"La révision active {source} est absente : {revision_id or 'non renseignée'}")
    manifest_path = path / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Le manifeste actif {source} est invalide") from error
    if manifest.get("status") != "validated" or manifest.get("source") != source:
        raise RuntimeError(f"La révision active {source} n'est pas validée")
    return {**record, "path": str(path), "manifestData": manifest}


def create_staging_revision(source: str) -> tuple[str, Path]:
    if source not in SOURCE_NAMES:
        raise ValueError(f"Source inconnue : {source}")
    root = SOURCE_REVISIONS_DIR / source
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    revision_id = f"{source}-{stamp}-{uuid.uuid4().hex[:8]}"
    staging = root / f".staging-{revision_id}"
    staging.mkdir()
    return revision_id, staging


def write_revision_manifest(staging: Path, manifest: dict) -> Path:
    target = staging / "manifest.json"
    _atomic_json(target, manifest)
    return target


def validate_revision(source: str, staging: Path, required_files: list[str]) -> dict:
    try:
        manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Manifeste {source} absent ou invalide") from error
    if manifest.get("schemaVersion") != 2 or manifest.get("source") != source:
        raise RuntimeError(f"Contrat de manifeste {source} invalide")
    if manifest.get("status") != "validated":
        raise RuntimeError(f"Révision {source} non validée")
    missing = [name for name in required_files if not (staging / name).is_file()]
    if missing:
        raise RuntimeError(f"Révision {source} incomplète : {', '.join(missing)}")
    descriptors = {item.get("path"): item for item in manifest.get("files", [])}
    for name in required_files:
        expected = descriptors.get(name.replace("\\", "/"))
        if not expected:
            raise RuntimeError(f"Empreinte absente du manifeste {source} : {name}")
        actual = file_descriptor(staging / name, relative_to=staging)
        if actual["sha256"] != expected.get("sha256") or actual["sizeBytes"] != expected.get("sizeBytes"):
            raise RuntimeError(f"Empreinte invalide dans la révision {source} : {name}")
    return manifest


def promote_revision(
    source: str,
    revision_id: str,
    staging: Path,
    required_files: list[str],
) -> tuple[Path, dict | None]:
    manifest = validate_revision(source, staging, required_files)
    final = SOURCE_REVISIONS_DIR / source / revision_id
    if final.exists():
        raise RuntimeError(f"La révision {revision_id} existe déjà")
    previous = load_active_sources()
    staging.replace(final)
    registry = dict(previous or {"schemaVersion": 2})
    registry["schemaVersion"] = 2
    registry[source] = {
        "revisionId": revision_id,
        "activatedAt": utc_now(),
        "manifest": str((Path(source) / revision_id / "manifest.json").as_posix()),
        "completedAt": manifest.get("completedAt"),
        "records": manifest.get("records", 0),
    }
    registry["updatedAt"] = utc_now()
    try:
        _atomic_json(ACTIVE_SOURCES_POINTER, registry)
    except Exception:
        shutil.rmtree(final, ignore_errors=True)
        raise
    return final, previous


def restore_registry(previous: dict | None) -> None:
    if previous:
        _atomic_json(ACTIVE_SOURCES_POINTER, previous)
    else:
        ACTIVE_SOURCES_POINTER.unlink(missing_ok=True)
