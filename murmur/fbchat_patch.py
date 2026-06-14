from __future__ import annotations

import re
import os
import time
import asyncio
import json
import socket
from typing import Any
from urllib.parse import urlparse


_PATCHED = False


def _jazoest(token: str) -> str:
    return "2" + str(sum(ord(char) for char in token))


def _first_non_empty(pattern: str, html: str) -> str | None:
    for value in re.findall(pattern, html):
        if isinstance(value, tuple):
            value = next((part for part in value if part), "")
        value = str(value).strip()
        if value:
            return value
    return None


def _extract_tokens_from_html(html: str) -> tuple[Any, ...]:
    from fbchat_muqit.exception.errors import ValidationError
    from fbchat_muqit.utils import stateHelper

    fb_dtsg = _first_non_empty(r'"DTSGInitialData".*?"token":"(.*?)"', html)
    if not fb_dtsg:
        fb_dtsg = _first_non_empty(
            r'"DTSGInitData"(?:\s*,\s*\[\])?(?:\s*,\s*)\{[^}]*'
            r'"token"\s*:\s*"([^"]*)"',
            html,
        )
    if not fb_dtsg:
        fb_dtsg = _first_non_empty(r'name="fb_dtsg"\s+value="([^"]*)"', html)
    if not fb_dtsg:
        raise ValidationError("Non-empty 'fb_dtsg' token not found.")

    async_pattern = (
        r'"DTSGInitData"(?:\s*,\s*\[\])?(?:\s*,\s*)\{[^}]*'
        r'"async_get_token"\s*:\s*"([^"]*)"[^}]*\}'
    )
    fb_dtsg_ag = _first_non_empty(async_pattern, html)
    if not fb_dtsg_ag:
        fb_dtsg_ag = fb_dtsg
        stateHelper.logger.warning(
            "fbchat-muqit token fallback: async_get_token missing or empty; reusing fb_dtsg."
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
    if not mqtt_client_id:
        from fbchat_muqit.muqit import generate_uuid

        mqtt_client_id = generate_uuid()

    mqtt_app_id = None
    mqtt_app_match = re.search(r'\["MqttWebConfig".*?"appID"\s*:\s*(\d+)', html)
    if mqtt_app_match:
        mqtt_app_id = mqtt_app_match.group(1)
    if not mqtt_app_id:
        mqtt_app_id = os.getenv("FBCHAT_MQTT_APP_ID") or "219994525426954"

    user_app_id = None
    user_app_match = re.search(r'\["CurrentUserInitialData".*?"APP_ID"\s*:\s*"(\d+)"', html)
    if user_app_match:
        user_app_id = user_app_match.group(1)

    mqtt_endpoint = re.search(
        r'\["MqttWebConfig".*?"endpoint"\s*:\s*"([^"]+)"',
        html,
    )
    if not mqtt_endpoint:
        mqtt_endpoint = re.search(
            r'"endpoint"\s*:\s*"([^"]*?edge-chat[^"]*)"',
            html,
        )
    if mqtt_endpoint:
        endpoint = mqtt_endpoint.group(1).encode().decode("unicode_escape")
        region_match = re.search(r"[?&]region=([a-zA-Z0-9_-]+)", endpoint)
        region = region_match.group(1) if region_match else ""
    else:
        endpoint = "wss://edge-chat.facebook.com/chat"
        region = os.getenv("FBCHAT_MQTT_REGION") or ""
        stateHelper.logger.warning(
            "fbchat-muqit MQTT endpoint fallback: using edge-chat.facebook.com."
        )

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
        "https://www.facebook.com/",
        "https://www.facebook.com/messages/t/",
        "https://www.messenger.com/",
        "https://www.messenger.com/t/",
    ]


def _bootstrap_timeout_seconds() -> int:
    raw = os.getenv("FBCHAT_BOOTSTRAP_TIMEOUT_SECONDS") or os.getenv("FB_HTTP_TIMEOUT_SECONDS") or "25"
    try:
        return max(5, min(int(raw), 180))
    except ValueError:
        return 25


def _bootstrap_retries() -> int:
    raw = os.getenv("FBCHAT_BOOTSTRAP_RETRIES") or "3"
    try:
        return max(1, min(int(raw), 10))
    except ValueError:
        return 3


def _bootstrap_retry_delay_seconds() -> float:
    raw = os.getenv("FBCHAT_BOOTSTRAP_RETRY_DELAY_SECONDS") or "2"
    try:
        return max(0.0, min(float(raw), 30.0))
    except ValueError:
        return 2.0


def _html_markers(html: str) -> str:
    # careful: "checkpoint" and "/login" appear in JS route tables on every page
    # only flag if it's an actual checkpoint page we're ON, not route definitions
    is_checkpoint = (
        bool(re.search(r'<title>[^<]*checkpoint[^<]*</title>', html, re.I))
        or bool(re.search(r'<form[^>]*checkpoint', html, re.I))
        or 'checkpoint_submit' in html
    )
    is_login = (
        bool(re.search(r'<title>[^<]*login[^<]*</title>', html, re.I))
        or 'id="login_form"' in html
        or 'action="/login"' in html
    )
    markers = {
        "dtsg": '"DTSGInitialData"' in html,
        "async": "async_get_token" in html,
        "mqtt": "MqttWebConfig" in html or "edge-chat" in html,
        "region": "region=" in html,
        "checkpoint": is_checkpoint,
        "login": is_login,
    }
    return ", ".join(f"{key}={value}" for key, value in markers.items())


def _configure_mqtt_options(self: Any) -> None:
    from fbchat_muqit.muqit import generate_session_id, get_cookie_header

    session_id = generate_session_id()
    region = self._region
    mqtt_client_id = self._mqttClientID
    mqtt_app_id = self._mqttAppID

    topics = [
        "/legacy_web",
        "/ls_req",
        "/ls_resp",
        "/t_ms",
        "/rtc_multi",
        "/thread_typing",
        "/orca_typing_notifications",
        "/orca_presence",
        "/br_sr",
        "/friend_request",
        "/friending_state_change",
        "/friend_requests_seen",
        "/sr_res",
        "/webrtc",
        "/onevc",
        "/notify_disconnect",
        "/mercury",
        "/inbox",
        "/messaging_events",
        "/orca_message_notifications",
        "/pp",
        "/webrtc_response",
    ]
    username = {
        "u": self._state.user_id,
        "s": session_id,
        "chat_on": self._chat_on,
        "fg": self._foreground,
        "d": mqtt_client_id,
        "aid": mqtt_app_id,
        "st": topics,
        "pm": [],
        "cp": 3,
        "ecp": 10,
        "ct": "websocket",
        "mqtt_sid": "",
        "dc": "",
        "no_auto_fg": True,
        "gas": None,
        "pack": [],
        "p": None,
        "aids": None,
        "a": self._state._userAgent,
    }
    self._mqttClient._client.username_pw_set(json.dumps(username))

    headers = {
        "Cookie": get_cookie_header(
            self._state._session, "https://edge-chat.facebook.com/chat"
        ),
        "User-Agent": self._state._userAgent,
        "Origin": "https://www.facebook.com",
        "Host": self.HOST,
    }

    query = f"sid={session_id}&cid={mqtt_client_id}"
    if region:
        query = f"region={region}&{query}"
    self._mqttClient._client.ws_set_options(
        path=f"/chat?{query}",
        headers=headers,
    )


def _facebook_http_timeout_seconds() -> int:
    raw = os.getenv("FB_HTTP_TIMEOUT_SECONDS") or os.getenv("FBCHAT_BOOTSTRAP_TIMEOUT_SECONDS") or "120"
    try:
        return max(30, min(int(raw), 300))
    except ValueError:
        return 120


def _configure_http_session_timeout() -> None:
    import aiohttp
    from fbchat_muqit import state as fb_state
    from fbchat_muqit.utils import stateHelper as fb_state_helper

    timeout_seconds = _facebook_http_timeout_seconds()
    timeout = aiohttp.ClientTimeout(
        total=timeout_seconds,
        connect=timeout_seconds,
        sock_connect=timeout_seconds,
        sock_read=timeout_seconds,
    )

    def get_session(cookie_jar=None, proxy=None):
        proxy_arg = None
        if proxy:
            scheme = urlparse(proxy).scheme.lower()
            if scheme in {"http", "https"}:
                proxy_arg = proxy
                connector = aiohttp.TCPConnector(
                    family=socket.AF_INET,
                    ttl_dns_cache=300,
                    enable_cleanup_closed=True,
                )
            else:
                from aiohttp_socks import ProxyConnector

                connector = ProxyConnector.from_url(proxy)
        else:
            connector = aiohttp.TCPConnector(
                family=socket.AF_INET,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
            )

        session = aiohttp.ClientSession(
            cookie_jar=cookie_jar,
            connector=connector,
            timeout=timeout,
        )
        if proxy_arg:
            original_request = session._request

            async def proxied_request(method, url, **kwargs):
                kwargs.setdefault("proxy", proxy_arg)
                return await original_request(method, url, **kwargs)

            session._request = proxied_request
        return session

    fb_state.get_session = get_session
    fb_state_helper.get_session = get_session


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
    urls = _bootstrap_urls()
    logger.info(f"fbchat-patch: bootstrap starting with {len(urls)} URL(s): {urls}")
    try:
        user_id = get_user_id(session)
        logger.debug(f"Extracted user ID: {user_id}")

        last_error: Exception | None = None
        checkpoint_error: Exception | None = None
        for attempt in range(1, _bootstrap_retries() + 1):
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

                    # Reject checkpoint/login pages even when token extraction
                    # succeeds — checkpoint pages still carry fb_dtsg.
                    if "checkpoint=True" in _html_markers(html_content) or "login=True" in _html_markers(html_content):
                        logger.warning(
                            "fbchat-muqit bootstrap extracted tokens but "
                            f"page shows checkpoint/login; markers={_html_markers(html_content)}"
                        )
                        continue

                    out = cls(
                        user_id=user_id,
                        user_name=user_name,
                        _host=host,
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
                        markers = _html_markers(html_text)
                        if "checkpoint=True" in markers or "login=True" in markers:
                            checkpoint_error = AuthenticationError(
                                "Facebook returned checkpoint/login page; "
                                "cookies are not fully authenticated."
                            )
                        logger.warning(
                            "fbchat-muqit bootstrap failed for "
                            f"{url} (attempt {attempt}/{_bootstrap_retries()}): "
                            f"{exc}; html_len={len(html_text)}; markers={markers}"
                        )
                    else:
                        logger.warning(
                            "fbchat-muqit bootstrap failed for "
                            f"{url} (attempt {attempt}/{_bootstrap_retries()}): {exc}"
                        )
            if attempt < _bootstrap_retries():
                await asyncio.sleep(_bootstrap_retry_delay_seconds())

        raise (
            checkpoint_error
            or last_error
            or AuthenticationError("Failed to extract session data")
        )
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
    from fbchat_muqit import muqit
    from fbchat_muqit.utils import stateHelper

    stateHelper.extract_tokens_from_html = _extract_tokens_from_html
    state.extract_tokens_from_html = _extract_tokens_from_html
    state.State.login = classmethod(_login_with_bootstrap_fallback)
    muqit.Mqtt._configure_mqtt_options = _configure_mqtt_options
    _configure_http_session_timeout()
    _PATCHED = True

    logger = stateHelper.get_logger() if hasattr(stateHelper, "get_logger") else None
    if logger:
        logger.info("fbchat-patch: applied bootstrap, mqtt, and timeout patches.")
    else:
        print("fbchat-patch: applied bootstrap, mqtt, and timeout patches.", flush=True)
