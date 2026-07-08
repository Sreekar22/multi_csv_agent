import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.core.config import settings


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def create_session() -> dict:
    session_id = str(uuid4())
    created_at = datetime.now(timezone.utc).isoformat()

    raw_session_dir = settings.raw_data_dir / session_id
    profile_session_dir = settings.profile_data_dir / session_id
    processed_session_dir = settings.processed_data_dir / session_id
    raw_session_dir.mkdir(parents=True, exist_ok=False)
    profile_session_dir.mkdir(parents=True, exist_ok=False)
    processed_session_dir.mkdir(parents=True, exist_ok=False)

    manifest = {
        "session_id": session_id,
        "created_at": created_at,
        "files": [],
    }
    _write_json(profile_session_dir / "session_manifest.json", manifest)
    return manifest


def get_session_paths(session_id: str) -> tuple[Path, Path]:
    raw_session_dir = settings.raw_data_dir / session_id
    profile_session_dir = settings.profile_data_dir / session_id
    return raw_session_dir, profile_session_dir


def ensure_session_exists(session_id: str) -> tuple[Path, Path]:
    raw_session_dir, profile_session_dir = get_session_paths(session_id)
    if not raw_session_dir.exists() or not profile_session_dir.exists():
        raise FileNotFoundError(f"Session '{session_id}' does not exist")
    return raw_session_dir, profile_session_dir


def read_manifest(session_id: str) -> dict:
    _, profile_session_dir = ensure_session_exists(session_id)
    manifest_path = profile_session_dir / "session_manifest.json"
    if not manifest_path.exists():
        return {
            "session_id": session_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": [],
        }
    return _read_json(manifest_path)


def upsert_manifest(session_id: str, files: list[dict]) -> dict:
    _, profile_session_dir = ensure_session_exists(session_id)
    manifest_path = profile_session_dir / "session_manifest.json"

    manifest = read_manifest(session_id)

    existing_by_name = {entry["file_name"]: entry for entry in manifest.get("files", [])}
    for item in files:
        existing_by_name[item["file_name"]] = item

    manifest["files"] = sorted(existing_by_name.values(), key=lambda item: item["file_name"])
    _write_json(manifest_path, manifest)
    return manifest


def read_chat_context(session_id: str) -> dict:
    _, profile_session_dir = ensure_session_exists(session_id)
    context_path = profile_session_dir / "chat_context.json"
    if not context_path.exists():
        return {
            "last_metric": None,
            "last_view": None,
            "last_time_window": None,
            "last_dimension": None,
        }
    return _read_json(context_path)


def upsert_chat_context(session_id: str, context: dict) -> dict:
    _, profile_session_dir = ensure_session_exists(session_id)
    context_path = profile_session_dir / "chat_context.json"

    current = read_chat_context(session_id)
    current.update(context)
    _write_json(context_path, current)
    return current
