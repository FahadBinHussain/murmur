import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import signal
import time
from pathlib import Path
from collections.abc import Iterable
from contextlib import suppress
from urllib.parse import quote

import aiohttp
from aiohttp import ClientError, WSMsgType, web

from .admin_state import (
    parse_thread_ids,
    read_thread_allowlist,
    read_thread_registry,
    thread_allowed,
    thread_allowlist_path,
    thread_registry_path,
    write_thread_allowlist,
)
from .runtime_state import persist_cookie_state


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def filtered_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
    }


def target_url(request: web.Request) -> str:
    base_url = request.app["target_base_url"]
    return f"{base_url}{request.rel_url}"


def admin_base_path() -> str:
    path = os.getenv("MURMUR_ADMIN_PATH", "/murmur-admin").strip() or "/murmur-admin"
    return "/" + path.strip("/")


def admin_console_enabled() -> bool:
    return env_bool("MURMUR_ADMIN_CONSOLE", True)


def admin_username() -> str:
    return (
        os.getenv("MURMUR_ADMIN_USERNAME")
        or os.getenv("WEBUI_ADMIN_EMAIL")
        or "admin"
    )


def admin_password() -> str:
    return os.getenv("MURMUR_ADMIN_PASSWORD") or os.getenv("WEBUI_ADMIN_PASSWORD") or ""


def no_store_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-Robots-Tag": "noindex, nofollow",
    }
    if extra:
        headers.update(extra)
    return headers


def admin_session_cookie_name() -> str:
    return os.getenv("MURMUR_ADMIN_SESSION_COOKIE", "murmur_admin_session")


def admin_session_seconds() -> int:
    return parse_positive_int(os.getenv("MURMUR_ADMIN_SESSION_SECONDS"), 86400)


def admin_session_secret() -> str:
    return (
        os.getenv("MURMUR_ADMIN_SESSION_SECRET")
        or os.getenv("WEBUI_SECRET_KEY")
        or admin_password()
        or ""
    ).strip()


def admin_cookie_secure(request: web.Request) -> bool:
    configured = os.getenv("MURMUR_ADMIN_COOKIE_SECURE")
    if configured is not None:
        return configured.lower() in {"1", "true", "yes", "on"}
    return request.secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"


def admin_basic_auth_enabled() -> bool:
    return env_bool("MURMUR_ADMIN_BASIC_AUTH", False)


def admin_redirect_location(request: web.Request) -> str:
    sign = request.query.get("__sign", "")
    if sign:
        return f"{admin_base_path()}?__sign={quote(sign, safe='')}"
    return admin_base_path()


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def sign_admin_payload(payload: str) -> str:
    secret = admin_session_secret()
    return b64url_encode(hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest())


def make_admin_session(username: str) -> str:
    payload = b64url_encode(
        json.dumps(
            {"u": username, "iat": int(time.time()), "n": secrets.token_urlsafe(12)},
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return f"{payload}.{sign_admin_payload(payload)}"


def valid_admin_session(request: web.Request) -> bool:
    secret = admin_session_secret()
    if not secret:
        return False

    cookie = request.cookies.get(admin_session_cookie_name(), "")
    payload, sep, signature = cookie.partition(".")
    if not sep or not payload or not signature:
        return False

    expected = sign_admin_payload(payload)
    if not secrets.compare_digest(signature, expected):
        return False

    try:
        data = json.loads(b64url_decode(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return False

    if str(data.get("u") or "") != admin_username():
        return False

    try:
        issued_at = int(data.get("iat") or 0)
    except (TypeError, ValueError):
        return False
    return issued_at > 0 and int(time.time()) - issued_at <= admin_session_seconds()


def valid_basic_admin_auth(request: web.Request) -> bool:
    expected_password = admin_password()
    if not expected_password:
        return False

    auth_header = request.headers.get("Authorization", "")
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "basic" or not token:
        return False

    try:
        decoded = base64.b64decode(token).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False

    username, sep, password = decoded.partition(":")
    return bool(
        sep
        and secrets.compare_digest(username, admin_username())
        and secrets.compare_digest(password, expected_password)
    )


def admin_authenticated(request: web.Request) -> bool:
    return valid_admin_session(request) or (
        admin_basic_auth_enabled() and valid_basic_admin_auth(request)
    )


def admin_missing_config_response() -> web.Response | None:
    if admin_password() and admin_session_secret():
        return None
    return web.Response(
        text="Murmur admin console is missing MURMUR_ADMIN_PASSWORD or WEBUI_ADMIN_PASSWORD.",
        status=503,
        headers=no_store_headers(),
    )


def admin_cookie_path() -> Path:
    path = Path(os.getenv("FB_COOKIES_PATH", "cookies.json"))
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def admin_pid_file() -> Path:
    return Path(os.getenv("MURMUR_PID_FILE", "/tmp/murmur.pid"))


def admin_restart_now_file() -> Path:
    return Path(os.getenv("MURMUR_RESTART_NOW_FILE", "/tmp/murmur-restart-now"))


def image_proxy_auth_key() -> str | None:
    return os.getenv("IMAGE_PROXY_API_KEY") or os.getenv("IMAGES_OPENAI_API_KEY")


def image_proxy_auth_error(request: web.Request) -> web.Response | None:
    expected_key = image_proxy_auth_key()
    if not expected_key:
        return web.json_response(
            {"error": {"message": "Image proxy API key is not configured."}},
            status=503,
        )

    image_proxy_key = request.headers.get("X-Image-Proxy-Key", "")
    auth_header = request.headers.get("Authorization", "")
    scheme, _, bearer_token = auth_header.partition(" ")
    token = image_proxy_key or bearer_token
    if (
        (not image_proxy_key and scheme.lower() != "bearer")
        or not secrets.compare_digest(token, expected_key)
    ):
        return web.json_response(
            {"error": {"message": "Invalid image proxy API key."}},
            status=401,
        )

    return None


def image_proxy_config_error() -> web.Response | None:
    missing = [
        name
        for name in ("CF_ACCOUNT_ID", "CF_API_TOKEN")
        if not os.getenv(name)
    ]
    if not cloudflare_image_model():
        missing.append("IMAGE_GENERATION_MODEL")
    if missing:
        return web.json_response(
            {
                "error": {
                    "message": "Image proxy is missing: " + ", ".join(missing),
                }
            },
            status=503,
        )
    return None


def cloudflare_image_model() -> str:
    return (
        os.getenv("IMAGE_GENERATION_MODEL")
        or os.getenv("CLOUDFLARE_IMAGE_MODEL")
        or ""
    ).strip()


def parse_positive_int(value: object, default: int, maximum: int | None = None) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    parsed = max(1, parsed)
    return min(parsed, maximum) if maximum is not None else parsed


def openai_error(message: str, status: int = 502) -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": "image_generation_error"}},
        status=status,
    )


def short_prompt_id(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()[:12]


def compact_log_value(value: object, max_length: int = 2000) -> str:
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    if len(text) <= max_length:
        return text
    return f"{text[:max_length]}...<truncated>"


def log_image_proxy_error(prompt: str, message: str) -> None:
    prompt_id = short_prompt_id(prompt)
    print(
        "IMAGE_PROXY_ERROR "
        f"prompt_id={prompt_id} "
        f"{compact_log_value(message)}",
        flush=True,
    )
    write_image_proxy_error(prompt_id, message)


def write_image_proxy_error(prompt_id: str, message: str) -> None:
    error_dir = Path(os.getenv("IMAGE_PROXY_ERROR_DIR", "/tmp/murmur-image-errors"))
    try:
        error_dir.mkdir(parents=True, exist_ok=True)
        (error_dir / f"{prompt_id}.json").write_text(
            json.dumps(
                {
                    "prompt_id": prompt_id,
                    "created": int(time.time()),
                    "message": message,
                }
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        print(
            "IMAGE_PROXY_ERROR_WRITE_FAILED "
            f"prompt_id={prompt_id} error={compact_log_value(exc)}",
            flush=True,
        )


def validate_cookie_payload(raw_text: str) -> tuple[list[dict], str]:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc

    if isinstance(payload, dict) and isinstance(payload.get("cookies"), list):
        cookies = payload["cookies"]
    elif isinstance(payload, list):
        cookies = payload
    else:
        raise ValueError("Cookie JSON must be a browser-exported list of cookies.")

    if not cookies:
        raise ValueError("Cookie JSON is empty.")
    if not all(isinstance(cookie, dict) for cookie in cookies):
        raise ValueError("Every cookie entry must be a JSON object.")

    names = {
        str(cookie.get("name") or "")
        for cookie in cookies
        if isinstance(cookie, dict)
    }
    missing = [name for name in ("c_user", "xs") if name not in names]
    if missing:
        raise ValueError(
            "Cookie JSON is missing required Facebook session cookies: "
            + ", ".join(missing)
        )

    account_id = next(
        (
            str(cookie.get("value") or "")
            for cookie in cookies
            if str(cookie.get("name") or "") == "c_user"
        ),
        "",
    )
    summary = f"{len(cookies)} cookies"
    if account_id:
        summary += f", c_user={account_id}"
    return cookies, summary


def write_cookie_file(cookies: list[dict]) -> Path:
    path = admin_cookie_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(cookies, ensure_ascii=False),
        encoding="utf-8",
    )
    with suppress(OSError):
        temp_path.chmod(0o600)
    temp_path.replace(path)
    with suppress(OSError):
        path.chmod(0o600)
    return path


def restart_murmur_listener() -> str:
    try:
        restart_file = admin_restart_now_file()
        restart_file.parent.mkdir(parents=True, exist_ok=True)
        restart_file.write_text(str(int(time.time())), encoding="utf-8")
    except OSError as exc:
        return f"Cookie saved. Restart marker could not be written: {exc}"

    pid_file = admin_pid_file()
    try:
        pid_text = pid_file.read_text(encoding="utf-8").strip()
        pid = int(pid_text)
    except (OSError, ValueError):
        return "Cookie saved. Murmur listener is already stopped or sleeping; supervisor wake-up requested."

    if pid <= 0:
        return "Cookie saved. Murmur listener PID was invalid; supervisor wake-up requested."

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "Cookie saved. Murmur listener was already stopped; supervisor wake-up requested."
    except OSError as exc:
        return f"Cookie saved. Supervisor wake-up requested, but listener stop failed: {exc}"

    return "Cookie saved. Murmur listener restart requested."


def admin_status() -> dict[str, str]:
    cookie_path = admin_cookie_path()
    pid_file = admin_pid_file()
    status = {
        "cookie_path": str(cookie_path),
        "pid_file": str(pid_file),
        "restart_file": str(admin_restart_now_file()),
    }
    try:
        stat = cookie_path.stat()
        status["cookie_file"] = f"{stat.st_size} bytes, modified {time.ctime(stat.st_mtime)}"
    except OSError:
        status["cookie_file"] = "missing"

    try:
        status["murmur_pid"] = pid_file.read_text(encoding="utf-8").strip()
    except OSError:
        status["murmur_pid"] = "missing"

    try:
        stat = admin_restart_now_file().stat()
        status["restart_marker"] = f"present, modified {time.ctime(stat.st_mtime)}"
    except OSError:
        status["restart_marker"] = "missing"
    return status


def admin_thread_state() -> tuple[list[dict[str, object]], str, set[str], set[str]]:
    threads = read_thread_registry(thread_registry_path())
    mode, allowed_ids = read_thread_allowlist(thread_allowlist_path())
    env_ids = {
        thread_id.strip()
        for thread_id in os.getenv("ALLOWED_THREAD_IDS", "").split(",")
        if thread_id.strip()
    }
    entries = []
    for thread_id, entry in threads.items():
        entries.append(
            {
                "id": thread_id,
                "name": str(entry.get("name") or thread_id),
                "type": str(entry.get("type") or ""),
                "last_seen": int(entry.get("last_seen") or 0),
                "last_sender_name": str(entry.get("last_sender_name") or ""),
                "allowed": thread_allowed(thread_id, mode, allowed_ids),
            }
        )
    entries.sort(key=lambda item: int(item["last_seen"]), reverse=True)
    known_ids = {str(entry["id"]) for entry in entries}
    extra_allowed_ids = allowed_ids - known_ids
    extra_env_ids = env_ids - known_ids
    return entries, mode, allowed_ids, extra_allowed_ids | extra_env_ids


def format_time(timestamp: object) -> str:
    try:
        value = int(timestamp)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def admin_threads_html(csrf_token: str) -> str:
    entries, mode, _allowed_ids, extra_ids = admin_thread_state()
    policy_label = {
        "allow_all": "All threads are currently allowed until you save a custom allowlist.",
        "env_allowlist": "Using ALLOWED_THREAD_IDS until you save a custom allowlist.",
        "allowlist": "Using admin console allowlist.",
    }.get(mode, mode)

    if entries:
        rows = "\n".join(
            "<tr>"
            "<td>"
            f'<input type="checkbox" name="allowed_thread_ids" value="{html.escape(str(entry["id"]))}" '
            f'{"checked" if entry["allowed"] else ""}>'
            "</td>"
            f"<td><strong>{html.escape(str(entry['name']))}</strong><br><code>{html.escape(str(entry['id']))}</code></td>"
            f"<td>{html.escape(str(entry['type']))}</td>"
            f"<td>{html.escape(str(entry['last_sender_name']))}</td>"
            f"<td>{html.escape(format_time(entry['last_seen']))}</td>"
            "</tr>"
            for entry in entries
        )
        table = (
            '<table class="threads">'
            "<thead><tr><th>Use</th><th>Thread</th><th>Type</th><th>Last Sender</th><th>Last Seen</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    else:
        table = '<p class="muted">No threads have been seen yet. Send a message in a thread or wait for the listener to refresh recent inbox threads.</p>'

    extra_value = "\n".join(sorted(extra_ids))
    return f"""
    <section>
      <h2>Thread Access</h2>
      <p>{html.escape(policy_label)}</p>
      <form method="post">
        <input type="hidden" name="csrf_token" value="{csrf_token}">
        <input type="hidden" name="action" value="save_threads">
        {table}
        <label for="extra_thread_ids">Additional allowed thread IDs</label>
        <textarea id="extra_thread_ids" name="extra_thread_ids" spellcheck="false">{html.escape(extra_value)}</textarea>
        <button type="submit">Save Thread Access</button>
      </form>
    </section>
    """


def admin_login_html(csrf_token: str, error: str = "", username: str = "") -> str:
    token = html.escape(csrf_token)
    error_html = f'<div class="notice error">{html.escape(error)}</div>' if error else ""
    username_value = html.escape(username)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Murmur Admin Login</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #eef2f3;
      color: #111827;
    }}
    main {{
      width: min(420px, calc(100vw - 32px));
      background: #fff;
      border: 1px solid #d7dde2;
      border-radius: 8px;
      padding: 28px;
      box-shadow: 0 18px 48px rgba(15, 23, 42, 0.14);
    }}
    h1 {{ margin: 0 0 6px; font-size: 24px; letter-spacing: 0; }}
    p {{ margin: 0 0 22px; color: #5b6472; line-height: 1.5; }}
    label {{ display: block; margin: 16px 0 8px; font-weight: 650; }}
    input {{
      width: 100%;
      box-sizing: border-box;
      border: 1px solid #c9d2dc;
      border-radius: 6px;
      padding: 11px 12px;
      background: #fff;
      color: #111827;
      font: inherit;
    }}
    input:focus {{
      outline: 2px solid #0f766e;
      outline-offset: 2px;
      border-color: #0f766e;
    }}
    button {{
      width: 100%;
      margin-top: 22px;
      border: 0;
      border-radius: 6px;
      background: #0f766e;
      color: white;
      font-weight: 750;
      padding: 11px 14px;
      cursor: pointer;
      font: inherit;
    }}
    .notice {{ margin: 16px 0 0; padding: 12px; border-radius: 6px; }}
    .error {{ background: #fef2f2; color: #991b1b; }}
    @media (prefers-color-scheme: dark) {{
      body {{ background: #0f172a; color: #e5e7eb; }}
      main {{ background: #111827; border-color: #374151; }}
      p {{ color: #cbd5e1; }}
      input {{ background: #0f172a; color: #e5e7eb; border-color: #4b5563; }}
      .error {{ background: #7f1d1d; color: #fee2e2; }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>Murmur Admin</h1>
    <p>Sign in to manage Messenger threads, cookies, and listener status.</p>
    {error_html}
    <form method="post" autocomplete="on">
      <input type="hidden" name="csrf_token" value="{token}">
      <input type="hidden" name="action" value="login">
      <label for="username">Username</label>
      <input id="username" name="username" type="text" value="{username_value}" autocomplete="username" required autofocus>
      <label for="password">Password</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required>
      <button type="submit">Sign In</button>
    </form>
  </main>
</body>
</html>"""


def admin_html(csrf_token: str, message: str = "", error: str = "") -> str:
    token = html.escape(csrf_token)
    status = admin_status()
    threads_html = admin_threads_html(token)
    rows = "\n".join(
        "<tr>"
        f"<th>{html.escape(key.replace('_', ' ').title())}</th>"
        f"<td>{html.escape(value)}</td>"
        "</tr>"
        for key, value in status.items()
    )
    message_html = (
        f'<div class="notice success">{html.escape(message)}</div>' if message else ""
    )
    error_html = f'<div class="notice error">{html.escape(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Murmur Admin</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f6f7f8;
      color: #111827;
    }}
    main {{
      width: min(720px, calc(100vw - 32px));
      margin: 48px auto;
      background: #fff;
      border: 1px solid #d9dde3;
      border-radius: 8px;
      padding: 28px;
      box-shadow: 0 12px 32px rgba(15, 23, 42, 0.08);
    }}
    h1 {{ margin: 0; font-size: 24px; }}
    .topbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 8px;
    }}
    .logout {{
      margin: 0;
    }}
    .logout button {{
      margin: 0;
      background: transparent;
      color: #0f766e;
      border: 1px solid #99c8c2;
      padding: 8px 10px;
    }}
    p {{ color: #4b5563; line-height: 1.5; }}
    h2 {{ margin: 32px 0 8px; font-size: 18px; }}
    section {{ margin-top: 28px; }}
    label {{ display: block; margin: 18px 0 8px; font-weight: 650; }}
    input[type="file"], textarea {{
      width: 100%;
      box-sizing: border-box;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      padding: 10px;
      background: #fff;
      color: #111827;
    }}
    textarea {{ min-height: 160px; resize: vertical; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }}
    button {{
      margin-top: 18px;
      border: 0;
      border-radius: 6px;
      background: #0f766e;
      color: white;
      font-weight: 700;
      padding: 10px 14px;
      cursor: pointer;
    }}
    table {{ width: 100%; margin-top: 24px; border-collapse: collapse; }}
    th, td {{ border-top: 1px solid #e5e7eb; padding: 10px 0; text-align: left; vertical-align: top; }}
    th {{ width: 160px; color: #374151; }}
    code {{ font-size: 12px; color: #4b5563; }}
    .threads th:first-child, .threads td:first-child {{ width: 48px; }}
    .muted {{ color: #6b7280; }}
    .notice {{ margin: 18px 0; padding: 12px; border-radius: 6px; }}
    .success {{ background: #ecfdf5; color: #065f46; }}
    .error {{ background: #fef2f2; color: #991b1b; }}
    @media (prefers-color-scheme: dark) {{
      body {{ background: #0f172a; color: #e5e7eb; }}
      main {{ background: #111827; border-color: #374151; }}
      p {{ color: #cbd5e1; }}
      input[type="file"], textarea {{ background: #0f172a; color: #e5e7eb; border-color: #4b5563; }}
      th, td {{ border-color: #374151; }}
      th {{ color: #cbd5e1; }}
      code {{ color: #cbd5e1; }}
      .logout button {{ color: #5eead4; border-color: #0f766e; }}
      .success {{ background: #064e3b; color: #d1fae5; }}
      .error {{ background: #7f1d1d; color: #fee2e2; }}
    }}
  </style>
</head>
<body>
  <main>
    <div class="topbar">
      <h1>Murmur Admin</h1>
      <form class="logout" method="post">
        <input type="hidden" name="csrf_token" value="{token}">
        <input type="hidden" name="action" value="logout">
        <button type="submit">Sign Out</button>
      </form>
    </div>
    {message_html}
    {error_html}
    {threads_html}
    <section>
    <h2>Facebook Cookies</h2>
    <p>Upload a fresh Facebook cookie JSON file. Murmur writes it to the active cookie path and restarts only the Messenger listener.</p>
    <form method="post" enctype="multipart/form-data">
      <input type="hidden" name="csrf_token" value="{token}">
      <input type="hidden" name="action" value="upload_cookies">
      <label for="cookies_file">Cookie JSON file</label>
      <input id="cookies_file" name="cookies_file" type="file" accept=".json,application/json">
      <label for="cookies_json">Or paste cookie JSON</label>
      <textarea id="cookies_json" name="cookies_json" spellcheck="false"></textarea>
      <button type="submit">Save Cookies</button>
    </form>
    </section>
    <section>
    <h2>Runtime Status</h2>
    <table>{rows}</table>
    <form method="post">
      <input type="hidden" name="csrf_token" value="{token}">
      <input type="hidden" name="action" value="restart_murmur">
      <button type="submit">Restart Messenger Listener</button>
    </form>
    </section>
  </main>
</body>
</html>"""


async def admin_get(request: web.Request) -> web.Response:
    return web.Response(
        text=admin_html(request.app["admin_csrf_token"]),
        content_type="text/html",
        headers=no_store_headers(),
    )


async def admin_login_post(request: web.Request, form) -> web.Response:
    if str(form.get("csrf_token") or "") != request.app["admin_csrf_token"]:
        return web.Response(
            text=admin_login_html(
                request.app["admin_csrf_token"],
                error="Invalid login token. Refresh and try again.",
            ),
            content_type="text/html",
            status=400,
            headers=no_store_headers(),
        )

    username = str(form.get("username") or "")
    password = str(form.get("password") or "")
    if not (
        secrets.compare_digest(username, admin_username())
        and secrets.compare_digest(password, admin_password())
    ):
        return web.Response(
            text=admin_login_html(
                request.app["admin_csrf_token"],
                error="Invalid username or password.",
                username=username,
            ),
            content_type="text/html",
            status=401,
            headers=no_store_headers(),
        )

    response = web.HTTPSeeOther(admin_redirect_location(request))
    response.set_cookie(
        admin_session_cookie_name(),
        make_admin_session(username),
        max_age=admin_session_seconds(),
        path=admin_base_path(),
        httponly=True,
        secure=admin_cookie_secure(request),
        samesite="Strict",
    )
    return response


def admin_logout_response(request: web.Request) -> web.Response:
    response = web.HTTPSeeOther(admin_redirect_location(request), headers=no_store_headers())
    response.del_cookie(admin_session_cookie_name(), path=admin_base_path())
    response.del_cookie(admin_session_cookie_name(), path="/")
    return response


async def admin_post(request: web.Request, form=None) -> web.Response:
    try:
        if form is None:
            form = await request.post()
        action = str(form.get("action") or "upload_cookies")
        if action == "logout":
            return admin_logout_response(request)

        if str(form.get("csrf_token") or "") != request.app["admin_csrf_token"]:
            raise ValueError("Invalid admin form token. Refresh the page and try again.")

        if action == "save_threads":
            allowed_ids = {
                str(thread_id).strip()
                for thread_id in form.getall("allowed_thread_ids", [])
                if str(thread_id).strip()
            }
            allowed_ids.update(parse_thread_ids(str(form.get("extra_thread_ids") or "")))
            write_thread_allowlist(allowed_ids, thread_allowlist_path())
            message = f"Saved thread access for {len(allowed_ids)} allowed thread(s)."
            print(
                "MURMUR_ADMIN_THREAD_ACCESS_UPDATE "
                f"allowed_count={len(allowed_ids)}",
                flush=True,
            )
            return web.Response(
                text=admin_html(request.app["admin_csrf_token"], message=message),
                content_type="text/html",
                headers=no_store_headers(),
            )

        if action == "restart_murmur":
            message = restart_murmur_listener()
            print(
                "MURMUR_ADMIN_RESTART_REQUEST "
                f"result={compact_log_value(message)}",
                flush=True,
            )
            return web.Response(
                text=admin_html(request.app["admin_csrf_token"], message=message),
                content_type="text/html",
                headers=no_store_headers(),
            )

        if action != "upload_cookies":
            raise ValueError(f"Unknown admin action: {action}")

        raw_text = str(form.get("cookies_json") or "").strip()
        file_field = form.get("cookies_file")
        if getattr(file_field, "file", None):
            raw_bytes = file_field.file.read()
            raw_text = raw_bytes.decode("utf-8-sig")

        if not raw_text:
            raise ValueError("Upload a cookie JSON file or paste cookie JSON.")

        cookies, summary = validate_cookie_payload(raw_text)
        path = write_cookie_file(cookies)
        state_sync_message = await asyncio.to_thread(persist_cookie_state, cookies)
        restart_message = " " + restart_murmur_listener()
        message = f"Saved {summary} to {path}. {state_sync_message}{restart_message}"
        print(
            f"MURMUR_ADMIN_COOKIE_UPLOAD {summary} path={path} "
            f"state_sync={compact_log_value(state_sync_message)} "
            f"restart={compact_log_value(restart_message.strip())}",
            flush=True,
        )
        return web.Response(
            text=admin_html(request.app["admin_csrf_token"], message=message),
            content_type="text/html",
            headers=no_store_headers(),
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return web.Response(
            text=admin_html(request.app["admin_csrf_token"], error=str(exc)),
            content_type="text/html",
            status=400,
            headers=no_store_headers(),
        )


async def maybe_handle_admin_console(request: web.Request) -> web.Response | None:
    base_path = admin_base_path()
    if request.path not in {base_path, f"{base_path}/"}:
        return None

    if not admin_console_enabled():
        return web.Response(text="Not found.", status=404)

    config_error = admin_missing_config_response()
    if config_error is not None:
        return config_error

    if request.method == "GET":
        if admin_authenticated(request):
            return await admin_get(request)
        return web.Response(
            text=admin_login_html(request.app["admin_csrf_token"]),
            content_type="text/html",
            headers=no_store_headers(),
        )
    if request.method == "POST":
        form = await request.post()
        action = str(form.get("action") or "")
        if action == "login":
            return await admin_login_post(request, form)
        if not admin_authenticated(request):
            return web.Response(
                text=admin_login_html(
                    request.app["admin_csrf_token"],
                    error="Sign in again to continue.",
                ),
                content_type="text/html",
                status=401,
                headers=no_store_headers(),
            )
        return await admin_post(request, form)
    return web.Response(
        text="Method not allowed.",
        status=405,
        headers=no_store_headers(),
    )


async def cloudflare_image_proxy_models(request: web.Request) -> web.Response:
    auth_error = image_proxy_auth_error(request)
    if auth_error is not None:
        return auth_error

    config_error = image_proxy_config_error()
    if config_error is not None:
        return config_error

    model = cloudflare_image_model()
    return web.json_response(
        {
            "object": "list",
            "data": [
                {
                    "id": model,
                    "object": "model",
                    "owned_by": "cloudflare",
                }
            ],
        }
    )


async def cloudflare_image_proxy_generations(request: web.Request) -> web.Response:
    if request.method != "POST":
        return web.json_response({"error": {"message": "Method not allowed."}}, status=405)

    auth_error = image_proxy_auth_error(request)
    if auth_error is not None:
        return auth_error

    config_error = image_proxy_config_error()
    if config_error is not None:
        return config_error

    try:
        payload = await request.json()
    except ValueError:
        return openai_error("Request body must be JSON.", status=400)

    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return openai_error("Missing required field: prompt.", status=400)

    max_images = parse_positive_int(os.getenv("IMAGE_PROXY_MAX_IMAGES"), 1, 4)
    image_count = parse_positive_int(payload.get("n"), 1, max_images)
    steps = parse_positive_int(payload.get("steps") or os.getenv("IMAGE_STEPS"), 4, 8)

    results = []
    for _ in range(image_count):
        generated = await generate_cloudflare_image(
            request.app["session"],
            prompt=prompt,
            steps=steps,
        )
        if isinstance(generated, web.Response):
            return generated
        results.append({"b64_json": generated})

    return web.json_response({"created": int(time.time()), "data": results})


async def generate_cloudflare_image(
    session: aiohttp.ClientSession, prompt: str, steps: int
) -> str | web.Response:
    account_id = os.environ["CF_ACCOUNT_ID"]
    api_token = os.environ["CF_API_TOKEN"]
    model = cloudflare_image_model()
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"

    try:
        async with session.post(
            url,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            },
            json={"prompt": prompt, "steps": steps},
        ) as response:
            content_type = response.headers.get("Content-Type", "")
            if response.status >= 400:
                body = await response.text()
                log_image_proxy_error(
                    prompt,
                    "cloudflare_status="
                    f"{response.status} content_type={content_type} body={body}",
                )
                return openai_error(
                    f"Cloudflare image generation failed ({response.status}): {body}",
                    status=response.status if response.status < 500 else 502,
                )

            if content_type.startswith("image/"):
                return base64.b64encode(await response.read()).decode("ascii")

            body = await response.json(content_type=None)
    except (ClientError, asyncio.TimeoutError) as exc:
        log_image_proxy_error(prompt, f"request_failed={exc}")
        return openai_error(f"Cloudflare image generation request failed: {exc}")
    except ValueError as exc:
        log_image_proxy_error(prompt, f"invalid_json={exc}")
        return openai_error(f"Cloudflare returned an invalid JSON response: {exc}")

    result = body.get("result", body) if isinstance(body, dict) else {}
    image = result.get("image") if isinstance(result, dict) else None
    if isinstance(image, str) and image:
        return image

    errors = body.get("errors") if isinstance(body, dict) else None
    log_image_proxy_error(
        prompt,
        f"missing_image errors={errors or body}",
    )
    return openai_error(f"Cloudflare response did not include an image: {errors or body}")


async def maybe_handle_image_proxy(request: web.Request) -> web.Response | None:
    base_path = os.getenv("IMAGE_PROXY_BASE_PATH", "/murmur-image-openai/v1").rstrip("/")
    if request.path == f"{base_path}/models":
        return await cloudflare_image_proxy_models(request)
    if request.path == f"{base_path}/images/generations":
        return await cloudflare_image_proxy_generations(request)
    return None


async def proxy_http(request: web.Request) -> web.Response:
    if request.path == "/health":
        return web.json_response({"status": True, "proxy": "ok"})

    url = target_url(request)
    headers = filtered_headers(request.headers.items())
    headers["X-Forwarded-Host"] = request.host
    headers["X-Forwarded-Proto"] = request.scheme

    try:
        async with request.app["session"].request(
            request.method,
            url,
            headers=headers,
            data=await request.read(),
            allow_redirects=False,
        ) as response:
            body = await response.read()
            return web.Response(
                body=body,
                status=response.status,
                headers=filtered_headers(response.headers.items()),
            )
    except (ClientError, asyncio.TimeoutError):
        return web.Response(
            text="Open WebUI is still starting. Try again in a moment.",
            status=503,
        )


async def proxy_websocket(request: web.Request) -> web.WebSocketResponse:
    ws_response = web.WebSocketResponse()
    await ws_response.prepare(request)

    backend_url = target_url(request).replace("http://", "ws://", 1).replace(
        "https://", "wss://", 1
    )

    backend_ws = None
    try:
        backend_ws = await request.app["session"].ws_connect(
            backend_url,
            headers=filtered_headers(request.headers.items()),
        )

        async def client_to_backend() -> None:
            async for message in ws_response:
                if message.type == WSMsgType.TEXT:
                    await backend_ws.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await backend_ws.send_bytes(message.data)
                elif message.type == WSMsgType.CLOSE:
                    await backend_ws.close()

        async def backend_to_client() -> None:
            async for message in backend_ws:
                if message.type == WSMsgType.TEXT:
                    await ws_response.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await ws_response.send_bytes(message.data)
                elif message.type == WSMsgType.CLOSE:
                    await ws_response.close()

        await asyncio.gather(client_to_backend(), backend_to_client())
    except (ClientError, asyncio.TimeoutError):
        await ws_response.close(message=b"Open WebUI is still starting")
    finally:
        if backend_ws is not None:
            with suppress(Exception):
                await backend_ws.close()

    return ws_response


async def handle(request: web.Request) -> web.StreamResponse:
    admin_response = await maybe_handle_admin_console(request)
    if admin_response is not None:
        return admin_response

    image_proxy_response = await maybe_handle_image_proxy(request)
    if image_proxy_response is not None:
        return image_proxy_response

    if request.headers.get("upgrade", "").lower() == "websocket":
        return await proxy_websocket(request)
    return await proxy_http(request)


async def session_context(app: web.Application):
    app["session"] = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=10)
    )
    yield
    await app["session"].close()


def create_app() -> web.Application:
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app["target_base_url"] = os.environ["PROXY_TARGET_BASE_URL"].rstrip("/")
    app["admin_csrf_token"] = secrets.token_urlsafe(32)
    app.cleanup_ctx.append(session_context)
    app.router.add_route("*", "/{path_info:.*}", handle)
    return app


def main() -> None:
    host = os.getenv("PROXY_LISTEN_HOST", "0.0.0.0")
    port = int(os.getenv("PROXY_LISTEN_PORT", "8080"))
    web.run_app(create_app(), host=host, port=port, access_log=None)


if __name__ == "__main__":
    main()
