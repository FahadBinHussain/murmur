from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any


COOKIE_STATE_KEY = "facebook_cookies"
FACEBOOK_PROXY_STATE_KEY = "facebook_proxies"
FACEBOOK_PROFILE_STATE_KEY = "facebook_browser_profile"
THREAD_MODEL_SELECTIONS_STATE_KEY = "thread_model_selections"

PROFILE_EXCLUDED_DIRS = {
    "BrowserMetrics",
    "Cache",
    "Code Cache",
    "Crashpad",
    "DawnCache",
    "GraphiteDawnCache",
    "GrShaderCache",
    "GPUCache",
    "ShaderCache",
    "Safe Browsing",
    "component_crx_cache",
    "extensions_crx_cache",
    "segmentation_platform",
}
PROFILE_EXCLUDED_FILES = {
    "BrowserMetrics-spare.pma",
    "first_party_sets.db-journal",
    "LOCK",
    "SingletonCookie",
    "SingletonLock",
    "SingletonSocket",
}


class RuntimeStateError(Exception):
    pass


class RuntimeStateNotConfigured(RuntimeStateError):
    pass


class RuntimeStateMissing(RuntimeStateError):
    pass


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def load_env_file() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path.cwd() / ".env")


def state_database_url() -> str:
    return (os.getenv("MURMUR_STATE_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()


def state_table_name() -> str:
    table = (os.getenv("MURMUR_STATE_TABLE") or "murmur_runtime_state").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise RuntimeStateError("MURMUR_STATE_TABLE must be a simple SQL identifier.")
    return table


def cookie_state_secret() -> str:
    return (
        os.getenv("MURMUR_COOKIE_STATE_SECRET")
        or os.getenv("MURMUR_STATE_SECRET")
        or os.getenv("WEBUI_SECRET_KEY")
        or ""
    ).strip()


def cookie_file_path() -> Path:
    return Path(os.getenv("FB_COOKIES_PATH", "cookies.json"))


def facebook_profile_dir() -> Path:
    return Path(os.getenv("FB_LOGIN_PROFILE_DIR", ".murmur-facebook-profile"))


def connect_state_db():
    database_url = state_database_url()
    if not database_url:
        raise RuntimeStateNotConfigured("missing MURMUR_STATE_DATABASE_URL or DATABASE_URL")

    import psycopg

    return psycopg.connect(database_url, autocommit=True)


def ensure_state_table(connection: Any) -> None:
    from psycopg import sql

    table = sql.Identifier(state_table_name())
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {table} (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    encoding TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            ).format(table=table)
        )


def encode_json_state(payload: Any) -> tuple[str, str]:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not env_bool("MURMUR_COOKIE_STATE_ENCRYPT", True):
        return base64.b64encode(raw).decode("ascii"), "base64-json"

    secret = cookie_state_secret()
    if not secret:
        raise RuntimeStateError(
            "missing MURMUR_COOKIE_STATE_SECRET for encrypted cookie state"
        )

    from cryptography.fernet import Fernet

    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key).encrypt(raw).decode("ascii"), "fernet:v1"


def encode_plain_json_state(payload: Any) -> tuple[str, str]:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ), "plain-json"


def encode_binary_state(raw: bytes, plain_encoding: str, encrypted_encoding: str) -> tuple[str, str]:
    if not env_bool("MURMUR_COOKIE_STATE_ENCRYPT", True):
        return base64.b64encode(raw).decode("ascii"), plain_encoding

    secret = cookie_state_secret()
    if not secret:
        raise RuntimeStateError(
            "missing MURMUR_COOKIE_STATE_SECRET for encrypted runtime state"
        )

    from cryptography.fernet import Fernet

    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key).encrypt(raw).decode("ascii"), encrypted_encoding


def encode_cookie_state(cookies: list[dict]) -> tuple[str, str]:
    return encode_json_state(cookies)


def decode_json_state(value: str, encoding: str) -> str:
    if encoding == "base64-json":
        return base64.b64decode(value).decode("utf-8")
    if encoding == "plain-json":
        return value
    if encoding == "fernet:v1":
        secret = cookie_state_secret()
        if not secret:
            raise RuntimeStateError(
                "missing MURMUR_COOKIE_STATE_SECRET for encrypted cookie state"
            )

        from cryptography.fernet import Fernet

        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
        return Fernet(key).decrypt(value.encode("ascii")).decode("utf-8")
    raise RuntimeStateError(f"unsupported runtime state encoding: {encoding}")


def decode_binary_state(
    value: str,
    encoding: str,
    plain_encoding: str,
    encrypted_encoding: str,
) -> bytes:
    if encoding == plain_encoding:
        return base64.b64decode(value)
    if encoding == encrypted_encoding:
        secret = cookie_state_secret()
        if not secret:
            raise RuntimeStateError(
                "missing MURMUR_COOKIE_STATE_SECRET for encrypted runtime state"
            )

        from cryptography.fernet import Fernet

        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
        return Fernet(key).decrypt(value.encode("ascii"))
    raise RuntimeStateError(f"unsupported runtime state encoding: {encoding}")


def decode_cookie_state(value: str, encoding: str) -> str:
    return decode_json_state(value, encoding)


def validate_cookie_state_text(cookie_text: str) -> None:
    payload = json.loads(cookie_text)
    if not isinstance(payload, list):
        raise RuntimeStateError("stored cookie state is not a cookie list")
    names = {str(cookie.get("name") or "") for cookie in payload if isinstance(cookie, dict)}
    missing = [name for name in ("c_user", "xs") if name not in names]
    if missing:
        raise RuntimeStateError(
            "stored cookie state is missing required Facebook cookies: " + ", ".join(missing)
        )


def persist_cookie_state(cookies: list[dict]) -> str:
    if not env_bool("MURMUR_PERSIST_COOKIES_TO_DB", True):
        return "DB cookie state sync disabled."
    if not state_database_url():
        return "DB cookie state not synced: missing MURMUR_STATE_DATABASE_URL or DATABASE_URL."

    try:
        value, encoding = encode_cookie_state(cookies)
        from psycopg import sql

        with connect_state_db() as connection:
            ensure_state_table(connection)
            table = sql.Identifier(state_table_name())
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        """
                        INSERT INTO {table} (key, value, encoding, updated_at)
                        VALUES (%s, %s, %s, NOW())
                        ON CONFLICT (key) DO UPDATE
                        SET value = EXCLUDED.value,
                            encoding = EXCLUDED.encoding,
                            updated_at = NOW()
                        """
                    ).format(table=table),
                    (COOKIE_STATE_KEY, value, encoding),
                )
    except Exception as exc:
        return f"DB cookie state sync failed: {exc}"

    return "DB cookie state synced."


def upsert_runtime_state(key: str, value: str, encoding: str) -> None:
    from psycopg import sql

    with connect_state_db() as connection:
        ensure_state_table(connection)
        table = sql.Identifier(state_table_name())
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {table} (key, value, encoding, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value,
                        encoding = EXCLUDED.encoding,
                        updated_at = NOW()
                    """
                ).format(table=table),
                (key, value, encoding),
            )


def fetch_runtime_state(key: str) -> tuple[str, str]:
    from psycopg import sql

    with connect_state_db() as connection:
        ensure_state_table(connection)
        table = sql.Identifier(state_table_name())
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("SELECT value, encoding FROM {table} WHERE key = %s").format(
                    table=table
                ),
                (key,),
            )
            row = cursor.fetchone()

    if not row:
        raise RuntimeStateMissing(f"no stored runtime state for {key}")
    return str(row[0]), str(row[1])


def normalize_thread_model_selections(payload: Any) -> dict[str, dict[str, str]]:
    if not isinstance(payload, dict):
        raise RuntimeStateError("stored thread model selections are not an object")

    allowed_keys = {
        "chat_model",
        "chat_alias",
        "chat_provider",
        "image_model",
        "image_alias",
    }
    cleaned: dict[str, dict[str, str]] = {}
    for raw_thread_id, raw_selection in payload.items():
        thread_id = str(raw_thread_id or "").strip()
        if not thread_id or not isinstance(raw_selection, dict):
            continue

        selection: dict[str, str] = {}
        for key in allowed_keys:
            value = raw_selection.get(key)
            if isinstance(value, str) and value.strip():
                selection[key] = value.strip()

        if selection:
            cleaned[thread_id] = selection

    return cleaned


def persist_thread_model_selections(selections: dict[str, dict[str, str]]) -> str:
    if not env_bool("MURMUR_PERSIST_THREAD_MODELS_TO_DB", True):
        return "DB thread model selections sync disabled."
    if not state_database_url():
        return "DB thread model selections not synced: missing MURMUR_STATE_DATABASE_URL or DATABASE_URL."

    try:
        cleaned = normalize_thread_model_selections(selections)
        value, encoding = encode_plain_json_state(cleaned)
        upsert_runtime_state(THREAD_MODEL_SELECTIONS_STATE_KEY, value, encoding)
    except Exception as exc:
        return f"DB thread model selections sync failed: {exc}"

    return "DB thread model selections synced."


def load_thread_model_selections() -> dict[str, dict[str, str]]:
    if not env_bool("MURMUR_PERSIST_THREAD_MODELS_TO_DB", True):
        return {}

    value, encoding = fetch_runtime_state(THREAD_MODEL_SELECTIONS_STATE_KEY)
    raw = json.loads(decode_json_state(value, encoding))
    return normalize_thread_model_selections(raw)


def profile_max_bytes() -> int:
    raw = os.getenv("FB_LOGIN_PROFILE_VAULT_MAX_BYTES", "").strip()
    if not raw:
        return 200 * 1024 * 1024
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeStateError("FB_LOGIN_PROFILE_VAULT_MAX_BYTES must be an integer") from exc


def should_skip_profile_path(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    parts = set(rel.parts)
    if parts & PROFILE_EXCLUDED_DIRS:
        return True
    if path.name in PROFILE_EXCLUDED_FILES:
        return True
    if path.name.endswith((".tmp", ".pma")):
        return True
    return False


def pack_facebook_profile(profile_dir: Path) -> tuple[bytes, dict[str, int]]:
    if not profile_dir.exists() or not profile_dir.is_dir():
        raise RuntimeStateError(f"Facebook browser profile directory is missing: {profile_dir}")

    profile_dir = profile_dir.resolve()
    buffer = BytesIO()
    file_count = 0
    raw_bytes = 0
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(profile_dir.rglob("*")):
            if should_skip_profile_path(path, profile_dir):
                continue
            if not path.is_file():
                continue
            rel = path.relative_to(profile_dir).as_posix()
            try:
                stat = path.stat()
            except OSError:
                continue
            info = zipfile.ZipInfo(rel)
            info.date_time = time.localtime(stat.st_mtime)[:6]
            info.external_attr = (stat.st_mode & 0xFFFF) << 16
            try:
                archive.writestr(info, path.read_bytes())
            except OSError:
                continue
            file_count += 1
            raw_bytes += stat.st_size

    payload = buffer.getvalue()
    if not file_count:
        raise RuntimeStateError("Facebook browser profile has no packable files")
    max_bytes = profile_max_bytes()
    if len(payload) > max_bytes:
        raise RuntimeStateError(
            f"packed Facebook browser profile is {len(payload)} bytes, above limit {max_bytes}"
        )
    return payload, {"files": file_count, "raw_bytes": raw_bytes, "zip_bytes": len(payload)}


def persist_facebook_profile_state(profile_dir: Path | None = None) -> str:
    if not env_bool("FB_LOGIN_PROFILE_VAULT_ENABLED", True):
        return "DB Facebook profile vault sync disabled."
    if not state_database_url():
        return "DB Facebook profile vault not synced: missing MURMUR_STATE_DATABASE_URL or DATABASE_URL."

    try:
        profile_dir = profile_dir or facebook_profile_dir()
        payload, stats = pack_facebook_profile(profile_dir)
        value, encoding = encode_binary_state(payload, "base64-zip", "fernet-zip:v1")
        upsert_runtime_state(FACEBOOK_PROFILE_STATE_KEY, value, encoding)
    except Exception as exc:
        return f"DB Facebook profile vault sync failed: {exc}"

    return (
        "DB Facebook profile vault synced "
        f"({stats['files']} files, {stats['raw_bytes']} raw bytes, {stats['zip_bytes']} zipped bytes)."
    )


def safe_extract_zip(payload: bytes, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        for info in archive.infolist():
            rel = Path(info.filename)
            if rel.is_absolute() or ".." in rel.parts:
                raise RuntimeStateError(f"unsafe path in profile vault: {info.filename}")
            if info.is_dir():
                continue
            destination = target_dir / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)


def restore_facebook_profile_state(
    profile_dir: Path | None = None,
    *,
    overwrite: bool = False,
) -> str:
    if not env_bool("FB_LOGIN_PROFILE_VAULT_ENABLED", True):
        return "DB Facebook profile vault restore disabled."
    if not state_database_url():
        return "DB Facebook profile vault not restored: missing MURMUR_STATE_DATABASE_URL or DATABASE_URL."

    profile_dir = profile_dir or facebook_profile_dir()
    if profile_dir.exists() and any(profile_dir.iterdir()) and not overwrite:
        return f"DB Facebook profile vault restore skipped: {profile_dir} is not empty."

    try:
        value, encoding = fetch_runtime_state(FACEBOOK_PROFILE_STATE_KEY)
        payload = decode_binary_state(value, encoding, "base64-zip", "fernet-zip:v1")
        parent = profile_dir.parent
        parent.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix=f".{profile_dir.name}.restore-", dir=str(parent)))
        try:
            safe_extract_zip(payload, temp_dir)
            if profile_dir.exists():
                shutil.rmtree(profile_dir)
            shutil.move(str(temp_dir), str(profile_dir))
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
    except RuntimeStateMissing as exc:
        return f"DB Facebook profile vault unavailable: {exc}"
    except Exception as exc:
        return f"DB Facebook profile vault restore failed: {exc}"

    return f"DB Facebook profile vault restored to {profile_dir}."


def persist_facebook_proxy_state(proxies: dict[str, str]) -> str:
    if not env_bool("MURMUR_PERSIST_COOKIES_TO_DB", True):
        return "DB proxy state sync disabled."
    if not state_database_url():
        return "DB proxy state not synced: missing MURMUR_STATE_DATABASE_URL or DATABASE_URL."

    cleaned = {
        key: str(proxies.get(key) or "").strip()
        for key in ("FB_PROXY", "FB_UPLOAD_PROXY", "FB_MQTT_PROXY")
    }

    try:
        value, encoding = encode_json_state(cleaned)
        from psycopg import sql

        with connect_state_db() as connection:
            ensure_state_table(connection)
            table = sql.Identifier(state_table_name())
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        """
                        INSERT INTO {table} (key, value, encoding, updated_at)
                        VALUES (%s, %s, %s, NOW())
                        ON CONFLICT (key) DO UPDATE
                        SET value = EXCLUDED.value,
                            encoding = EXCLUDED.encoding,
                            updated_at = NOW()
                        """
                    ).format(table=table),
                    (FACEBOOK_PROXY_STATE_KEY, value, encoding),
                )
    except Exception as exc:
        return f"DB proxy state sync failed: {exc}"

    return "DB proxy state synced."


def load_facebook_proxy_state() -> dict[str, str]:
    from psycopg import sql

    with connect_state_db() as connection:
        ensure_state_table(connection)
        table = sql.Identifier(state_table_name())
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("SELECT value, encoding FROM {table} WHERE key = %s").format(
                    table=table
                ),
                (FACEBOOK_PROXY_STATE_KEY,),
            )
            row = cursor.fetchone()

    if not row:
        raise RuntimeStateMissing("no stored Facebook proxy state")

    raw = json.loads(decode_json_state(str(row[0]), str(row[1])))
    if not isinstance(raw, dict):
        raise RuntimeStateError("stored Facebook proxy state is not an object")

    return {
        key: str(raw.get(key) or "").strip()
        for key in ("FB_PROXY", "FB_UPLOAD_PROXY", "FB_MQTT_PROXY")
    }


def load_cookie_state_text() -> str:
    value, encoding = fetch_runtime_state(COOKIE_STATE_KEY)
    cookie_text = decode_cookie_state(value, encoding)
    validate_cookie_state_text(cookie_text)
    return cookie_text


def write_cookie_file_from_state() -> None:
    cookie_text = load_cookie_state_text()
    path = cookie_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cookie_text, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    print("Writing Messenger cookies from DB cookie state.")


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    argv = argv or sys.argv[1:]
    command = argv[0] if argv else ""
    if command not in {"write-cookies", "persist-profile", "restore-profile"}:
        print(
            "Usage: python -m murmur.runtime_state "
            "write-cookies|persist-profile|restore-profile [--overwrite]",
            file=sys.stderr,
        )
        return 64

    if not state_database_url():
        return 2

    try:
        if command == "write-cookies":
            write_cookie_file_from_state()
        elif command == "persist-profile":
            print(persist_facebook_profile_state())
        elif command == "restore-profile":
            print(restore_facebook_profile_state(overwrite="--overwrite" in argv[1:]))
    except RuntimeStateMissing as exc:
        print(f"DB runtime state unavailable: {exc}")
        return 1
    except Exception as exc:
        print(f"DB runtime state unavailable: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
