from __future__ import annotations

import re
import os
import time
import asyncio
from typing import Any


_PATCHED = False


def _jazoest(token: str) -> str:
    return "2" + str(sum(ord(char) for char in token))


def _extract_tokens_from_html(html: str) -> tuple[Any, ...]:
    from fbchat_muqit.exception.errors import ValidationError
    from fbchat_muqit.utils import stateHelper

    fb_dtsg_match = re.search(r'"DTSGInitialData".*?"token":"(.*?)"', html)
    if not fb_dtsg_match:
        raise ValidationError("'fb_dtsg' token not found.")
    fb_dtsg = fb_dtsg_match.group(1)

    async_pattern = (
        r'"DTSGInitData"(?:\s*,\s*\[\])?(?:\s*,\s*)\{[^}]*'
        r'"async_get_token"\s*:\s*"([^"]+)"[^}]*\}'
    )
    async_match = re.search(async_pattern, html)
    if async_match:
        fb_dtsg_ag = async_match.group(1)
    else:
        fb_dtsg_ag = fb_dtsg
        stateHelper.logger.warning(
            "fbchat-muqit token fallback: async_get_token missing; reusing fb_dtsg."
        )

    lsd_token = None
    lsd_match = re.search(
        r'"LSD"\s*,\s*\[\s*\]\s*,\s*\{\s*"token"\s*:\s*"([A-Za-z0-9_-]+)"',
        html,
    )
    if lsd_match:
        lsd_token = lsd_match.group(1)

    client_revision = None
    client_revision_match = re.search(r'client_revision":(\d+)', html)
    if client_revision_match:
        client_revision = int(client_revision_match.group(1))

    mqtt_client_id = None
    mqtt_client_match = re.search(
        r'\["MqttWebDeviceID".*?"clientID"\s*:\s*"([a-f0-9\-]+)"',
        html,
    )
    if mqtt_client_match:
        mqtt_client_id = mqtt_client_match.group(1)

    mqtt_app_id = None
    mqtt_app_match = re.search(r'\["MqttWebConfig".*?"appID"\s*:\s*(\d+)', html)
    if mqtt_app_match:
        mqtt_app_id = mqtt_app_match.group(1)

    user_app_id = None
    user_app_match = re.search(r'\["CurrentUserInitialData".*?"APP_ID"\s*:\s*"(\d+)"', html)
    if user_app_match:
        user_app_id = user_app_match.group(1)

    mqtt_endpoint = re.search(
        r'"endpoint"\s*:\s*"([^"]*?region=([a-zA-Z0-9_-]+)[^"]*)"',
        html,
    )
    if not mqtt_endpoint:
        raise ValueError("Mqtt Endpoint not found!")
    endpoint = mqtt_endpoint.group(1).encode().decode("unicode_escape")
    region = mqtt_endpoint.group(2)

    user_name = None
    user_name_match = re.search(r'"NAME"\s*:\s*"([^"]+)"', html)
    if user_name_match:
        user_name = user_name_match.group(1)
        stateHelper.logger.debug(f"User name: {user_name}")

    jazoest = _jazoest(fb_dtsg)
    jazoest_async = _jazoest(fb_dtsg_ag)

    stateHelper.logger.debug(f"fb dtag: {fb_dtsg}")
    stateHelper.logger.debug(f"fb dtsg async: {fb_dtsg_ag}")
    stateHelper.logger.debug(f"LSD token: {lsd_token}")
    stateHelper.logger.debug(f"jazoest: {jazoest}")
    stateHelper.logger.debug(f"jazoest async: {jazoest_async}")
    stateHelper.logger.debug(f"client revision: {client_revision}")
    stateHelper.logger.debug(f"client id(uuid): {mqtt_client_id}")
    stateHelper.logger.debug(f"mqttAppID: {mqtt_app_id}")
    stateHelper.logger.debug(f"User App_ID: {user_app_id}")
    stateHelper.logger.debug(f"Mqtt Endpoint URL: {endpoint}")
    stateHelper.logger.debug(f"Region: {region}")

    return (
        fb_dtsg,
        fb_dtsg_ag,
        lsd_token,
        jazoest,
        jazoest_async,
        client_revision,
        mqtt_client_id,
        mqtt_app_id,
        user_app_id,
        endpoint,
        region,
        user_name,
    )


def _bootstrap_urls() -> list[str]:
    raw_urls = os.getenv("FBCHAT_BOOTSTRAP_URLS") or os.getenv("FB_BOOTSTRAP_URLS") or ""
    urls = [item.strip() for item in raw_urls.replace("\n", ",").split(",") if item.strip()]
    if urls:
        return urls
    return [
        "https://www.messenger.com/",
        "https://www.messenger.com/t/",
        "https://www.facebook.com/messages/t/",
        "https://www.facebook.com/",
    ]


def _bootstrap_timeout_seconds() -> int:
    raw = os.getenv("FBCHAT_BOOTSTRAP_TIMEOUT_SECONDS") or os.getenv("FB_HTTP_TIMEOUT_SECONDS") or "25"
    try:
        return max(5, min(int(raw), 30))
    except ValueError:
        return 25


def _html_markers(html: str) -> str:
    markers = {
        "dtsg": '"DTSGInitialData"' in html,
        "async": "async_get_token" in html,
        "mqtt": "MqttWebConfig" in html or "edge-chat" in html,
        "region": "region=" in html,
        "checkpoint": "checkpoint" in html.lower(),
        "login": "login_form" in html or "/login" in html.lower(),
    }
    return ", ".join(f"{key}={value}" for key, value in markers.items())


async def _fetch_bootstrap_html(cls: Any, session: Any, url: str, user_agent: str | None) -> tuple[str, str]:
    from yarl import URL
    from fbchat_muqit.exception.errors import NetworkError

    current_url = URL(url)
    host = str(current_url.host or "www.facebook.com")
    origin = f"{current_url.scheme}://{host}"
    headers = cls.ALLHEADERS["get"].copy()
    headers["User-Agent"] = user_agent or headers["User-Agent"]
    headers["Host"] = host
    headers["Origin"] = origin
    headers["Referer"] = str(current_url)

    async with session.get(str(current_url), headers=headers, allow_redirects=False) as response:
        if 300 <= response.status < 400:
            location = response.headers.get("Location")
            if location:
                redirected_url = URL(location)
                if not redirected_url.is_absolute():
                    redirected_url = current_url.join(redirected_url)
                host = str(redirected_url.host or host)
                origin = f"{redirected_url.scheme}://{host}"
                headers.update({"Host": host, "Origin": origin, "Referer": str(redirected_url)})
                response = await session.get(str(redirected_url), headers=headers)

        if response.status != 200:
            raise NetworkError(
                f"Failed to fetch Facebook bootstrap page {url}: HTTP {response.status}",
                error_code=str(response.status),
            )

        html = await response.text()
    return host, html


async def _login_with_bootstrap_fallback(cls: Any, session: Any, jar: Any, user_agent: str | None = None) -> Any:
    from fbchat_muqit.exception.errors import AuthenticationError, FBChatError
    from fbchat_muqit.logging.logger import get_logger
    from fbchat_muqit.utils.stateHelper import get_user_id

    logger = get_logger()
    try:
        user_id = get_user_id(session)
        logger.debug(f"Extracted user ID: {user_id}")

        last_error: Exception | None = None
        for url in _bootstrap_urls():
            html_content = None
            try:
                host, html_content = await asyncio.wait_for(
                    _fetch_bootstrap_html(cls, session, url, user_agent),
                    timeout=_bootstrap_timeout_seconds(),
                )
                logger.debug(
                    f"Received bootstrap HTML from {url}: {len(html_content)} characters"
                )
                (
                    fb_dtsg,
                    fb_dtsg_ag,
                    lsd,
                    jazoest,
                    jazoest_async,
                    client_revision,
                    mqtt_client_id,
                    mqtt_app_id,
                    user_app_id,
                    endpoint,
                    region,
                    user_name,
                ) = _extract_tokens_from_html(html_content)
                out = cls(
                    user_id=user_id,
                    user_name=user_name,
                    _host="www.facebook.com",
                    _fb_dtsg=fb_dtsg,
                    _fb_dtsg_ag=fb_dtsg_ag,
                    _lsd=lsd,
                    _jazoest=jazoest,
                    _jazoest_async=jazoest_async,
                    _revision=client_revision,
                    _mqttClientID=mqtt_client_id,
                    _mqttAppID=mqtt_app_id,
                    _userAppID=user_app_id,
                    _endpoint=endpoint,
                    _region=region,
                    _session=session,
                    _is_logged=True,
                    _jar=jar,
                    _last_refresh=time.time(),
                    _userAgent=user_agent or cls.BASE_HEADERS["User-Agent"],
                )
                logger.debug(f"Extracted tokens from bootstrap page: {url}")
                return out
            except Exception as exc:
                last_error = exc
                html_text = locals().get("html_content")
                if isinstance(html_text, str):
                    logger.warning(
                        "fbchat-muqit bootstrap failed for "
                        f"{url}: {exc}; html_len={len(html_text)}; markers={_html_markers(html_text)}"
                    )
                else:
                    logger.warning(f"fbchat-muqit bootstrap failed for {url}: {exc}")

        raise last_error or AuthenticationError("Failed to extract session data")
    except Exception as exc:
        logger.error(f"Failed to create State from session: {exc}")
        if isinstance(exc, FBChatError):
            raise
        raise AuthenticationError(
            f"Failed to extract session data: {exc}", original_exception=exc
        ) from exc


def apply_fbchat_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from fbchat_muqit import state
    from fbchat_muqit.utils import stateHelper

    stateHelper.extract_tokens_from_html = _extract_tokens_from_html
    state.extract_tokens_from_html = _extract_tokens_from_html
    state.State.login = classmethod(_login_with_bootstrap_fallback)
    _PATCHED = True
