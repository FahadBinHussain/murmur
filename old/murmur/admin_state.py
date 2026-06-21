import json
import os
import re
import time
from pathlib import Path
from typing import Any


def thread_registry_path() -> Path:
    return Path(os.getenv("MURMUR_THREAD_REGISTRY_PATH", "/tmp/murmur-threads.json"))


def thread_allowlist_path() -> Path:
    return Path(
        os.getenv("MURMUR_THREAD_ALLOWLIST_PATH", "/tmp/murmur-thread-allowlist.json")
    )


def env_allowed_thread_ids() -> set[str]:
    return {
        thread_id.strip()
        for thread_id in os.getenv("ALLOWED_THREAD_IDS", "").split(",")
        if thread_id.strip()
    }


def read_json_file(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temp_path.replace(path)


def read_thread_registry(path: Path | None = None) -> dict[str, dict[str, Any]]:
    payload = read_json_file(path or thread_registry_path(), {})
    threads = payload.get("threads", {}) if isinstance(payload, dict) else {}
    if not isinstance(threads, dict):
        return {}

    result: dict[str, dict[str, Any]] = {}
    for thread_id, entry in threads.items():
        if not isinstance(entry, dict):
            continue
        thread_id_text = str(thread_id).strip()
        if not thread_id_text:
            continue
        clean_entry = dict(entry)
        clean_entry["id"] = thread_id_text
        result[thread_id_text] = clean_entry
    return result


def write_thread_registry(
    threads: dict[str, dict[str, Any]],
    path: Path | None = None,
) -> None:
    payload = {
        "updated_at": int(time.time()),
        "threads": threads,
    }
    write_json_file(path or thread_registry_path(), payload)


def read_thread_allowlist(path: Path | None = None) -> tuple[str, set[str]]:
    path = path or thread_allowlist_path()
    payload = read_json_file(path, None)
    if isinstance(payload, dict) and payload.get("mode") == "allowlist":
        ids = payload.get("thread_ids", [])
        if isinstance(ids, list):
            return "allowlist", {str(thread_id) for thread_id in ids if thread_id}

    env_ids = env_allowed_thread_ids()
    if env_ids:
        return "env_allowlist", env_ids
    return "allow_all", set()


def write_thread_allowlist(thread_ids: set[str], path: Path | None = None) -> None:
    payload = {
        "mode": "allowlist",
        "thread_ids": sorted(thread_ids),
        "updated_at": int(time.time()),
    }
    write_json_file(path or thread_allowlist_path(), payload)


def thread_allowed(thread_id: str, mode: str, allowed_ids: set[str]) -> bool:
    return mode == "allow_all" or thread_id in allowed_ids


def parse_thread_ids(value: str) -> set[str]:
    return {
        item.strip()
        for item in re.split(r"[\s,;]+", value or "")
        if item.strip()
    }
