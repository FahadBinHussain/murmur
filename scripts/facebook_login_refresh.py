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


def env_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    values = [item.strip() for item in raw.replace("\n", ",").split(",")]
    return [item for item in values if item]


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


NETWORK_VERIFY_FAILURE_SIGNATURES = (
    "timeouterror",
    "timeout",
    "mqtt endpoint not found",
    "proxy authentication required",
    "clienthttpproxyerror",
    "cannot connect",
    "connection reset",
    "connection refused",
    "connection aborted",
    "temporary failure",
    "network is unreachable",
    "ssl:",
)


def exception_chain_text(exc: BaseException) -> str:
    seen: set[int] = set()
    parts: list[str] = []
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}".lower())
        current = current.__cause__ or current.__context__
    return "\n".join(parts)


def verification_network_failure(exc: BaseException) -> bool:
    text = exception_chain_text(exc)
    return any(signature in text for signature in NETWORK_VERIFY_FAILURE_SIGNATURES)


def should_clear_cookies_after_verify_failure(exc: BaseException) -> bool:
    if verification_network_failure(exc):
        return False
    return env_bool("FB_LOGIN_CLEAR_ON_VERIFY_FAILURE", False)


def fbchat_proxy(proxy_url: str | None) -> str | None:
    if not proxy_url:
        return None
    if proxy_url.strip().lower() in {"direct", "none", "off", "false"}:
        return None
    return proxy_url.strip()


def load_db_proxy_state() -> dict[str, str]:
    try:
        from murmur.runtime_state import (
            RuntimeStateMissing,
            RuntimeStateNotConfigured,
            load_facebook_proxy_state,
        )
    except Exception:
        return {}

    try:
        return load_facebook_proxy_state()
    except (RuntimeStateMissing, RuntimeStateNotConfigured):
        return {}
    except Exception as exc:
        print(f"DB proxy state unavailable: {exc}")
        return {}


def select_login_proxy() -> tuple[str, str]:
    network_policy = (os.getenv("FB_NETWORK_POLICY") or "").strip().lower()
    if network_policy in {"direct", "none", "off", "false"}:
        return "", "FB_NETWORK_POLICY=direct"

    login_proxy = os.getenv("FB_LOGIN_PROXY")
    if login_proxy is not None and login_proxy.strip():
        return login_proxy.strip(), "FB_LOGIN_PROXY"

    db_proxy = load_db_proxy_state().get("FB_PROXY", "").strip()
    if db_proxy:
        return db_proxy, "DB FB_PROXY"

    env_proxy = os.getenv("FB_PROXY", "").strip()
    if env_proxy:
        return env_proxy, "FB_PROXY"

    return "", "direct"


async def prepare_webshare_proxy_state() -> None:
    try:
        from murmur.webshare_proxy import (
            ensure_webshare_proxy_state,
            network_policy,
            webshare_api_key,
        )
    except Exception as exc:
        print(f"Webshare proxy manager unavailable: {compact_exception(exc)}")
        return

    policy = network_policy()
    if policy == "direct":
        return
    if policy == "auto" and not webshare_api_key():
        return
    await ensure_webshare_proxy_state()


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


def normalize_browser_engine(value: str | None) -> str:
    raw = (value or "playwright").strip().lower()
    if raw in {"cloak", "cloakbrowser", "cloak-browser"}:
        return "cloakbrowser"
    if raw in {"playwright", "chromium"}:
        return "playwright"
    raise SystemExit("FB_LOGIN_BROWSER_ENGINE must be playwright or cloakbrowser.")


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


def playwright_cookie(cookie: dict[str, Any]) -> dict[str, Any] | None:
    name = str(cookie.get("name") or "").strip()
    value = str(cookie.get("value") or "")
    domain = str(cookie.get("domain") or "").strip()
    if not name or not domain:
        return None

    out: dict[str, Any] = {
        "name": name,
        "value": value,
        "domain": domain,
        "path": str(cookie.get("path") or "/"),
    }
    for source, target in (("secure", "secure"), ("httpOnly", "httpOnly")):
        if source in cookie:
            out[target] = bool(cookie[source])
    same_site = str(cookie.get("sameSite") or "").strip().lower()
    if same_site in {"strict", "lax", "none"}:
        out["sameSite"] = same_site.capitalize()
    expires = cookie.get("expires", cookie.get("expirationDate"))
    if isinstance(expires, (int, float)) and expires > 0:
        out["expires"] = expires
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


def load_cookie_list(path: Path) -> tuple[list[dict[str, Any]], str] | None:
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            source = str(path)
        elif env_bool("FB_LOGIN_PROFILE_BOOTSTRAP_DB_COOKIES", True):
            from murmur.runtime_state import load_cookie_state_text

            payload = json.loads(load_cookie_state_text())
            source = "DB cookie state"
        else:
            return None
    except Exception as exc:
        print(f"Cookie bootstrap skipped: {exc}")
        return None

    if isinstance(payload, dict) and isinstance(payload.get("cookies"), list):
        payload = payload["cookies"]
    if not isinstance(payload, list):
        print("Cookie bootstrap skipped: cookie payload is not a list.")
        return None

    cookies = [cookie for cookie in payload if isinstance(cookie, dict)]
    if not cookies:
        print("Cookie bootstrap skipped: cookie payload is empty.")
        return None
    return cookies, source


async def bootstrap_context_cookies(context, path: Path) -> None:
    if not env_bool("FB_LOGIN_PROFILE_BOOTSTRAP_COOKIES", True):
        return
    try:
        current = await facebook_cookies(context)
        if has_login_cookies(current):
            return
    except Exception:
        pass

    loaded = load_cookie_list(path)
    if loaded is None:
        return
    cookies, source = loaded
    converted = [item for item in (playwright_cookie(cookie) for cookie in cookies) if item]
    if not converted:
        print("Cookie bootstrap skipped: no browser-compatible Facebook cookies found.")
        return
    try:
        await context.add_cookies(converted)
        print(f"Bootstrapped browser profile with {len(converted)} cookies from {source}.")
    except Exception as exc:
        print(f"Cookie bootstrap failed: {exc}")


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


def page_scopes(page) -> list[Any]:
    scopes: list[Any] = [page]
    try:
        main_frame = page.main_frame
        for frame in page.frames:
            if frame != main_frame:
                scopes.append(frame)
    except Exception:
        pass
    return scopes


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
        'button[name="login"]',
        'input[name="login"]',
        'button:has-text("Log in")',
        'div[role="button"]:has-text("Log in")',
        'button:has-text("Login")',
        'div[role="button"]:has-text("Login")',
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Submit")',
        'button:has-text("Confirm")',
        'button:has-text("Continue")',
        'div[role="button"]:has-text("Continue")',
    ]
    for scope in page_scopes(page):
        for selector in selectors:
            submit = await first_visible(scope, [selector], timeout_ms=300)
            if submit is None:
                continue
            try:
                await submit.click(timeout=1500)
                return True
            except Exception:
                continue
    return False


async def click_cookie_consent_continue(page) -> bool:
    cookie_markers = [
        "Allow the use of cookies",
        "We use cookies and similar technologies",
        "cookies from Facebook on this browser",
    ]
    for scope in page_scopes(page):
        try:
            body = await scope.locator("body").inner_text(timeout=500)
        except Exception:
            body = ""
        if not any(marker.lower() in body.lower() for marker in cookie_markers):
            continue

        for selector in [
            'button:has-text("Allow all cookies")',
            'div[role="button"]:has-text("Allow all cookies")',
            'button:has-text("Accept all")',
            'div[role="button"]:has-text("Accept all")',
            'button:has-text("Continue")',
            'div[role="button"]:has-text("Continue")',
        ]:
            try:
                locator = scope.locator(selector)
                count = await locator.count()
            except Exception:
                continue
            # Facebook can render both profile Continue and cookie Continue.
            # The consent action usually appears lower in the DOM, so prefer the last visible match.
            for index in range(count - 1, -1, -1):
                target = locator.nth(index)
                try:
                    if not await target.is_visible(timeout=250):
                        continue
                    await target.click(timeout=1500)
                    print("Clicked Facebook cookie consent step.")
                    return True
                except Exception:
                    continue
    return False


async def click_automated_behavior_warning_dismiss(page) -> bool:
    warning_markers = (
        "we suspect automated behaviour on your account",
        "we suspect automated behavior on your account",
    )
    for scope in page_scopes(page):
        try:
            body = await scope.locator("body").inner_text(timeout=500)
        except Exception:
            continue
        normalized = " ".join(body.lower().split())
        if not any(marker in normalized for marker in warning_markers):
            continue

        for selector in [
            'button:has-text("Dismiss")',
            'div[role="button"]:has-text("Dismiss")',
            'a[role="button"]:has-text("Dismiss")',
            'input[type="submit"][value="Dismiss"]',
        ]:
            target = await first_visible(scope, [selector], timeout_ms=250)
            if target is None:
                continue
            try:
                await target.click(timeout=1500)
                print("Dismissed Facebook automated-behaviour checkpoint warning.")
                return True
            except Exception:
                continue
    return False


async def click_safe_facebook_step(page) -> bool:
    safe_texts = [
        "Continue",
        "Try another way",
        "Try another method",
        "Use another method",
        "Choose another way",
        "Choose another method",
        "Get a code",
        "Enter code",
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
    for scope in page_scopes(page):
        for text in safe_texts:
            selectors = [
                f'button:has-text("{text}")',
                f'div[role="button"]:has-text("{text}")',
                f'a[role="button"]:has-text("{text}")',
                f'input[type="submit"][value="{text}"]',
            ]
            target = await first_visible(scope, selectors, timeout_ms=250)
            if target is None:
                continue
            try:
                await target.click()
                print(f"Clicked Facebook step: {text}")
                return True
            except Exception:
                continue
    return False


async def facebook_page_debug(page) -> None:
    try:
        url = page.url
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        flow = qs.get("flow", [""])[0]
        compact_url = parsed.path + (f"?flow={flow}" if flow else "")
        title = ""
        html_len = 0
        inputs: list[str] = []
        buttons: list[str] = []
        frames: list[str] = []
        try:
            title = await page.title()
        except Exception:
            title = ""
        try:
            html_len = len(await page.content())
        except Exception:
            html_len = 0
        body = await page.locator("body").inner_text(timeout=1000)
        body = " ".join(body.split())
        if len(body) > 700:
            body = body[:700] + "..."
        try:
            inputs = await page.locator("input").evaluate_all(
                """els => els.slice(0, 10).map(e => [
                    e.getAttribute('name') || '',
                    e.getAttribute('type') || '',
                    e.getAttribute('autocomplete') || '',
                    e.getAttribute('aria-label') || '',
                    e.getAttribute('placeholder') || ''
                ].filter(Boolean).join('/')).filter(Boolean)"""
            )
        except Exception:
            inputs = []
        try:
            buttons = await page.locator("button, div[role='button'], input[type='submit']").evaluate_all(
                """els => els.slice(0, 10).map(e =>
                    (e.innerText || e.getAttribute('aria-label') || e.getAttribute('value') || '')
                        .replace(/\\s+/g, ' ')
                        .trim()
                ).filter(Boolean)"""
            )
        except Exception:
            buttons = []
        try:
            for frame in page.frames[:6]:
                frame_url = urlparse(frame.url)
                frame_text = ""
                try:
                    frame_text = await frame.locator("body").inner_text(timeout=500)
                    frame_text = " ".join(frame_text.split())
                    if len(frame_text) > 120:
                        frame_text = frame_text[:120] + "..."
                except Exception:
                    frame_text = ""
                frames.append(f"{frame_url.netloc}{frame_url.path} text={frame_text or '<empty>'}")
        except Exception:
            frames = []
        print(
            "Facebook page debug: "
            f"{compact_url} title={title!r} html_len={html_len} "
            f"body={body or '<empty>'} inputs={inputs} buttons={buttons} frames={frames}"
        )
    except Exception as exc:
        print(f"Facebook page debug unavailable: {exc}")


def messenger_warmup_urls() -> list[str]:
    urls = env_list(
        "FB_LOGIN_MESSENGER_WARMUP_URLS",
        [
            "https://www.messenger.com/",
            "https://www.facebook.com/messages/t/",
        ],
    )
    thread_url = (os.getenv("FB_LOGIN_MESSENGER_THREAD_URL") or "").strip()
    if thread_url:
        urls.append(thread_url)
    return urls


async def messenger_surface_loaded(page) -> bool:
    url = (page.url or "").lower()
    bad_url_markers = ("checkpoint", "login", "recover", "captcha")
    if any(marker in url for marker in bad_url_markers):
        return False

    messenger_text_markers = (
        "search messenger",
        "new message",
        "message requests",
        "chats",
        "messenger,",
        "unread chats",
    )
    for scope in page_scopes(page):
        try:
            text = await scope.locator("body").inner_text(timeout=700)
        except Exception:
            continue
        normalized = " ".join(text.lower().split())
        if any(marker in normalized for marker in messenger_text_markers):
            return True
    return False


async def warmup_messenger_session(context, page) -> None:
    if not env_bool("FB_LOGIN_MESSENGER_WARMUP", True):
        return

    urls = messenger_warmup_urls()
    if not urls:
        return

    nav_timeout_ms = max(
        1000,
        env_int("FB_LOGIN_MESSENGER_WARMUP_NAV_SECONDS", 45) * 1000,
    )
    wait_seconds = max(0, env_int("FB_LOGIN_MESSENGER_WARMUP_WAIT_SECONDS", 6))
    print("Warming hosted browser on Messenger pages before cookie verification.")
    for url in urls:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
            try:
                await page.wait_for_load_state(
                    "networkidle",
                    timeout=min(nav_timeout_ms, 10000),
                )
            except Exception:
                pass
            await click_cookie_consent_continue(page)
            await click_automated_behavior_warning_dismiss(page)
            if wait_seconds:
                await page.wait_for_timeout(wait_seconds * 1000)
            loaded = await messenger_surface_loaded(page)
            parsed = urlparse(page.url)
            print(
                "Messenger warm-up visited "
                f"{urlparse(url).netloc}{urlparse(url).path}; "
                f"current={parsed.netloc}{parsed.path}; loaded={loaded}."
            )
            if loaded:
                break
        except Exception as exc:
            print(
                "Messenger warm-up failed for "
                f"{urlparse(url).netloc}{urlparse(url).path}: {compact_exception(exc)}"
            )

    try:
        cookies = await facebook_cookies(context)
        names = required_cookie_names(cookies)
        print(
            "Messenger warm-up cookies: "
            f"count={len(cookies)} c_user={'c_user' in names} xs={'xs' in names}."
        )
    except Exception as exc:
        print(f"Messenger warm-up cookie read failed: {compact_exception(exc)}")


async def facebook_captcha_detected(page) -> bool:
    try:
        for frame in page.frames:
            frame_url = frame.url.lower()
            if "recaptcha" in frame_url or "google.com/recaptcha" in frame_url:
                return True
    except Exception:
        pass
    try:
        recaptcha = await page.locator('iframe[src*="recaptcha"], iframe[title*="reCAPTCHA" i]').count()
        return recaptcha > 0
    except Exception:
        return False


async def click_authentication_app_option(page) -> bool:
    option_texts = [
        "Authentication app",
        "Authenticator app",
        "Use authentication app",
        "Use an authentication app",
        "Get a code from your authentication app",
        "Get code from authentication app",
        "Enter a code from your authentication app",
        "Go to your authentication app",
        "Code generator",
    ]
    for scope in page_scopes(page):
        for text in option_texts:
            locators = [
                scope.get_by_text(text, exact=True),
                scope.get_by_text(text),
                scope.locator(f'div[role="button"]:has-text("{text}")'),
                scope.locator(f'label:has-text("{text}")'),
                scope.locator(f'[role="radio"]:has-text("{text}")'),
            ]
            target = await first_visible_locator(locators, timeout_ms=250)
            if target is None:
                continue
            try:
                await target.click(timeout=1500)
                print(f"Clicked Facebook 2FA option: {text}")
                await page.wait_for_timeout(500)
                await click_submit_or_continue(page)
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

    if password_box is None:
        return False

    if email_box is not None:
        await email_box.fill(identifier, timeout=2000)
    await password_box.fill(password, timeout=2000)

    if not await click_submit_or_continue(page):
        await password_box.press("Enter")
    return True


async def facebook_totp_page(page) -> bool:
    url = (page.url or "").lower()
    auth_url_markers = (
        "two_step_verification",
        "two_factor",
        "checkpoint",
        "approvals",
        "login/reauth",
    )
    if any(marker in url for marker in auth_url_markers):
        return True

    auth_text_markers = (
        "go to your authentication app",
        "authentication app",
        "authenticator app",
        "two-factor authentication",
        "enter the 6-digit code",
        "enter a code from your authentication app",
        "code generator",
    )
    home_text_markers = (
        "what's on your mind",
        "facebook menu",
        "your shortcuts",
        "messenger,",
        "search messenger",
    )
    for scope in page_scopes(page):
        try:
            text = await scope.locator("body").inner_text(timeout=500)
        except Exception:
            continue
        normalized = " ".join(text.lower().split())
        if any(marker in normalized for marker in home_text_markers):
            return False
        if any(marker in normalized for marker in auth_text_markers):
            return True
    return False


async def submit_totp_if_needed(page, totp_secret: str, last_code: str | None) -> str | None:
    if not totp_secret:
        return last_code
    if not await facebook_totp_page(page):
        return last_code
    for scope in page_scopes(page):
        login_password = await first_visible(
            scope,
            [
                'input[name="pass"]',
                "#pass",
                'input[type="password"]',
                'input[autocomplete="current-password"]',
            ],
            timeout_ms=100,
        )
        if login_password is not None:
            return last_code

    code_box = None
    for scope in page_scopes(page):
        code_box = await first_visible_locator(
            [
                scope.locator('input[name="approvals_code"]'),
                scope.locator("#approvals_code"),
                scope.locator('input[autocomplete="one-time-code"]'),
                scope.get_by_placeholder("Code"),
                scope.get_by_label("Code"),
                scope.locator('input[aria-label*="code" i]'),
                scope.locator('input[inputmode="numeric"]'),
                scope.locator('input[maxlength="6"]'),
                scope.locator('input[type="tel"]'),
                scope.locator('input[name="checkpoint_data"]'),
                scope.get_by_role("textbox"),
            ],
            timeout_ms=500,
        )
        if code_box is not None:
            break
    if code_box is None:
        return last_code

    # If input already has 6-digit value and is disabled/busy, code was already entered.
    try:
        current_value = await code_box.input_value(timeout=500)
        if current_value and len(current_value.strip()) >= 6 and current_value.strip().isdigit():
            is_disabled = await code_box.is_disabled()
            if is_disabled:
                await asyncio.sleep(2)
            if not await click_submit_or_continue(page):
                await page.keyboard.press("Enter")
            print("Submitted authenticator code.")
            return generate_totp(totp_secret)
    except Exception:
        pass

    code = generate_totp(totp_secret)
    if code == last_code:
        return last_code

    try:
        await code_box.fill(code, timeout=2000)
    except Exception:
        return last_code

    await page.wait_for_timeout(300)
    if not await click_submit_or_continue(page):
        try:
            await code_box.press("Enter")
        except Exception:
            return last_code
    print("Submitted authenticator code.")
    return code


async def wait_for_login(
    context,
    page,
    timeout_seconds: int,
    totp_secret: str,
    login_identifier: str = "",
    password: str = "",
    headless: bool = False,
    require_verified: bool = False,
    verify_output_path: Path | None = None,
    verify_user_agent: str | None = None,
    verify_proxy: str | None = None,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    last_totp_code: str | None = None
    last_status_log = 0.0
    login_resubmits = 0
    last_login_submit = 0.0
    last_verify_attempt = 0.0
    last_messenger_warmup = 0.0
    last_verify_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            cookies = await facebook_cookies(context)
        except Exception as exc:
            if "Target page, context or browser has been closed" in str(exc):
                raise RuntimeError("Browser closed before Facebook login cookies were exported.") from exc
            raise
        if await click_automated_behavior_warning_dismiss(page):
            last_verify_attempt = 0.0
            await asyncio.sleep(2)
            continue
        if has_login_cookies(cookies):
            if not require_verified:
                url = (page.url or "").lower()
                auth_url_markers = ("two_step_verification", "two_factor", "checkpoint", "approvals", "login/reauth")
                if not any(m in url for m in auth_url_markers):
                    return cookies
            if time.monotonic() - last_verify_attempt > 15:
                last_verify_attempt = time.monotonic()
                try:
                    await verify_cookie_candidate(
                        cookies,
                        verify_output_path or (ROOT / "cookies.json"),
                        verify_user_agent,
                        verify_proxy,
                    )
                    return cookies
                except Exception as exc:
                    last_verify_error = exc
                    print(
                        "Facebook cookies are present but fbchat-muqit verification still fails: "
                        f"{compact_exception(exc)}"
                    )
                    warmup_interval = max(
                        15,
                        env_int("FB_LOGIN_MESSENGER_WARMUP_INTERVAL_SECONDS", 45),
                    )
                    if time.monotonic() - last_messenger_warmup > warmup_interval:
                        last_messenger_warmup = time.monotonic()
                        await warmup_messenger_session(context, page)
                        last_verify_attempt = 0.0
                        await asyncio.sleep(1)
                        continue
        if await facebook_captcha_detected(page):
            await facebook_page_debug(page)
            screenshot_path = ROOT / "output" / "facebook_login_refresh_captcha.png"
            try:
                screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(screenshot_path), full_page=True)
                print(f"Saved reCAPTCHA screenshot: {screenshot_path}")
            except Exception as exc:
                print(f"Could not save reCAPTCHA screenshot: {exc}")
            retry_hint = (
                "Run the same command with --headed, solve the visible challenge once, "
                "and let the script persist the refreshed trusted profile."
                if headless
                else "Solve the visible challenge in the browser and let the script continue."
            )
            raise RuntimeError(
                "Facebook login blocked by reCAPTCHA during fresh login. "
                f"{retry_hint} Hosted auto-refresh cannot solve this challenge "
                "without an already trusted profile."
            )
        if time.monotonic() - last_status_log > 20:
            try:
                print(f"Waiting on Facebook page: {page.url}")
                await facebook_page_debug(page)
            except Exception:
                pass
            last_status_log = time.monotonic()
        last_totp_code = await submit_totp_if_needed(page, totp_secret, last_totp_code)
        await click_authentication_app_option(page)
        if await click_cookie_consent_continue(page):
            await asyncio.sleep(2)
            continue
        if await click_automated_behavior_warning_dismiss(page):
            await asyncio.sleep(2)
            continue
        if (
            login_identifier
            and password
            and login_resubmits < 3
            and time.monotonic() - last_login_submit > 15
            and await fill_login_form(page, login_identifier, password)
        ):
            login_resubmits += 1
            last_login_submit = time.monotonic()
            print(f"Resubmitted Facebook login form after redirect ({login_resubmits}/3).")
            await asyncio.sleep(3)
            continue
        await click_safe_facebook_step(page)
        await asyncio.sleep(2)
    screenshot_path = ROOT / "output" / "facebook_login_refresh_timeout.png"
    try:
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"Saved timeout screenshot: {screenshot_path}")
    except Exception as exc:
        print(f"Could not save timeout screenshot: {exc}")
    if require_verified:
        last_error = (
            f" Last verification error: {compact_exception(last_verify_error)}."
            if last_verify_error
            else ""
        )
        raise TimeoutError(
            "Timed out waiting for Facebook cookies to pass fbchat-muqit verification."
            f"{last_error} Finish any checkpoint, reCAPTCHA, or 2FA in the browser, then run again."
        )
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
    from murmur.fbchat_patch import apply_fbchat_patches
    from fbchat_muqit.muqit import Mqtt
    from fbchat_muqit.state import State

    apply_fbchat_patches()
    state = None
    try:
        state = await State.from_json_cookies(str(path), user_agent, proxy)
        sequence_id = await Mqtt._fetch_sequence_id(state)
        who = state.user_name or state.user_id
        print(
            f"Verified with fbchat-muqit as {who} ({state.user_id}); "
            f"sequence_id={sequence_id}."
        )
    finally:
        if state is not None:
            await state.close()


async def verify_cookie_candidate(
    cookies: list[dict[str, Any]],
    path: Path,
    user_agent: str | None,
    proxy: str | None,
) -> None:
    verify_path = path.with_name(path.name + ".verify")
    write_cookies(verify_path, cookies, backup=False)
    try:
        await verify_with_fbchat(verify_path, user_agent, proxy)
    finally:
        try:
            verify_path.unlink()
        except FileNotFoundError:
            pass


def compact_exception(exc: BaseException) -> str:
    text = str(exc).strip().replace("\n", " ")
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def configure_page_timeouts(page: Any, timeout_seconds: int) -> None:
    timeout_ms = max(1000, timeout_seconds * 1000)
    try:
        page.set_default_timeout(timeout_ms)
    except Exception:
        pass
    try:
        page.set_default_navigation_timeout(timeout_ms)
    except Exception:
        pass


def persist_to_db(cookies: list[dict[str, Any]]) -> None:
    from murmur.runtime_state import persist_cookie_state

    message = persist_cookie_state(cookies)
    print(message)


def persist_profile_to_db(profile_dir: Path) -> None:
    from murmur.runtime_state import persist_facebook_profile_state

    message = persist_facebook_profile_state(profile_dir)
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
    parser.add_argument("--browser-engine", default=os.getenv("FB_LOGIN_BROWSER_ENGINE", "playwright"))
    parser.add_argument("--profile-dir", type=Path, default=env_path("FB_LOGIN_PROFILE_DIR", ".murmur-facebook-profile"))
    parser.add_argument("--output", type=Path, default=env_path("FB_LOGIN_EXPORT_PATH", os.getenv("FB_COOKIES_PATH", "cookies.json")))
    parser.add_argument("--timeout", type=int, default=env_int("FB_LOGIN_TIMEOUT_SECONDS", 300))
    parser.add_argument("--nav-timeout", type=int, default=env_int("FB_LOGIN_NAV_TIMEOUT_SECONDS", 120))
    parser.add_argument("--headless", action="store_true", default=env_bool("FB_LOGIN_HEADLESS", False))
    parser.add_argument("--headed", action="store_false", dest="headless")
    parser.add_argument("--no-verify", action="store_true", default=not env_bool("FB_LOGIN_VERIFY", True))
    parser.add_argument("--persist-db", action="store_true", default=env_bool("FB_LOGIN_PERSIST_DB", False))
    parser.add_argument(
        "--persist-profile-db",
        action="store_true",
        default=env_bool("FB_LOGIN_PROFILE_PERSIST_DB", False),
    )
    parser.add_argument("--no-persist-profile-db", action="store_false", dest="persist_profile_db")
    parser.add_argument("--no-backup", action="store_true", default=not env_bool("FB_LOGIN_BACKUP_EXISTING", True))
    return parser.parse_args()


async def main_async() -> int:
    load_env()
    args = parse_args()
    browser_engine = normalize_browser_engine(args.browser_engine)

    login_identifier = (args.email or args.phone).strip()
    password = args.password
    totp_secret = str(args.totp_secret or "").strip()
    await prepare_webshare_proxy_state()
    proxy_url, proxy_source = select_login_proxy()
    verify_proxy = fbchat_proxy(os.getenv("FB_LOGIN_VERIFY_PROXY") or proxy_url)
    user_agent = os.getenv("FB_LOGIN_USER_AGENT") or os.getenv("FB_USER_AGENT") or None
    verify_existing_profile = env_bool("FB_LOGIN_VERIFY_EXISTING_PROFILE", False)

    print(f"Browser profile: {args.profile_dir}")
    print(f"Browser engine: {browser_engine}")
    print(f"Cookie output: {args.output}")
    print(f"Login email/phone: {redact(login_identifier)}")
    print(f"Authenticator secret: {'set' if totp_secret else 'empty'}")
    print(f"Browser proxy: {redacted_url(proxy_url) if proxy_url else 'direct'}")
    print(f"Browser proxy source: {proxy_source}")
    print("If Facebook asks for 2FA or checkpoint approval, finish it in the opened browser.")

    if browser_engine == "cloakbrowser":
        try:
            from cloakbrowser import launch_persistent_context_async
        except ImportError:
            print("CloakBrowser is not installed.")
            print("Install locally with:")
            print("  python -m pip install -e .[facebook-login]")
            return 2

        launch_args: dict[str, Any] = {
            "headless": args.headless,
            "viewport": {"width": 1280, "height": 900},
            "humanize": True,
        }
        if fbchat_proxy(proxy_url):
            launch_args["proxy"] = proxy_url
        if user_agent:
            launch_args["user_agent"] = user_agent
        context = await launch_persistent_context_async(str(args.profile_dir), **launch_args)
        try:
            await bootstrap_context_cookies(context, args.output)
            page = context.pages[0] if context.pages else await context.new_page()
            configure_page_timeouts(page, args.nav_timeout)

            require_verified_login = False
            cookies = await facebook_cookies(context)
            if has_login_cookies(cookies):
                print("Existing browser profile already has Facebook login cookies.")

                if not args.no_verify and verify_existing_profile:
                    try:
                        await verify_cookie_candidate(cookies, args.output, user_agent, verify_proxy)
                        print("Existing browser profile cookies verified with fbchat-muqit.")
                    except Exception as exc:
                        print(
                            "Existing browser profile cookies failed fbchat-muqit verification: "
                            f"{compact_exception(exc)}"
                        )
                        if should_clear_cookies_after_verify_failure(exc):
                            print("Clearing browser cookies and forcing a fresh Facebook login.")
                            await context.clear_cookies()
                            cookies = []
                        elif verification_network_failure(exc):
                            print(
                                "Keeping browser cookies because verification failed like a "
                                "network/proxy problem, not a cookie-expiry problem."
                            )
                            raise
                        else:
                            print(
                                "Keeping browser cookies and profile trust state. "
                                "Opening Facebook so the existing profile can finish reauthentication."
                            )
                            require_verified_login = True
                            await page.goto(
                                args.login_url,
                                wait_until="domcontentloaded",
                                timeout=max(1000, args.nav_timeout * 1000),
                            )
                elif not args.no_verify:
                    if env_bool("FB_LOGIN_CLEAR_ON_VERIFY_FAILURE", False):
                        print("Clearing browser cookies and forcing a fresh Facebook login.")
                        await context.clear_cookies()
                        cookies = []
                    else:
                        print(
                            "Opening Facebook before verifying existing profile cookies, "
                            "so visible checkpoint steps can complete."
                        )
                        require_verified_login = True
                        await page.goto(
                            args.login_url,
                            wait_until="domcontentloaded",
                            timeout=max(1000, args.nav_timeout * 1000),
                        )

            if not has_login_cookies(cookies):
                await page.goto(
                    args.login_url,
                    wait_until="domcontentloaded",
                    timeout=max(1000, args.nav_timeout * 1000),
                )
                cookies = await facebook_cookies(context)

            if has_login_cookies(cookies) and require_verified_login:
                print("Waiting for browser session to pass fbchat-muqit verification...")
                cookies = await wait_for_login(
                    context,
                    page,
                    args.timeout,
                    totp_secret,
                    login_identifier,
                    password,
                    args.headless,
                    require_verified=True,
                    verify_output_path=args.output,
                    verify_user_agent=user_agent,
                    verify_proxy=verify_proxy,
                )
            elif has_login_cookies(cookies):
                pass
            elif login_identifier and password:
                filled = await fill_login_form(page, login_identifier, password)
                if filled:
                    print("Submitted Facebook login form. Waiting for c_user/xs cookies...")
                else:
                    print("Could not find the login form automatically. Use the browser manually.")
                cookies = await wait_for_login(
                    context,
                    page,
                    args.timeout,
                    totp_secret,
                    login_identifier,
                    password,
                    args.headless,
                )
            else:
                print("FB_LOGIN_EMAIL/FB_LOGIN_PHONE or FB_LOGIN_PASSWORD is empty. Use the browser manually.")
                cookies = await wait_for_login(
                    context,
                    page,
                    args.timeout,
                    totp_secret,
                    login_identifier,
                    password,
                    args.headless,
                )

            write_cookies(args.output, cookies, backup=not args.no_backup)
            names = required_cookie_names(cookies)
            print(f"Exported {len(cookies)} cookies. Required cookies present: c_user={'c_user' in names}, xs={'xs' in names}.")
        finally:
            await context.close()
    else:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            print("Playwright is not installed.")
            print("Install locally with:")
            print("  python -m pip install -e .[facebook-login]")
            print("  python -m playwright install chromium")
            return 2

        async with async_playwright() as p:
            launch_args = {
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
                await bootstrap_context_cookies(context, args.output)
                page = context.pages[0] if context.pages else await context.new_page()
                configure_page_timeouts(page, args.nav_timeout)

                require_verified_login = False
                cookies = await facebook_cookies(context)
                if has_login_cookies(cookies):
                    print("Existing browser profile already has Facebook login cookies.")

                    if not args.no_verify and verify_existing_profile:
                        try:
                            await verify_cookie_candidate(cookies, args.output, user_agent, verify_proxy)
                            print("Existing browser profile cookies verified with fbchat-muqit.")
                        except Exception as exc:
                            print(
                                "Existing browser profile cookies failed fbchat-muqit verification: "
                                f"{compact_exception(exc)}"
                            )
                            if should_clear_cookies_after_verify_failure(exc):
                                print("Clearing browser cookies and forcing a fresh Facebook login.")
                                await context.clear_cookies()
                                cookies = []
                            elif verification_network_failure(exc):
                                print(
                                    "Keeping browser cookies because verification failed like a "
                                    "network/proxy problem, not a cookie-expiry problem."
                                )
                                raise
                            else:
                                print(
                                    "Keeping browser cookies and profile trust state. "
                                    "Opening Facebook so the existing profile can finish reauthentication."
                                )
                                require_verified_login = True
                                await page.goto(
                                    args.login_url,
                                    wait_until="domcontentloaded",
                                    timeout=max(1000, args.nav_timeout * 1000),
                                )
                    elif not args.no_verify:
                        if env_bool("FB_LOGIN_CLEAR_ON_VERIFY_FAILURE", False):
                            print("Clearing browser cookies and forcing a fresh Facebook login.")
                            await context.clear_cookies()
                            cookies = []
                        else:
                            print(
                                "Opening Facebook before verifying existing profile cookies, "
                                "so visible checkpoint steps can complete."
                            )
                            require_verified_login = True
                            await page.goto(
                                args.login_url,
                                wait_until="domcontentloaded",
                                timeout=max(1000, args.nav_timeout * 1000),
                            )

                if not has_login_cookies(cookies):
                    await page.goto(
                        args.login_url,
                        wait_until="domcontentloaded",
                        timeout=max(1000, args.nav_timeout * 1000),
                    )
                    cookies = await facebook_cookies(context)

                if has_login_cookies(cookies) and require_verified_login:
                    print("Waiting for browser session to pass fbchat-muqit verification...")
                    cookies = await wait_for_login(
                        context,
                        page,
                        args.timeout,
                        totp_secret,
                        login_identifier,
                        password,
                        args.headless,
                        require_verified=True,
                        verify_output_path=args.output,
                        verify_user_agent=user_agent,
                        verify_proxy=verify_proxy,
                    )
                elif has_login_cookies(cookies):
                    pass
                elif login_identifier and password:
                    filled = await fill_login_form(page, login_identifier, password)
                    if filled:
                        print("Submitted Facebook login form. Waiting for c_user/xs cookies...")
                    else:
                        print("Could not find the login form automatically. Use the browser manually.")
                    cookies = await wait_for_login(
                        context,
                        page,
                        args.timeout,
                        totp_secret,
                        login_identifier,
                        password,
                        args.headless,
                    )
                else:
                    print("FB_LOGIN_EMAIL/FB_LOGIN_PHONE or FB_LOGIN_PASSWORD is empty. Use the browser manually.")
                    cookies = await wait_for_login(
                        context,
                        page,
                        args.timeout,
                        totp_secret,
                        login_identifier,
                        password,
                        args.headless,
                    )

                write_cookies(args.output, cookies, backup=not args.no_backup)
                names = required_cookie_names(cookies)
                print(f"Exported {len(cookies)} cookies. Required cookies present: c_user={'c_user' in names}, xs={'xs' in names}.")
            finally:
                await context.close()

    if not args.no_verify:
        await verify_with_fbchat(args.output, user_agent, verify_proxy)

    if args.persist_db:
        persist_to_db(cookies)

    if args.persist_profile_db:
        persist_profile_to_db(args.profile_dir)

    return 0


def main() -> None:
    configure_console_encoding()
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
