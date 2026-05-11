from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


COOKIE_STATE_KEY = "facebook_cookies"
FACEBOOK_PROXY_STATE_KEY = "facebook_proxies"


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
            "missing MURMUR_COOKIE_STATE_SECRET or WEBUI_SECRET_KEY for encrypted cookie state"
        )

    from cryptography.fernet import Fernet

    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key).encrypt(raw).decode("ascii"), "fernet:v1"


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
                "missing MURMUR_COOKIE_STATE_SECRET or WEBUI_SECRET_KEY for encrypted cookie state"
            )

        from cryptography.fernet import Fernet

        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
        return Fernet(key).decrypt(value.encode("ascii")).decode("utf-8")
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
    from psycopg import sql

    with connect_state_db() as connection:
        ensure_state_table(connection)
        table = sql.Identifier(state_table_name())
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("SELECT value, encoding FROM {table} WHERE key = %s").format(
                    table=table
                ),
                (COOKIE_STATE_KEY,),
            )
            row = cursor.fetchone()

    if not row:
        raise RuntimeStateMissing("no stored Facebook cookie state")

    cookie_text = decode_cookie_state(str(row[0]), str(row[1]))
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
    argv = argv or sys.argv[1:]
    command = argv[0] if argv else ""
    if command != "write-cookies":
        print("Usage: python -m murmur.runtime_state write-cookies", file=sys.stderr)
        return 64

    if not state_database_url():
        return 2

    try:
        write_cookie_file_from_state()
    except RuntimeStateMissing as exc:
        print(f"DB cookie state unavailable: {exc}")
        return 1
    except Exception as exc:
        print(f"DB cookie state unavailable: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
