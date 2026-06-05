from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import aiohttp

from .runtime_state import (
    RuntimeStateMissing,
    RuntimeStateNotConfigured,
    load_facebook_proxy_state,
    persist_facebook_proxy_state,
)


FACEBOOK_PROXY_KEYS = ("FB_PROXY", "FB_UPLOAD_PROXY", "FB_MQTT_PROXY")


class WebshareProxyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProxyCandidate:
    proxy_url: str
    label: str
    raw: dict[str, Any]


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
    except ValueError as exc:
        raise WebshareProxyError(f"{name} must be an integer, got {raw!r}") from exc


def network_policy() -> str:
    policy = (os.getenv("FB_NETWORK_POLICY") or "auto").strip().lower()
    if policy in {"", "auto"}:
        return "auto"
    if policy in {"direct", "none", "off", "false"}:
        return "direct"
    if policy in {"webshare", "proxy", "proxied"}:
        return "webshare"
    raise WebshareProxyError(
        "FB_NETWORK_POLICY must be one of auto, direct, or webshare; "
        f"got {policy!r}"
    )


def webshare_api_key() -> str:
    return (os.getenv("WEBSHARE_API_KEY") or "").strip()


def webshare_proxy_mode() -> str:
    mode = (os.getenv("WEBSHARE_PROXY_MODE") or "direct").strip().lower() or "direct"
    if mode not in {"direct", "backbone"}:
        raise WebshareProxyError(
            "WEBSHARE_PROXY_MODE must be direct or backbone; "
            f"got {mode!r}"
        )
    return mode


def keep_last_proxy_on_rotation_failure() -> bool:
    return env_bool("WEBSHARE_KEEP_LAST_ON_ROTATION_FAILURE", True)


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


def proxy_disabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"", "direct", "none", "off", "false"}


def proxy_explicitly_direct(value: str | None) -> bool:
    return (value or "").strip().lower() in {"direct", "none", "off", "false"}


def proxy_matches_mode(proxy_url: str, mode: str) -> bool:
    host = (urlparse(proxy_url).hostname or "").lower()
    backbone_host = (os.getenv("WEBSHARE_BACKBONE_HOST") or "p.webshare.io").lower()
    if mode == "backbone":
        return host == backbone_host
    return host != backbone_host


def load_stored_proxy_state() -> dict[str, str]:
    try:
        return load_facebook_proxy_state()
    except (RuntimeStateMissing, RuntimeStateNotConfigured):
        return {}
    except Exception as exc:
        print(f"Webshare proxy manager could not load DB proxy state: {exc}", flush=True)
        return {}


def current_proxy_state() -> dict[str, str]:
    stored = load_stored_proxy_state()
    return {
        key: (stored.get(key) or os.getenv(key) or "").strip()
        for key in FACEBOOK_PROXY_KEYS
    }


def first_configured_proxy(state: dict[str, str]) -> str:
    for key in FACEBOOK_PROXY_KEYS:
        value = (state.get(key) or "").strip()
        if value and not proxy_disabled(value):
            return value
    return ""


def candidate_from_record(record: dict[str, Any], *, mode: str) -> ProxyCandidate | None:
    username = str(record.get("username") or "").strip()
    password = str(record.get("password") or "").strip()
    if mode == "backbone":
        address = (os.getenv("WEBSHARE_BACKBONE_HOST") or "p.webshare.io").strip()
        port = str(os.getenv("WEBSHARE_BACKBONE_PORT") or "80").strip()
    else:
        address = str(
            record.get("proxy_address")
            or record.get("address")
            or record.get("host")
            or ""
        ).strip()
        port = str(record.get("port") or "").strip()

    if not username or not password or not address or not port:
        return None

    country = str(record.get("country_code") or record.get("country") or "").strip()
    city = str(record.get("city_name") or record.get("city") or "").strip()
    label_bits = [address, port]
    if country:
        label_bits.append(country)
    if city:
        label_bits.append(city)
    if mode == "backbone":
        label_bits.append("backbone")
    label = " / ".join(label_bits)
    proxy_url = (
        "http://"
        f"{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{address}:{port}"
    )
    return ProxyCandidate(proxy_url=proxy_url, label=label, raw=record)


def filter_candidate(record: dict[str, Any]) -> bool:
    valid = record.get("valid")
    if valid is False:
        return False

    allowed_countries = {
        country.strip().upper()
        for country in (os.getenv("WEBSHARE_PROXY_COUNTRIES") or "").split(",")
        if country.strip()
    }
    if not allowed_countries:
        return True

    country_code = str(record.get("country_code") or record.get("country") or "").upper()
    return country_code in allowed_countries


async def read_response_body(response: aiohttp.ClientResponse) -> object:
    try:
        return await response.json(content_type=None)
    except Exception:
        return await response.text()


async def fetch_webshare_candidates(api_key: str) -> list[ProxyCandidate]:
    base_url = (
        os.getenv("WEBSHARE_API_BASE_URL") or "https://proxy.webshare.io/api/v2"
    ).rstrip("/")
    mode = webshare_proxy_mode()
    page_size = env_int("WEBSHARE_PROXY_PAGE_SIZE", 25)
    url = f"{base_url}/proxy/list/"
    headers = {"Authorization": f"Token {api_key}"}
    params = {
        "mode": mode,
        "page": "1",
        "page_size": str(page_size),
        "valid": "true",
    }

    timeout = aiohttp.ClientTimeout(total=env_int("WEBSHARE_API_TIMEOUT_SECONDS", 20))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers, params=params) as response:
            body = await read_response_body(response)
            if response.status >= 400:
                raise WebshareProxyError(
                    f"Webshare proxy list error {response.status}: {body}"
                )

    records = body.get("results", body) if isinstance(body, dict) else body
    if not isinstance(records, list):
        raise WebshareProxyError(f"Unexpected Webshare proxy list response: {body}")

    candidates: list[ProxyCandidate] = []
    for record in records:
        if not isinstance(record, dict) or not filter_candidate(record):
            continue
        candidate = candidate_from_record(record, mode=mode)
        if candidate:
            candidates.append(candidate)

    return candidates


async def test_proxy(proxy_url: str, *, label: str = "proxy") -> tuple[bool, str]:
    if proxy_disabled(proxy_url):
        return False, "empty proxy"

    test_url = (
        os.getenv("WEBSHARE_PROXY_TEST_URL")
        or os.getenv("FB_PROXY_TEST_URL")
        or "https://www.facebook.com/"
    )
    timeout_seconds = env_int("WEBSHARE_PROXY_TEST_TIMEOUT_SECONDS", 20)
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    headers = {
        "User-Agent": os.getenv("FB_USER_AGENT")
        or os.getenv("FB_LOGIN_USER_AGENT")
        or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0 Safari/537.36"
        )
    }

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(
                test_url,
                proxy=proxy_url,
                allow_redirects=False,
            ) as response:
                await response.read()
                if response.status == 407:
                    return False, "407 Proxy Authentication Required"
                if response.status >= 500:
                    return False, f"HTTP {response.status}"
                return True, f"HTTP {response.status}"
    except asyncio.TimeoutError:
        return False, f"timeout after {timeout_seconds}s"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def proxy_state_for(proxy_url: str, current: dict[str, str] | None = None) -> dict[str, str]:
    current = current or {}
    state = {key: proxy_url for key in FACEBOOK_PROXY_KEYS}
    for key in FACEBOOK_PROXY_KEYS:
        if proxy_explicitly_direct(current.get(key)):
            state[key] = "direct"
    return state


async def select_working_candidate(
    candidates: list[ProxyCandidate],
    *,
    current_proxy: str,
) -> tuple[ProxyCandidate | None, str, list[str]]:
    current_proxy = (current_proxy or "").strip()
    fresh_candidates = [
        candidate
        for candidate in candidates
        if candidate.proxy_url != current_proxy
    ]
    failures: list[str] = []
    concurrency = max(1, min(env_int("WEBSHARE_PROXY_TEST_CONCURRENCY", 5), 20))

    async def test_candidate(
        candidate: ProxyCandidate,
    ) -> tuple[ProxyCandidate, bool, str]:
        ok, reason = await test_proxy(candidate.proxy_url, label=candidate.label)
        return candidate, ok, reason

    for index in range(0, len(fresh_candidates), concurrency):
        batch = fresh_candidates[index : index + concurrency]
        tasks = [asyncio.create_task(test_candidate(candidate)) for candidate in batch]
        try:
            for task in asyncio.as_completed(tasks):
                candidate, ok, reason = await task
                if ok:
                    for pending in tasks:
                        if not pending.done():
                            pending.cancel()
                    return candidate, reason, failures
                failures.append(f"{candidate.label}: {reason}")
        finally:
            pending_tasks = [task for task in tasks if not task.done()]
            for task in pending_tasks:
                task.cancel()
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)

    return None, "", failures


async def ensure_webshare_proxy_state() -> dict[str, str]:
    policy = network_policy()
    if policy == "direct":
        print("Facebook network policy: direct.", flush=True)
        return current_proxy_state()

    api_key = webshare_api_key()
    current = current_proxy_state()
    current_proxy = first_configured_proxy(current)
    mode = webshare_proxy_mode()

    if policy == "auto" and not api_key:
        if current_proxy:
            print(
                "Facebook network policy: auto; using configured Facebook proxy.",
                flush=True,
            )
        else:
            print(
                "Facebook network policy: auto; no Webshare API key or proxy configured.",
                flush=True,
            )
        return current

    if policy == "webshare" and not api_key:
        raise WebshareProxyError(
            "FB_NETWORK_POLICY=webshare requires WEBSHARE_API_KEY. "
            "Create the first key in the Webshare dashboard, then store it as "
            "WEBSHARE_API_KEY."
        )

    if current_proxy:
        if not proxy_matches_mode(current_proxy, mode):
            print(
                "Webshare proxy manager ignored current Facebook proxy because "
                f"WEBSHARE_PROXY_MODE={mode}.",
                flush=True,
            )
            current_proxy = ""
        else:
            ok, reason = await test_proxy(current_proxy, label="stored Facebook proxy")
            if ok:
                proxy_state = proxy_state_for(current_proxy, current)
                if policy == "webshare":
                    proxy_state = {key: current_proxy for key in FACEBOOK_PROXY_KEYS}
                    for key in FACEBOOK_PROXY_KEYS:
                        if proxy_explicitly_direct(os.getenv(key)):
                            proxy_state[key] = "direct"
                sync_message = ""
                if proxy_state != current:
                    sync_message = persist_facebook_proxy_state(proxy_state)
                print(
                    "Webshare proxy manager kept current Facebook proxy: "
                    f"{redacted_url(current_proxy)} ({reason})",
                    flush=True,
                )
                if sync_message:
                    print(sync_message, flush=True)
                return proxy_state
            print(
                "Webshare proxy manager rejected current Facebook proxy: "
                f"{redacted_url(current_proxy)} ({reason})",
                flush=True,
            )

    candidates = await fetch_webshare_candidates(api_key)
    if not candidates:
        if current_proxy and keep_last_proxy_on_rotation_failure():
            print(
                "Webshare returned no valid proxy candidates; keeping last configured "
                f"Facebook proxy: {redacted_url(current_proxy)}",
                flush=True,
            )
            return current
        raise WebshareProxyError("Webshare returned no valid proxy candidates.")

    candidate, reason, failures = await select_working_candidate(
        candidates,
        current_proxy=current_proxy,
    )
    if candidate:
        proxy_state = proxy_state_for(candidate.proxy_url, current)
        sync_message = persist_facebook_proxy_state(proxy_state)
        print(
            "Webshare proxy manager selected Facebook proxy: "
            f"{redacted_url(candidate.proxy_url)} ({candidate.label}, {reason})",
            flush=True,
        )
        print(sync_message, flush=True)
        return proxy_state

    sample = "; ".join(failures[:5]) or "all candidates failed"
    if current_proxy and keep_last_proxy_on_rotation_failure():
        print(
            "No replacement Webshare proxy passed the connectivity test; keeping last "
            f"configured Facebook proxy: {redacted_url(current_proxy)}. {sample}",
            flush=True,
        )
        return current
    raise WebshareProxyError(
        f"No Webshare proxy passed the Facebook connectivity test. {sample}"
    )
