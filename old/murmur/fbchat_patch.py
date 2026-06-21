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
    from fbchat_muqit import state as fb_state
    from fbchat_muqit.utils import stateHelper as fb_state_helper

    timeout_seconds = _facebook_http_timeout_seconds()

    from aiohttp import ClientTimeout
    timeout = ClientTimeout(
        total=timeout_seconds,
        connect=timeout_seconds,
        sock_connect=timeout_seconds,
        sock_read=timeout_seconds,
    )

    def get_session(cookie_jar=None, proxy=None):
        return _make_curl_session(
            cookie_jar=cookie_jar,
            proxy=proxy,
            timeout=timeout,
        )

    fb_state.get_session = get_session
    fb_state_helper.get_session = get_session


class _CurlRequestContextManager:
    __slots__ = ("_session", "_method", "_url", "_kwargs", "_response")

    def __init__(self, session, method, url, **kwargs):
        self._session = session
        self._method = method
        self._url = url
        self._kwargs = kwargs
        self._response = None

    async def __aenter__(self):
        self._response = await self._session._do_request(self._method, self._url, **self._kwargs)
        return self._response

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class _CurlCompatResponse:
    __slots__ = ("_resp", "status", "headers", "ok", "url", "cookies")

    def __init__(self, response):
        self._resp = response
        self.status = response.status_code
        self.headers = response.headers
        self.ok = response.ok
        self.url = response.url
        self.cookies = response.cookies

    async def text(self, encoding=None):
        t = self._resp.text
        if encoding and encoding.lower() not in ("utf-8", "utf8"):
            return t.encode("utf-8").decode(encoding)
        return t

    async def json(self, content_type=None):
        return self._resp.json()

    async def read(self):
        return self._resp.content

    def raise_for_status(self):
        if not self.ok:
            from aiohttp import ClientResponseError
            raise ClientResponseError(
                self.url, self.status, headers=self.headers,
            )


class _CurlCompatSession:
    def __init__(self, cookie_jar=None, connector=None, timeout=None, headers=None, proxy=None):
        from curl_cffi.requests import AsyncSession as CurlSession

        self.closed = False
        self._cookie_jar = cookie_jar
        self._timeout = timeout

        curl_kwargs = {"impersonate": "chrome"}
        if timeout and hasattr(timeout, "total"):
            curl_kwargs["timeout"] = timeout.total

        self._proxy = proxy
        if not self._proxy and connector:
            proxy_url = getattr(connector, "_proxy_url", None)
            if proxy_url is not None:
                self._proxy = str(proxy_url)

        self._session = CurlSession(**curl_kwargs)

        if cookie_jar:
            from yarl import URL as _URL
            for domain in (
                "https://www.facebook.com",
                "https://www.messenger.com",
                "https://m.facebook.com",
            ):
                for name, morsel in cookie_jar.filter_cookies(_URL(domain)).items():
                    self._session.cookies.set(name, morsel.value, domain=domain)

    @property
    def cookie_jar(self):
        return self._cookie_jar

    @cookie_jar.setter
    def cookie_jar(self, value):
        self._cookie_jar = value

    def get(self, url, **kwargs):
        return _CurlRequestContextManager(self, "GET", url, **kwargs)

    def post(self, url, **kwargs):
        return _CurlRequestContextManager(self, "POST", url, **kwargs)

    def options(self, url, **kwargs):
        return _CurlRequestContextManager(self, "OPTIONS", url, **kwargs)

    async def ws_connect(self, url, *, headers=None, heartbeat=None, autoping=True, **kwargs):
        import aiohttp
        jar = self._cookie_jar
        if jar is None:
            jar = aiohttp.CookieJar()
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(cookie_jar=jar, connector=connector) as sess:
            return await sess.ws_connect(url, headers=headers, heartbeat=heartbeat, autoping=autoping, **kwargs)

    def _sync_jar_to_session(self):
        if not self._cookie_jar:
            return
        from yarl import URL as _URL
        for domain in (
            "https://www.facebook.com",
            "https://www.messenger.com",
            "https://m.facebook.com",
        ):
            for name, morsel in self._cookie_jar.filter_cookies(_URL(domain)).items():
                self._session.cookies.set(name, morsel.value, domain=f".{_URL(domain).host}")

    async def _do_request(self, method, url, **kwargs):
        proxy = kwargs.pop("proxy", None) or self._proxy
        allow_redirects = kwargs.pop("allow_redirects", True)
        data = kwargs.pop("data", None)
        params = kwargs.pop("params", None)
        headers = kwargs.pop("headers", None)

        self._sync_jar_to_session()

        curl_kwargs = {}
        if headers:
            curl_kwargs["headers"] = headers
        if params:
            curl_kwargs["params"] = params
        curl_kwargs["allow_redirects"] = allow_redirects
        if proxy:
            curl_kwargs["proxies"] = {"http": proxy, "https": proxy}

        if data is not None:
            form_data, files_data = self._convert_and_split_data(data)
            if form_data is not None:
                curl_kwargs["data"] = form_data
            if files_data is not None:
                curl_kwargs["files"] = files_data

        resp = await self._session.request(method, url, **curl_kwargs)
        self._sync_cookies(resp, url)
        return _CurlCompatResponse(resp)

    def _convert_and_split_data(self, data):
        if isinstance(data, bytes):
            return data, None
        if isinstance(data, str):
            return data, None
        if isinstance(data, dict):
            files = {k: v for k, v in data.items() if isinstance(v, tuple) and len(v) >= 2}
            form = {k: v for k, v in data.items() if k not in files}
            return form or None, files or None

        if hasattr(data, "_fields"):
            form: dict[str, str] = {}
            files: dict[str, tuple] = {}
            for field in data._fields:
                name = field[0]
                val = field[1]
                if hasattr(val, "value") and hasattr(val, "filename") and val.filename:
                    files[name] = (val.filename, val.value, val.content_type)
                elif hasattr(val, "value"):
                    v = val.value
                    form[name] = v if isinstance(v, str) else str(v)
                else:
                    form[name] = val if isinstance(val, str) else str(val)
            return form or None, files or None

        if hasattr(data, "__iter__"):
            try:
                d = dict(data)
                return d if d else None, None
            except (TypeError, ValueError):
                pass

        return data, None

    def _sync_cookies(self, response, url):
        if not self._cookie_jar or not hasattr(response, "cookies") or not response.cookies:
            return

        from http.cookies import SimpleCookie
        from yarl import URL

        parsed = URL(str(url)) if isinstance(url, str) else url
        host = parsed.host or "www.facebook.com"

        has_domain = False
        for name, cookie in response.cookies.items():
            if not isinstance(cookie, str) and hasattr(cookie, "get"):
                has_domain = True
            break

        for name, cookie in response.cookies.items():
            if isinstance(cookie, str) or not hasattr(cookie, "value"):
                val = str(cookie)
                sc = SimpleCookie()
                sc[name] = val
                sc[name]["domain"] = f".{host}"
                sc[name]["path"] = "/"
                self._cookie_jar.update_cookies(sc, URL(f"https://{host}"))
            else:
                sc = SimpleCookie()
                sc[name] = str(cookie.value)
                domain = cookie.get("domain", "") if hasattr(cookie, "get") else ""
                path = cookie.get("path", "/") if hasattr(cookie, "get") else "/"
                sc[name]["domain"] = domain or f".{host}"
                sc[name]["path"] = path
                if hasattr(cookie, "get") and cookie.get("expires"):
                    sc[name]["expires"] = cookie["expires"]
                self._cookie_jar.update_cookies(sc, URL(f"https://{host}"))

    async def close(self):
        if not self.closed:
            await self._session.close()
            self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


def _make_curl_session(
    cookie_jar=None, proxy=None, timeout=None,
) -> _CurlCompatSession:
    if proxy:
        from urllib.parse import urlparse as parse_url
        scheme = parse_url(proxy).scheme.lower()
        if scheme in ("http", "https"):
            connector = None
        else:
            from aiohttp_socks import ProxyConnector
            connector = ProxyConnector.from_url(proxy)
            return _CurlCompatSession(
                cookie_jar=cookie_jar,
                connector=connector,
                timeout=timeout,
            )
    else:
        connector = None

    return _CurlCompatSession(
        cookie_jar=cookie_jar,
        connector=connector,
        timeout=timeout,
        proxy=proxy,
    )


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

    cookie_dict: dict[str, str] = {}
    if session and hasattr(session, "cookie_jar") and session.cookie_jar:
        for name, morsel in session.cookie_jar.filter_cookies(current_url).items():
            cookie_dict[name] = morsel.value

    proxy = getattr(session, "_murmur_curl_proxy", None) or getattr(session, "_proxy", None)
    timeout = getattr(session, "_murmur_curl_timeout", None)

    from curl_cffi.requests import AsyncSession as CurlSession

    async with CurlSession(impersonate="chrome", timeout=timeout or 120) as curl_session:
        if cookie_dict:
            curl_session.cookies.update(cookie_dict)

        resp = await curl_session.get(
            str(current_url),
            headers=headers,
            allow_redirects=False,
            proxies={"http": proxy, "https": proxy} if proxy else None,
        )

        if 300 <= resp.status_code < 400:
            location = resp.headers.get("Location")
            if location:
                redirected_url = URL(location)
                if not redirected_url.is_absolute():
                    redirected_url = current_url.join(redirected_url)
                host = str(redirected_url.host or host)
                origin = f"{redirected_url.scheme}://{host}"
                headers.update({"Host": host, "Origin": origin, "Referer": str(redirected_url)})
                resp = await curl_session.get(
                    str(redirected_url),
                    headers=headers,
                    proxies={"http": proxy, "https": proxy} if proxy else None,
                )

        if resp.status_code != 200:
            raise NetworkError(
                f"Failed to fetch Facebook bootstrap page {url}: HTTP {resp.status_code}",
                error_code=str(resp.status_code),
            )

        html = resp.text

    if session and hasattr(session, "cookie_jar") and session.cookie_jar:
        from http.cookies import SimpleCookie

        for name, cookie in dict(resp.cookies).items():
            try:
                val = cookie.value
            except AttributeError:
                val = str(cookie)
            sc = SimpleCookie()
            sc[name] = val
            domain = cookie.get("domain", "") if hasattr(cookie, "get") else ""
            path = cookie.get("path", "/") if hasattr(cookie, "get") else "/"
            sc[name]["domain"] = domain or f".{host}"
            sc[name]["path"] = path
            expires = cookie.get("expires") if hasattr(cookie, "get") else None
            if expires:
                sc[name]["expires"] = expires
            session.cookie_jar.update_cookies(sc, URL(f"https://{host}"))

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


def _patch_mqtt_connect() -> None:
    """Wrap Mqtt.connect to skip tls_set() when TLS context is already configured.

    configure_mqtt() in app.py passes tls_context to aiomqtt.Client, which
    calls tls_set_context() on the paho client. Mqtt.connect then calls
    tls_set() again, which raises ValueError because _ssl_context is already
    set. This wrapper detects that case and skips the redundant call.
    """
    from fbchat_muqit.muqit import Mqtt
    import paho.mqtt.client as paho

    _original_connect = Mqtt.connect.__func__

    _original_tls_set = paho.Client.tls_set

    def _patched_tls_set(self, *args, **kwargs):
        if self._ssl_context is not None:
            return
        return _original_tls_set(self, *args, **kwargs)

    paho.Client.tls_set = _patched_tls_set


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
    _patch_mqtt_connect()
    _PATCHED = True

    logger = stateHelper.get_logger() if hasattr(stateHelper, "get_logger") else None
    if logger:
        logger.info("fbchat-patch: applied bootstrap, mqtt, and timeout patches.")
    else:
        print("fbchat-patch: applied bootstrap, mqtt, and timeout patches.", flush=True)
