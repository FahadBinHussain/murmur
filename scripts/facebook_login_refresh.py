from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import shutil
import struct
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]


def configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    load_dotenv(ROOT / ".env")


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}")


def env_path(name: str, default: str) -> Path:
    raw = os.getenv(name) or default
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path


def redact(value: str | None, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * max(4, len(value) - keep) + value[-keep:]


def redacted_url(value: str | None) -> str:
    if not value:
        return ""
    parsed = urlparse(value)
    if not parsed.username and not parsed.password:
        return value
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    auth = "***"
    if parsed.password:
        auth = f"{auth}:***"
    return f"{parsed.scheme}://{auth}@{host}"


def playwright_proxy(proxy_url: str | None) -> dict[str, str] | None:
    if not proxy_url:
        return None
    if proxy_url.strip().lower() in {"direct", "none", "off", "false"}:
        return None

    parsed = urlparse(proxy_url)
    if not parsed.scheme or not parsed.hostname:
        raise SystemExit(f"Invalid FB_LOGIN_PROXY/FB_PROXY URL: {redacted_url(proxy_url)}")

    scheme = "socks5" if parsed.scheme.lower() == "socks5h" else parsed.scheme.lower()
    server = f"{scheme}://{parsed.hostname}"
    if parsed.port:
        server = f"{server}:{parsed.port}"

    out = {"server": server}
    if parsed.username:
        out["username"] = unquote(parsed.username)
    if parsed.password:
        out["password"] = unquote(parsed.password)
    return out


def fbchat_cookie(cookie: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": cookie["name"],
        "value": cookie["value"],
        "domain": cookie.get("domain") or ".facebook.com",
        "path": cookie.get("path") or "/",
    }
    for key in ("secure", "httpOnly", "sameSite"):
        if key in cookie:
            out[key] = cookie[key]
    expires = cookie.get("expires")
    if isinstance(expires, (int, float)) and expires > 0:
        out["expirationDate"] = expires
    return out


def required_cookie_names(cookies: list[dict[str, Any]]) -> set[str]:
    return {str(cookie.get("name") or "") for cookie in cookies}


def has_login_cookies(cookies: list[dict[str, Any]]) -> bool:
    names = required_cookie_names(cookies)
    return "c_user" in names and "xs" in names


async def facebook_cookies(context) -> list[dict[str, Any]]:
    urls = [
        "https://facebook.com",
        "https://www.facebook.com",
        "https://m.facebook.com",
        "https://messenger.com",
        "https://www.messenger.com",
    ]
    cookies = await context.cookies(urls)
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        if "facebook.com" not in domain and "messenger.com" not in domain:
            continue
        key = (str(cookie.get("name") or ""), str(cookie.get("domain") or ""), str(cookie.get("path") or "/"))
        if key in seen:
            continue
        seen.add(key)
        out.append(fbchat_cookie(cookie))
    return out


async def first_visible(page, selectors: list[str], timeout_ms: int = 1500):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=timeout_ms)
            return locator
        except Exception:
            continue
    return None


async def first_visible_locator(locators: list[Any], timeout_ms: int = 1500):
    for locator in locators:
        target = locator.first
        try:
            await target.wait_for(state="visible", timeout=timeout_ms)
            return target
        except Exception:
            continue
    return None


def generate_totp(secret: str, digits: int = 6, period: int = 30) -> str:
    raw = secret.strip()
    if raw.isdigit() and len(raw) == digits:
        return raw
    parsed = urlparse(raw)
    if parsed.scheme.lower() == "otpauth":
        query_secret = parse_qs(parsed.query).get("secret", [""])[0]
        if query_secret:
            raw = query_secret
    elif "secret=" in raw:
        query = raw.split("?", 1)[-1]
        query_secret = parse_qs(query).get("secret", [""])[0]
        if query_secret:
            raw = query_secret

    normalized = "".join(ch for ch in raw.upper() if ch.isalnum())
    if not normalized:
        raise ValueError("FB_LOGIN_TOTP_SECRET is empty.")

    padding = "=" * ((8 - len(normalized) % 8) % 8)
    key = base64.b32decode(normalized + padding, casefold=True)
    counter = int(time.time() // period)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code_int % (10**digits)).zfill(digits)


async def click_submit_or_continue(page) -> bool:
    selectors = [
        'button:has-text("Continue")',
        'div[role="button"]:has-text("Continue")',
        'button:has-text("Submit")',
        'button:has-text("Confirm")',
        'button[name="login"]',
        'button[type="submit"]',
        'input[name="login"]',
        'input[type="submit"]',
    ]
    for selector in selectors:
        submit = await first_visible(page, [selector], timeout_ms=300)
        if submit is None:
            continue
        try:
            await submit.click(timeout=1500)
            return True
        except Exception:
            continue
    return False


async def click_safe_facebook_step(page) -> bool:
    safe_texts = [
        "Continue",
        "This was me",
        "Yes, continue",
        "Yes",
        "Trust this browser",
        "Save browser",
        "Save",
        "Don't save",
        "Not now",
        "Skip",
        "OK",
        "Done",
    ]
    for text in safe_texts:
        selectors = [
            f'button:has-text("{text}")',
            f'div[role="button"]:has-text("{text}")',
            f'a[role="button"]:has-text("{text}")',
            f'input[type="submit"][value="{text}"]',
        ]
        target = await first_visible(page, selectors, timeout_ms=250)
        if target is None:
            continue
        try:
            await target.click()
            print(f"Clicked Facebook step: {text}")
            return True
        except Exception:
            continue
    return False


async def fill_login_form(page, identifier: str, password: str) -> bool:
    email_box = await first_visible(
        page,
        [
            'input[name="email"]',
            "#email",
            'input[type="email"]',
            'input[autocomplete="username"]',
            'input[type="text"]',
        ],
    )
    password_box = await first_visible(
        page,
        [
            'input[name="pass"]',
            "#pass",
            'input[type="password"]',
            'input[autocomplete="current-password"]',
        ],
    )

    if email_box is None or password_box is None:
        return False

    await email_box.fill(identifier)
    await password_box.fill(password)

    if not await click_submit_or_continue(page):
        await password_box.press("Enter")
    return True


async def submit_totp_if_needed(page, totp_secret: str, last_code: str | None) -> str | None:
    if not totp_secret:
        return last_code

    code_box = await first_visible_locator(
        [
            page.locator('input[name="approvals_code"]'),
            page.locator("#approvals_code"),
            page.locator('input[autocomplete="one-time-code"]'),
            page.get_by_placeholder("Code"),
            page.get_by_label("Code"),
            page.locator('input[aria-label*="code" i]'),
            page.locator('input[inputmode="numeric"]'),
            page.locator('input[maxlength="6"]'),
            page.locator('input[type="tel"]'),
            page.locator('input[name="checkpoint_data"]'),
            page.get_by_role("textbox"),
        ],
        timeout_ms=500,
    )
    if code_box is None:
        return last_code

    code = generate_totp(totp_secret)
    if code == last_code:
        return last_code

    await code_box.fill(code)
    await page.wait_for_timeout(300)
    if not await click_submit_or_continue(page):
        await code_box.press("Enter")
    print("Submitted authenticator code.")
    return code


async def wait_for_login(context, page, timeout_seconds: int, totp_secret: str) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    last_totp_code: str | None = None
    last_status_log = 0.0
    while time.monotonic() < deadline:
        try:
            cookies = await facebook_cookies(context)
        except Exception as exc:
            if "Target page, context or browser has been closed" in str(exc):
                raise RuntimeError("Browser closed before Facebook login cookies were exported.") from exc
            raise
        if has_login_cookies(cookies):
            return cookies
        if time.monotonic() - last_status_log > 20:
            try:
                print(f"Waiting on Facebook page: {page.url}")
            except Exception:
                pass
            last_status_log = time.monotonic()
        last_totp_code = await submit_totp_if_needed(page, totp_secret, last_totp_code)
        await click_safe_facebook_step(page)
        await asyncio.sleep(2)
    screenshot_path = ROOT / "output" / "facebook_login_refresh_timeout.png"
    try:
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"Saved timeout screenshot: {screenshot_path}")
    except Exception as exc:
        print(f"Could not save timeout screenshot: {exc}")
    raise TimeoutError(
        "Timed out waiting for Facebook login cookies c_user and xs. "
        "Finish any checkpoint or 2FA in the browser, then run again."
    )


def write_cookies(path: Path, cookies: list[dict[str, Any]], backup: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_suffix(path.suffix + f".bak-{stamp}"))
    path.write_text(json.dumps(cookies, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


async def verify_with_fbchat(path: Path, user_agent: str | None, proxy: str | None) -> None:
    from fbchat_muqit.state import State

    state = None
    try:
        state = await State.from_json_cookies(str(path), user_agent, proxy)
        who = state.user_name or state.user_id
        print(f"Verified with fbchat-muqit as {who} ({state.user_id}).")
    finally:
        if state is not None:
            await state.close()


def persist_to_db(cookies: list[dict[str, Any]]) -> None:
    from murmur.runtime_state import persist_cookie_state

    message = persist_cookie_state(cookies)
    print(message)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open a local browser, log in to Facebook, and export fresh Murmur cookies."
    )
    parser.add_argument("--email", default=os.getenv("FB_LOGIN_EMAIL", ""))
    parser.add_argument("--phone", default=os.getenv("FB_LOGIN_PHONE", ""))
    parser.add_argument("--password", default=os.getenv("FB_LOGIN_PASSWORD", ""))
    parser.add_argument("--totp-secret", default=os.getenv("FB_LOGIN_TOTP_SECRET", ""))
    parser.add_argument("--login-url", default=os.getenv("FB_LOGIN_URL", "https://www.facebook.com/login"))
    parser.add_argument("--profile-dir", type=Path, default=env_path("FB_LOGIN_PROFILE_DIR", ".murmur-facebook-profile"))
    parser.add_argument("--output", type=Path, default=env_path("FB_LOGIN_EXPORT_PATH", os.getenv("FB_COOKIES_PATH", "cookies.json")))
    parser.add_argument("--timeout", type=int, default=env_int("FB_LOGIN_TIMEOUT_SECONDS", 300))
    parser.add_argument("--headless", action="store_true", default=env_bool("FB_LOGIN_HEADLESS", False))
    parser.add_argument("--no-verify", action="store_true", default=not env_bool("FB_LOGIN_VERIFY", True))
    parser.add_argument("--persist-db", action="store_true", default=env_bool("FB_LOGIN_PERSIST_DB", False))
    parser.add_argument("--no-backup", action="store_true", default=not env_bool("FB_LOGIN_BACKUP_EXISTING", True))
    return parser.parse_args()


async def main_async() -> int:
    load_env()
    args = parse_args()

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Playwright is not installed.")
        print("Install locally with:")
        print("  python -m pip install -e .[facebook-login]")
        print("  python -m playwright install chromium")
        return 2

    login_identifier = (args.email or args.phone).strip()
    password = args.password
    totp_secret = str(args.totp_secret or "").strip()
    proxy_url = os.getenv("FB_LOGIN_PROXY") or os.getenv("FB_PROXY") or ""
    verify_proxy = os.getenv("FB_LOGIN_VERIFY_PROXY", proxy_url)
    user_agent = os.getenv("FB_LOGIN_USER_AGENT") or os.getenv("FB_USER_AGENT") or None

    print(f"Browser profile: {args.profile_dir}")
    print(f"Cookie output: {args.output}")
    print(f"Login email/phone: {redact(login_identifier)}")
    print(f"Authenticator secret: {'set' if totp_secret else 'empty'}")
    if proxy_url:
        print(f"Browser proxy: {redacted_url(proxy_url)}")
    print("If Facebook asks for 2FA or checkpoint approval, finish it in the opened browser.")

    async with async_playwright() as p:
        launch_args: dict[str, Any] = {
            "headless": args.headless,
            "viewport": {"width": 1280, "height": 900},
        }
        proxy = playwright_proxy(proxy_url)
        if proxy:
            launch_args["proxy"] = proxy
        if user_agent:
            launch_args["user_agent"] = user_agent

        context = await p.chromium.launch_persistent_context(str(args.profile_dir), **launch_args)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(args.login_url, wait_until="domcontentloaded")

            cookies = await facebook_cookies(context)
            if has_login_cookies(cookies):
                print("Existing browser profile is already logged in.")
            elif login_identifier and password:
                filled = await fill_login_form(page, login_identifier, password)
                if filled:
                    print("Submitted Facebook login form. Waiting for c_user/xs cookies...")
                else:
                    print("Could not find the login form automatically. Use the browser manually.")
                cookies = await wait_for_login(context, page, args.timeout, totp_secret)
            else:
                print("FB_LOGIN_EMAIL/FB_LOGIN_PHONE or FB_LOGIN_PASSWORD is empty. Use the browser manually.")
                cookies = await wait_for_login(context, page, args.timeout, totp_secret)

            write_cookies(args.output, cookies, backup=not args.no_backup)
            names = required_cookie_names(cookies)
            print(f"Exported {len(cookies)} cookies. Required cookies present: c_user={'c_user' in names}, xs={'xs' in names}.")
        finally:
            await context.close()

    if args.persist_db:
        persist_to_db(cookies)

    if not args.no_verify:
        await verify_with_fbchat(args.output, user_agent, verify_proxy or None)

    return 0


def main() -> None:
    configure_console_encoding()
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
