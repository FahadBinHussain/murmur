import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import socket
import tempfile
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque
from urllib.parse import unquote, urljoin, urlparse

import aiohttp
import fbchat_muqit.state as fb_state
import fbchat_muqit.utils.stateHelper as fb_state_helper
from dotenv import load_dotenv
from fbchat_muqit import Client, EventType, Message


DEFAULT_FB_UPLOAD_ENDPOINTS = [
    "https://upload.facebook.com/ajax/mercury/upload.php",
    "https://upload.messenger.com/ajax/mercury/upload.php",
]


class UserVisibleError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    openwebui_base_url: str
    openwebui_api_key: str | None
    openwebui_login_email: str | None
    openwebui_login_password: str | None
    openwebui_model: str
    openwebui_model_aliases: dict[str, str]
    openwebui_warmup: bool
    openwebui_warmup_chat: bool
    image_provider_label: str | None
    image_generation_model: str | None
    image_size: str | None
    image_steps: int | None
    fb_cookies_path: str
    fb_user_agent: str | None
    fb_proxy: str | None
    fb_mqtt_proxy: str | None
    fb_mqtt_watchdog_seconds: int
    fb_http_timeout_seconds: int
    fb_upload_proxy: str | None
    fb_upload_retries: int
    fb_upload_endpoints: list[str]
    bot_prefix: str
    respond_only_on_prefix: bool
    respond_to_bot_replies: bool
    max_history_messages: int
    max_reply_chars: int
    request_timeout_seconds: int
    allowed_thread_ids: set[str]
    system_prompt: str


@dataclass(frozen=True)
class PromptRequest:
    text: str
    is_prefixed: bool


@dataclass(frozen=True)
class BotResponse:
    text: str | None = None
    file_paths: list[str] | None = None
    cleanup_paths: list[str] | None = None


@dataclass(frozen=True)
class ModelOption:
    id: str
    name: str
    provider: str = ""
    is_free: bool = False
    pricing: dict[str, str] | None = None


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def parse_model_aliases(default_model: str) -> dict[str, str]:
    aliases = {"default": default_model}
    raw_aliases = os.getenv("OPENWEBUI_MODEL_ALIASES", "")

    for item in raw_aliases.replace("\n", ",").split(","):
        item = item.strip()
        if not item:
            continue

        if "=" not in item:
            raise ValueError(
                "OPENWEBUI_MODEL_ALIASES must use alias=model pairs, "
                f"got {item!r}"
            )

        alias, model = item.split("=", 1)
        alias = alias.strip().lower().lstrip("@")
        model = model.strip()
        if not alias or not model:
            raise ValueError(
                "OPENWEBUI_MODEL_ALIASES must use non-empty alias=model pairs"
            )
        aliases[alias] = model

    return aliases


def env_int(name: str) -> int | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return int(value)


def env_csv(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return list(default)
    items = [item.strip() for item in value.replace("\n", ",").split(",")]
    return [item for item in items if item]


def env_proxy(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    value = value.strip()
    if "://" not in value:
        value = f"http://{value}"
    return value


def load_settings() -> Settings:
    load_dotenv()

    port = os.getenv("PORT", "8080")
    openwebui_base_url = os.getenv("OPENWEBUI_BASE_URL") or f"http://127.0.0.1:{port}"
    openwebui_model = os.environ["OPENWEBUI_MODEL"]

    allowed_thread_ids = {
        thread_id.strip()
        for thread_id in os.getenv("ALLOWED_THREAD_IDS", "").split(",")
        if thread_id.strip()
    }

    return Settings(
        openwebui_base_url=openwebui_base_url.rstrip("/"),
        openwebui_api_key=os.getenv("OPENWEBUI_API_KEY") or None,
        openwebui_login_email=os.getenv("OPENWEBUI_LOGIN_EMAIL")
        or os.getenv("WEBUI_ADMIN_EMAIL")
        or None,
        openwebui_login_password=os.getenv("OPENWEBUI_LOGIN_PASSWORD")
        or os.getenv("WEBUI_ADMIN_PASSWORD")
        or None,
        openwebui_model=openwebui_model,
        openwebui_model_aliases=parse_model_aliases(openwebui_model),
        openwebui_warmup=env_bool("OPENWEBUI_WARMUP", True),
        openwebui_warmup_chat=env_bool("OPENWEBUI_WARMUP_CHAT", True),
        image_provider_label=os.getenv("IMAGE_PROVIDER_LABEL") or None,
        image_generation_model=os.getenv("IMAGE_GENERATION_MODEL")
        or os.getenv("CLOUDFLARE_IMAGE_MODEL")
        or None,
        image_size=os.getenv("IMAGE_SIZE") or None,
        image_steps=env_int("IMAGE_STEPS"),
        fb_cookies_path=os.getenv("FB_COOKIES_PATH", "cookies.json"),
        fb_user_agent=os.getenv("FB_USER_AGENT") or None,
        fb_proxy=env_proxy("FB_PROXY"),
        fb_mqtt_proxy=env_proxy("FB_MQTT_PROXY"),
        fb_mqtt_watchdog_seconds=int(os.getenv("FB_MQTT_WATCHDOG_SECONDS", "15")),
        fb_http_timeout_seconds=int(os.getenv("FB_HTTP_TIMEOUT_SECONDS", "120")),
        fb_upload_proxy=env_proxy("FB_UPLOAD_PROXY"),
        fb_upload_retries=int(os.getenv("FB_UPLOAD_RETRIES", "3")),
        fb_upload_endpoints=env_csv(
            "FB_UPLOAD_ENDPOINTS", DEFAULT_FB_UPLOAD_ENDPOINTS
        ),
        bot_prefix=os.getenv("BOT_PREFIX", "/ai").strip(),
        respond_only_on_prefix=env_bool("RESPOND_ONLY_ON_PREFIX", True),
        respond_to_bot_replies=env_bool("RESPOND_TO_BOT_REPLIES", True),
        max_history_messages=int(os.getenv("MAX_HISTORY_MESSAGES", "12")),
        max_reply_chars=int(os.getenv("MAX_REPLY_CHARS", "1800")),
        request_timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "120")),
        allowed_thread_ids=allowed_thread_ids,
        system_prompt=os.getenv(
            "SYSTEM_PROMPT",
            "You are a helpful AI assistant replying inside a Messenger group chat. "
            "Be concise unless the user asks for detail.",
        ),
    )


class Murmur:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.history: dict[str, Deque[dict[str, str]]] = defaultdict(
            lambda: deque(maxlen=settings.max_history_messages)
        )
        self.sent_message_ids: dict[str, Deque[str]] = defaultdict(
            lambda: deque(maxlen=200)
        )
        self.thread_models: dict[str, str] = {}
        self.thread_model_aliases: dict[str, str] = {}
        self.thread_model_options: dict[str, list[ModelOption]] = {}
        self.thread_providers: dict[str, str] = {}
        self.thread_provider_options: dict[str, list[str]] = {}
        self.thread_provider_model_options: dict[str, dict[str, list[ModelOption]]] = {}
        self.resolved_model_cache: dict[str, str] = {}
        self.openwebui_token: str | None = None
        self.mqtt_watchdog_task: asyncio.Task | None = None
        self.response_tasks: set[asyncio.Task] = set()
        self.file_upload_lock = asyncio.Lock()
        self.configure_facebook_http_timeout()
        self.configure_mqtt_proxy()
        self.client = Client(
            cookies_file_path=settings.fb_cookies_path,
            userAgent=settings.fb_user_agent,
            proxy=settings.fb_proxy,
        )
        self.client.event(EventType.LISTENING)(self.on_listening)
        self.client.event(EventType.MESSAGE)(self.on_message)

    def run(self) -> None:
        if self.settings.openwebui_warmup:
            try:
                asyncio.run(self.warmup_openwebui())
            except Exception as exc:
                print(f"Open WebUI warmup failed: {exc}")
        self.client.run()

    def configure_facebook_http_timeout(self) -> None:
        timeout_seconds = max(30, self.settings.fb_http_timeout_seconds)
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

    async def warmup_openwebui(self) -> None:
        print("Warming Open WebUI before Messenger listener starts...")
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout_seconds)
        ) as session:
            headers = await self.openwebui_headers(session)

            for path in ("/api/models", "/api/v1/models"):
                try:
                    async with session.get(
                        f"{self.settings.openwebui_base_url}{path}",
                        headers=headers,
                    ) as response:
                        if response.status < 400:
                            await response.json(content_type=None)
                            print(f"Open WebUI model endpoint warmed via {path}.")
                            break
                        body = await response.text()
                        print(f"Open WebUI model warmup {path} returned {response.status}: {body[:200]}")
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    print(f"Open WebUI model warmup {path} failed: {exc}")

            if self.settings.openwebui_warmup_chat:
                model = await self.resolve_chat_model(
                    "__warmup__",
                    self.settings.openwebui_model,
                )
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply with OK."}],
                    "stream": False,
                    "max_tokens": 2,
                }
                body, status = await self.post_chat_completion(session, payload)
                if status >= 400:
                    print(f"Open WebUI chat warmup returned {status}: {body}")
                else:
                    print("Open WebUI chat completion warmed.")

    def configure_mqtt_proxy(self) -> None:
        if not self.settings.fb_mqtt_proxy:
            return

        try:
            import socks
            import fbchat_muqit.muqit as muqit
        except ImportError as exc:
            print(f"MQTT proxy requested but proxy support is unavailable: {exc}")
            return

        proxy_args = self.mqtt_proxy_args(self.settings.fb_mqtt_proxy, socks)
        if proxy_args is None:
            print("MQTT proxy requested but FB_MQTT_PROXY/FB_PROXY is invalid.")
            return

        original_client = muqit.aiomqtt.Client
        if getattr(original_client, "_murmur_proxy_wrapped", False):
            return

        def proxied_client(*args, **kwargs):
            client = original_client(*args, **kwargs)
            client._client.proxy_set(**proxy_args)
            return client

        proxied_client._murmur_proxy_wrapped = True
        muqit.aiomqtt.Client = proxied_client
        print("Configured Messenger MQTT proxy.")

    def mqtt_proxy_args(self, proxy_url: str, socks_module) -> dict | None:
        parsed = urlparse(proxy_url)
        scheme = parsed.scheme.lower()
        proxy_types = {
            "http": socks_module.HTTP,
            "https": socks_module.HTTP,
            "socks4": socks_module.SOCKS4,
            "socks4a": socks_module.SOCKS4,
            "socks5": socks_module.SOCKS5,
            "socks5h": socks_module.SOCKS5,
        }
        proxy_type = proxy_types.get(scheme)
        if proxy_type is None or not parsed.hostname:
            return None

        args = {
            "proxy_type": proxy_type,
            "proxy_addr": parsed.hostname,
            "proxy_port": parsed.port,
            "proxy_rdns": scheme in {"socks4a", "socks5h"},
        }
        if parsed.username:
            args["proxy_username"] = unquote(parsed.username)
        if parsed.password:
            args["proxy_password"] = unquote(parsed.password)
        return {key: value for key, value in args.items() if value is not None}

    async def on_listening(self) -> None:
        print(f"Murmur online as {self.client.name} ({self.client.uid})")
        if self.mqtt_watchdog_task is None or self.mqtt_watchdog_task.done():
            self.mqtt_watchdog_task = asyncio.create_task(self.watch_mqtt_listener())

    async def watch_mqtt_listener(self) -> None:
        await asyncio.sleep(self.settings.fb_mqtt_watchdog_seconds)
        while True:
            await asyncio.sleep(self.settings.fb_mqtt_watchdog_seconds)
            mqtt = getattr(self.client, "_mqtt", None)
            listen_task = getattr(mqtt, "_listen_task", None) if mqtt else None
            if listen_task is None or not listen_task.done():
                continue

            error = None
            try:
                error = listen_task.exception()
            except asyncio.CancelledError:
                error = "cancelled"
            except Exception as exc:
                error = exc

            print(
                "Messenger MQTT listener stopped; restarting Murmur. "
                f"Last listener error: {error}"
            )
            os._exit(12)

    async def on_message(self, message: Message) -> None:
        if message.sender_id == self.client.uid:
            return

        if not self.is_allowed_thread(message.thread_id):
            return

        request = self.get_request(message)
        if not request:
            return

        task = asyncio.create_task(self.answer_message(message, request))
        self.response_tasks.add(task)
        task.add_done_callback(self.response_tasks.discard)
        task.add_done_callback(self.log_response_task_error)

    def log_response_task_error(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return

        try:
            task.result()
        except Exception as exc:
            print(f"Unhandled Murmur response task failed: {exc}")

    async def answer_message(self, message: Message, request: PromptRequest) -> None:
        bot_response = None
        control_response = None
        prompt = request.text
        model = self.current_model(message.thread_id)

        try:
            await self.client.typing(message.thread_id, True, message.thread_type)

            if request.is_prefixed:
                bot_response = await self.handle_media_command(message, prompt)

                if bot_response is None:
                    control_response = await self.handle_control_command(
                        message.thread_id,
                        prompt,
                    )

                if control_response is None and bot_response is None:
                    one_shot_model = self.extract_one_shot_model(
                        message.thread_id,
                        prompt,
                    )
                    if one_shot_model is not None:
                        prompt, model = one_shot_model

            if bot_response is not None:
                pass
            elif control_response is not None:
                answer = control_response
                bot_response = BotResponse(text=answer)
            else:
                answer = await self.ask_openwebui(message.thread_id, prompt, model)
                bot_response = BotResponse(text=answer)
        except Exception as exc:
            print(f"Failed to answer message {message.id}: {exc}")
            bot_response = BotResponse(
                text=self.user_facing_error(exc)
            )
        finally:
            try:
                await self.client.typing(message.thread_id, False, message.thread_type)
            except Exception:
                pass

        try:
            await self.send_bot_response(message, bot_response)
        except Exception as exc:
            print(f"Failed to send response for message {message.id}: {exc}")

    async def send_bot_response(self, message: Message, response: BotResponse) -> None:
        if not response.text and not response.file_paths:
            return

        parts = self.split_reply(response.text) if response.text else []
        try:
            if response.file_paths:
                sent_message_id = await self.send_message_with_files(
                    text=parts[0] if parts else None,
                    thread_id=message.thread_id,
                    reply_to_message=message.id,
                    file_paths=response.file_paths,
                )
                if sent_message_id:
                    self.sent_message_ids[message.thread_id].append(sent_message_id)
                await asyncio.sleep(0.5)
                parts = parts[1:]

            for index, part in enumerate(parts):
                sent_message_id = await self.client.send_message(
                    text=part,
                    thread_id=message.thread_id,
                    reply_to_message=(
                        message.id
                        if index == 0 and not response.file_paths
                        else None
                    ),
                )
                if sent_message_id:
                    self.sent_message_ids[message.thread_id].append(sent_message_id)
                await asyncio.sleep(0.5)
        finally:
            for path in response.cleanup_paths or []:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass

    async def send_message_with_files(
        self,
        text: str | None,
        thread_id: str,
        reply_to_message: str | None,
        file_paths: list[str],
    ) -> str | None:
        file_ids = await self.upload_files_with_retries(file_paths)
        return await self.client.send_message(
            text=text,
            thread_id=thread_id,
            files_ids=file_ids,
            reply_to_message=reply_to_message,
        )

    async def upload_files_with_retries(self, file_paths: list[str]) -> list[str]:
        attempts = max(1, self.settings.fb_upload_retries)
        last_error: Exception | None = None

        async with self.file_upload_lock:
            for attempt in range(1, attempts + 1):
                for endpoint in self.settings.fb_upload_endpoints:
                    try:
                        print(
                            "Messenger file upload attempt "
                            f"{attempt}/{attempts} via {urlparse(endpoint).netloc}"
                        )
                        return await self.upload_files_to_endpoint(
                            file_paths, endpoint
                        )
                    except Exception as exc:
                        if last_error is None or not self.is_upload_auth_error(exc):
                            last_error = exc
                        print(
                            "Messenger file upload failed via "
                            f"{urlparse(endpoint).netloc} "
                            f"({attempt}/{attempts}): {exc}"
                        )

                if attempt >= attempts:
                    break

                delay = min(2 * attempt, 10)
                print(
                    "Messenger file upload endpoints failed; "
                    f"retrying in {delay}s ({attempt}/{attempts})"
                )
                await asyncio.sleep(delay)

        if last_error is not None:
            raise last_error
        raise RuntimeError("Messenger file upload failed without an exception")

    async def upload_files_to_endpoint(
        self, file_paths: list[str], endpoint: str
    ) -> list[str]:
        state = self.client._state
        if state is None:
            raise RuntimeError("Messenger state is not initialized")

        async with state.get_files_from_paths(file_paths) as files:
            file_dict = {
                f"upload_{index}": file_data
                for index, file_data in enumerate(files)
            }
            if self.settings.fb_upload_proxy:
                json_response = await self.post_upload_with_proxy(
                    endpoint,
                    data={"voice_clip": "false"},
                    files=file_dict,
                )
            else:
                json_response = await state._post(
                    endpoint,
                    data={"voice_clip": "false"},
                    files=file_dict,
                )

        payload = json_response["payload"]
        metadata = payload["metadata"]
        entries = list(metadata.values()) if isinstance(metadata, dict) else metadata

        if len(entries) != len(file_paths):
            raise RuntimeError(
                "Some files could not be uploaded to Messenger "
                f"via {urlparse(endpoint).netloc}"
            )

        ids: list[str] = []
        for entry in entries:
            for key in ("image_id", "gif_id", "video_id", "audio_id", "file_id"):
                if key in entry:
                    ids.append(entry[key])
                    break
            else:
                raise RuntimeError(
                    "Messenger upload response did not include a file id"
                )

        return ids

    async def post_upload_with_proxy(
        self,
        endpoint: str,
        data: dict[str, str],
        files: dict[str, tuple[str, object, str]],
    ) -> dict:
        state = self.client._state
        if state is None:
            raise RuntimeError("Messenger state is not initialized")

        form_data = aiohttp.FormData()
        upload_data = dict(data)
        upload_data.update(state.get_params())
        for key, value in upload_data.items():
            form_data.add_field(key, str(value))
        for key, (filename, file_obj, content_type) in files.items():
            form_data.add_field(
                key,
                file_obj,
                filename=filename,
                content_type=content_type,
            )

        headers = state.build_headers(endpoint, "upload")
        proxy_url = self.settings.fb_upload_proxy
        timeout = aiohttp.ClientTimeout(total=self.settings.fb_http_timeout_seconds)
        connector = None
        proxy_arg = None

        if proxy_url:
            scheme = urlparse(proxy_url).scheme.lower()
            if scheme in {"http", "https"}:
                proxy_arg = proxy_url
                connector = aiohttp.TCPConnector(
                    family=socket.AF_INET,
                    ttl_dns_cache=300,
                    enable_cleanup_closed=True,
                )
            else:
                from aiohttp_socks import ProxyConnector

                connector = ProxyConnector.from_url(proxy_url)

        async with aiohttp.ClientSession(
            cookie_jar=state._jar,
            connector=connector,
            timeout=timeout,
        ) as session:
            async with session.post(
                endpoint,
                data=form_data,
                headers=headers,
                proxy=proxy_arg,
            ) as response:
                content = await state._check_request(response)

        result = state._graphql.process_normal_response(content)
        state._graphql.handle_payload_error(result)
        return result

    @staticmethod
    def is_upload_auth_error(exc: Exception) -> bool:
        return "1357001" in str(exc) or "Not logged in" in str(exc)

    def is_allowed_thread(self, thread_id: str) -> bool:
        return (
            not self.settings.allowed_thread_ids
            or thread_id in self.settings.allowed_thread_ids
        )

    def get_request(self, message: Message) -> PromptRequest | None:
        text = (message.text or "").strip()
        if not text:
            return None

        prefix = self.settings.bot_prefix
        if prefix and text.lower().startswith(prefix.lower()):
            prompt = text[len(prefix) :].strip()
            return PromptRequest(prompt or "Hello", is_prefixed=True)

        inline_prompt = self.inline_prefix_prompt(text, prefix)
        if inline_prompt is not None:
            return PromptRequest(inline_prompt, is_prefixed=True)

        if text.lower() in {"/help", "help"}:
            return PromptRequest("help", is_prefixed=True)

        if self.settings.respond_to_bot_replies and self.is_reply_to_bot(message):
            return PromptRequest(
                self.reply_prompt(message, text),
                is_prefixed=False,
            )

        if not self.settings.respond_only_on_prefix:
            return PromptRequest(text, is_prefixed=False)

        return None

    def inline_prefix_prompt(self, text: str, prefix: str) -> str | None:
        if not prefix:
            return None

        lower_text = text.lower()
        lower_prefix = prefix.lower()
        start = 0

        while True:
            index = lower_text.find(lower_prefix, start)
            if index < 0:
                return None

            end = index + len(prefix)
            before_ok = index == 0 or text[index - 1].isspace()
            after_ok = end == len(text) or text[end].isspace() or text[end] in ",.!?:;"
            if before_ok and after_ok:
                before = text[:index].strip()
                after = text[end:].strip(" \t\r\n,.!?:;")
                prompt = re.sub(r"\s+", " ", f"{before} {after}".strip())
                return prompt or "Hello"

            start = end

    async def handle_control_command(self, thread_id: str, prompt: str) -> str | None:
        parts = prompt.split()
        if not parts:
            return None

        command = parts[0].lower()
        if command in {"help", "commands", "?"}:
            return self.help_message(thread_id)

        if command in {"status", "current"}:
            return self.status_message(thread_id)

        if command == "models":
            include_all = len(parts) > 1 and parts[1].lower() == "all"
            provider_filter = " ".join(parts[2:] if include_all else parts[1:])
            return await self.model_list_message(
                thread_id,
                include_all=include_all,
                provider_filter=provider_filter,
            )

        if command == "providers":
            include_all = len(parts) > 1 and parts[1].lower() == "all"
            return await self.provider_list_message(thread_id, include_all=include_all)

        if command == "provider":
            if len(parts) == 1:
                provider = self.current_provider(thread_id)
                return (
                    f"Current provider: {self.provider_display_name(provider)}\n"
                    f"Use {self.settings.bot_prefix} providers to see options."
                )
            return await self.set_thread_provider(thread_id, " ".join(parts[1:]))

        if command in {"model", "use"}:
            if len(parts) == 1:
                alias = self.current_model_alias(thread_id)
                model = self.current_model(thread_id)
                return (
                    f"Current model: {alias} ({self.model_short_id(model)})\n"
                    f"Use {self.settings.bot_prefix} models to see options."
                )

            return await self.set_thread_model(thread_id, " ".join(parts[1:]))

        if command.startswith("@"):
            if self.resolve_model(command[1:], thread_id) is None:
                return (
                    f"Unknown model alias: {command[1:]}\n"
                    f"Use {self.settings.bot_prefix} models to see available models."
                )
            if len(parts) == 1:
                return await self.set_thread_model(thread_id, command[1:])

        return None

    async def handle_media_command(
        self,
        _message: Message,
        prompt: str,
    ) -> BotResponse | None:
        parts = prompt.split(maxsplit=1)
        if not parts:
            return None

        command = parts[0].lower()

        if command in {"image", "img", "draw"}:
            if len(parts) == 1 or not parts[1].strip():
                return BotResponse(
                    text=f"Usage: {self.settings.bot_prefix} image <prompt>"
                )
            return await self.generate_image_response(parts[1].strip())

        if command in {"see", "vision", "look"}:
            return BotResponse(text=self.openwebui_only_message("vision"))

        return None

    def openwebui_only_message(self, feature: str) -> str:
        return (
            f"{feature.capitalize()} is disabled in Murmur until it can be "
            "bridged through Open WebUI. Murmur is bridge-only."
        )

    def user_facing_error(self, exc: Exception) -> str:
        if isinstance(exc, UserVisibleError):
            return str(exc)
        message = str(exc).strip()
        if message.startswith("Open WebUI "):
            return message
        return "I hit an error while thinking. Check the bot logs."

    def help_message(self, thread_id: str) -> str:
        prefix = self.settings.bot_prefix
        return "\n".join(
            [
                "Murmur help",
                "",
                "Quick start",
                f"- {prefix} your question",
                f"- Put {prefix} anywhere as a standalone tag, e.g. `wtf are you {prefix}`.",
                "- Reply to one of my messages to continue without the prefix.",
                f"- /help or {prefix} help shows this guide.",
                f"- {prefix} status shows current models/providers.",
                "",
                "Text chat",
                f"- {prefix} providers: list provider connections and free model counts.",
                f"- {prefix} provider <provider> [connection]: switch provider for this thread.",
                f"- {prefix} models: list free models grouped by provider.",
                f"- {prefix} models <provider> [connection]: list one provider.",
                f"- {prefix} models all: list all models grouped by provider.",
                f"- {prefix} model: show selected text model.",
                f"- {prefix} model <provider> [connection] <number>: set text model for this thread.",
                f"- {prefix} model <number|alias>: also works after {prefix} models.",
                f"- {prefix} @<number|alias> message: one-shot text model.",
                "",
                "Image generation",
                f"- {prefix} image ...: generate an image.",
                "",
                f"Use {prefix} status for current settings.",
            ]
        )

    def status_message(self, thread_id: str) -> str:
        text_provider = self.provider_display_name(self.current_provider(thread_id))
        text_alias = self.current_model_alias(thread_id)
        return "\n".join(
            [
                "Status",
                f"Text provider: {text_provider} via Open WebUI",
                f"Text model: {text_alias} ({self.model_short_id(self.current_model(thread_id))})",
                f"Image generation: {self.image_provider_label()} ({self.image_model_label()})",
                "Vision: disabled until bridged through Open WebUI",
            ]
        )

    def image_provider_label(self) -> str:
        if self.settings.image_provider_label:
            return self.settings.image_provider_label
        model = self.settings.image_generation_model
        if model and model.startswith("@cf/"):
            return "Cloudflare"
        return "API"

    def image_model_label(self) -> str:
        return self.settings.image_generation_model or "Open WebUI configured default"

    def response_header(self, provider: str, model: str) -> str:
        return f"[{self.provider_display_name(provider)} - {self.model_short_id(model)}]"

    def extract_one_shot_model(
        self,
        thread_id: str,
        prompt: str,
    ) -> tuple[str, str] | None:
        parts = prompt.split(maxsplit=1)
        if not parts or not parts[0].startswith("@"):
            return None

        model = self.resolve_model(parts[0][1:], thread_id)
        if model is None:
            return None

        one_shot_prompt = parts[1].strip() if len(parts) > 1 else "Hello"
        return one_shot_prompt, model

    def current_model(self, thread_id: str) -> str:
        return self.thread_models.get(thread_id, self.settings.openwebui_model)

    def current_model_alias(self, thread_id: str) -> str:
        return self.thread_model_aliases.get(thread_id, "default")

    def current_provider(self, thread_id: str) -> str:
        return self.thread_providers.get(
            thread_id,
            self.provider_for_model(self.current_model(thread_id)),
        )

    async def resolve_chat_model(self, thread_id: str, model: str) -> str:
        cached = self.resolved_model_cache.get(model)
        if cached:
            return cached

        options = self.thread_model_options.get(thread_id)
        if not options:
            options = await self.fetch_model_options(include_all=True)
            if options:
                self.thread_model_options[thread_id] = options
                groups = self.group_model_options(options)
                self.thread_provider_model_options[thread_id] = groups
                self.thread_provider_options[thread_id] = sorted(
                    groups,
                    key=self.provider_sort_key,
                )

        resolved = self.resolve_model_id_from_options(model, options or [])
        self.resolved_model_cache[model] = resolved
        return resolved

    def resolve_model_id_from_options(
        self,
        requested: str,
        options: list[ModelOption],
    ) -> str:
        requested_key = requested.strip().lower()
        requested_short = self.model_short_id(requested).lower()

        for option in options:
            if option.id.lower() == requested_key:
                return option.id

        for option in options:
            option_short = self.model_short_id(option.id).lower()
            if requested_key in {option_short, option.name.lower()}:
                return option.id
            if requested_short and option_short == requested_short:
                return option.id
            if requested_key and option.id.lower().endswith(f".{requested_key}"):
                return option.id

        return requested

    def resolve_model(self, name: str, thread_id: str | None = None) -> str | None:
        key = name.strip().lower().lstrip("@")
        if thread_id and key.isdigit():
            index = int(key) - 1
            options = self.thread_model_options.get(thread_id, [])
            if 0 <= index < len(options):
                return options[index].id

        return self.settings.openwebui_model_aliases.get(key)

    async def set_thread_model(self, thread_id: str, name: str) -> str:
        alias = name.strip().lower().lstrip("@")
        if len(alias.split()) >= 2 and not self.thread_provider_model_options.get(
            thread_id
        ):
            options = await self.fetch_model_options(include_all=False)
            self.thread_model_options[thread_id] = options
            groups = self.group_model_options(options)
            self.thread_provider_model_options[thread_id] = groups
            self.thread_provider_options[thread_id] = sorted(
                groups,
                key=self.provider_sort_key,
            )

        model = self.resolve_provider_model(thread_id, alias) or self.resolve_model(
            alias, thread_id
        )
        if model is None:
            return (
                f"Unknown model: {name}\n"
                f"Use {self.settings.bot_prefix} models to see available models.\n"
                f"Provider model syntax: {self.settings.bot_prefix} model openrouter 1"
            )

        self.thread_models[thread_id] = model
        self.thread_model_aliases[thread_id] = self.model_label(thread_id, alias, model)
        self.thread_providers[thread_id] = self.provider_for_selected_model(
            thread_id, model
        )
        return f"Model set to {alias} ({self.model_short_id(model)}) for this thread."

    def resolve_provider_model(self, thread_id: str, name: str) -> str | None:
        parts = name.split()
        if len(parts) < 2:
            return None

        provider_text = " ".join(parts[:-1])
        model_text = parts[-1]
        groups = self.thread_provider_model_options.get(thread_id, {})
        providers = sorted(groups)
        provider = self.resolve_provider(provider_text, providers)
        if provider is None:
            return None

        models = groups.get(provider, [])
        if model_text.isdigit():
            index = int(model_text) - 1
            if 0 <= index < len(models):
                return models[index].id

        normalized = model_text.strip().lower().lstrip("@")
        for option in models:
            if normalized in {
                option.id.lower(),
                option.name.lower(),
                self.model_short_id(option.id).lower(),
            }:
                return option.id
        return None

    def model_label(self, thread_id: str, requested: str, model: str) -> str:
        if requested.isdigit():
            index = int(requested) - 1
            options = self.thread_model_options.get(thread_id, [])
            if 0 <= index < len(options):
                return str(index + 1)
        return requested or "default"

    def provider_for_selected_model(self, thread_id: str, model_id: str) -> str:
        for provider, options in self.thread_provider_model_options.get(
            thread_id, {}
        ).items():
            if any(option.id == model_id for option in options):
                return provider
        for option in self.thread_model_options.get(thread_id, []):
            if option.id == model_id:
                return self.option_provider(option)
        return self.provider_for_model(model_id)

    async def model_list_message(
        self,
        thread_id: str,
        include_all: bool = False,
        provider_filter: str = "",
    ) -> str:
        options = await self.fetch_model_options(include_all=include_all)
        if options:
            self.thread_model_options[thread_id] = options
            groups = self.group_model_options(options)
            if provider_filter.strip():
                matching_providers = self.matching_provider_filters(
                    provider_filter,
                    sorted(groups, key=self.provider_sort_key),
                )
                if not matching_providers:
                    return (
                        f"Unknown provider: {provider_filter}\n"
                        f"Use {self.settings.bot_prefix} providers to see available providers."
                    )
                options = [
                    option
                    for provider in matching_providers
                    for option in groups.get(provider, [])
                ]
            return self.dynamic_model_list_message(thread_id, options, include_all)

        current_alias = self.current_model_alias(thread_id)
        lines = ["Available models:"]
        for alias, model in self.settings.openwebui_model_aliases.items():
            marker = "*" if alias == current_alias else "-"
            lines.append(f"{marker} {alias}: {model}")

        lines.append("")
        lines.append(f"Switch: {self.settings.bot_prefix} model <name>")
        lines.append(f"One-shot: {self.settings.bot_prefix} @<name> your message")
        return "\n".join(lines)

    def dynamic_model_list_message(
        self,
        thread_id: str,
        options: list[ModelOption],
        include_all: bool,
    ) -> str:
        current_model = self.current_model(thread_id)
        title = "All models" if include_all else "Free models"
        groups = self.group_model_options(options)
        self.thread_provider_model_options[thread_id] = groups
        self.thread_provider_options[thread_id] = sorted(
            groups,
            key=self.provider_sort_key,
        )
        lines = [f"{title} ({len(options)}):"]

        for provider, provider_options in groups.items():
            lines.append("")
            lines.append(f"[{self.provider_display_name(provider)}]")
            for index, option in enumerate(provider_options, start=1):
                current = " (current)" if option.id == current_model else ""
                lines.append(f"{index}. {self.model_display(option)}{current}")

        lines.append("")
        lines.append(f"Providers: {self.settings.bot_prefix} providers")
        lines.append(
            f"Switch: {self.settings.bot_prefix} model <provider> [connection] <number>"
        )
        lines.append(f"Example: {self.settings.bot_prefix} model openrouter 1")
        lines.append(f"Example: {self.settings.bot_prefix} model openrouter 2 1")
        lines.append(f"All models: {self.settings.bot_prefix} models all")
        return "\n".join(lines)

    def group_model_options(
        self, options: list[ModelOption]
    ) -> dict[str, list[ModelOption]]:
        groups: dict[str, list[ModelOption]] = defaultdict(list)
        for option in sorted(options, key=lambda model: model.id):
            groups[self.option_provider(option)].append(option)
        return dict(sorted(groups.items(), key=lambda item: self.provider_sort_key(item[0])))

    def model_display(self, option: ModelOption) -> str:
        model_id = self.model_short_id(option.id)
        name = self.model_short_id(option.name)
        if name and name != model_id:
            return f"{model_id} ({name})"
        return model_id

    def model_short_id(self, model_id: str) -> str:
        if "." in model_id:
            prefix, suffix = model_id.split(".", 1)
            if self.is_provider_connection(prefix):
                return suffix
        return model_id

    async def provider_list_message(
        self, thread_id: str, include_all: bool = False
    ) -> str:
        options = await self.fetch_model_options(include_all=include_all)
        if not options:
            return (
                "No providers returned from the Open WebUI model API.\n"
                "Exact source: Open WebUI /api/models returned no usable models."
            )

        self.thread_model_options[thread_id] = options
        grouped = self.group_model_options(options)

        provider_keys = sorted(grouped, key=self.provider_sort_key)
        self.thread_provider_options[thread_id] = provider_keys
        self.thread_provider_model_options[thread_id] = grouped
        current_provider = self.current_provider(thread_id)
        title = "All providers" if include_all else "Free providers"
        lines = [f"{title} ({len(provider_keys)}):"]
        for index, provider in enumerate(provider_keys, start=1):
            models = grouped[provider]
            marker = " (current)" if provider == current_provider else ""
            sample = ", ".join(self.model_short_id(model.id) for model in models[:3])
            suffix = f" - {sample}" if sample else ""
            lines.append(
                f"{index}. {self.provider_display_name(provider)}"
                f" - {len(models)} model(s){marker}{suffix}"
            )

        lines.append("")
        lines.append(
            f"Switch provider: {self.settings.bot_prefix} provider <provider> [connection]"
        )
        lines.append(
            f"Pick model: {self.settings.bot_prefix} model <provider> [connection] <number>"
        )
        lines.append(f"Models: {self.settings.bot_prefix} models <provider> [connection]")
        lines.append(f"All providers/models: {self.settings.bot_prefix} providers all")
        return "\n".join(lines)

    async def set_thread_provider(self, thread_id: str, name: str) -> str:
        options = self.thread_model_options.get(thread_id)
        providers = self.thread_provider_options.get(thread_id)
        if not options or not providers:
            options = await self.fetch_model_options(include_all=False)
            grouped = self.group_model_options(options)
            providers = sorted(grouped, key=self.provider_sort_key)
            self.thread_model_options[thread_id] = options
            self.thread_provider_options[thread_id] = providers
            self.thread_provider_model_options[thread_id] = grouped
        else:
            grouped = self.thread_provider_model_options.get(thread_id, {})

        provider = self.resolve_provider(name, providers)
        if provider is None:
            return (
                f"Unknown provider: {name}\n"
                f"Use {self.settings.bot_prefix} providers to see available providers."
            )

        provider_models = grouped.get(provider, [])
        if not provider_models:
            return (
                f"No models found for provider {self.provider_display_name(provider)}.\n"
                f"Use {self.settings.bot_prefix} providers all to refresh provider data."
            )

        model = provider_models[0]
        self.thread_providers[thread_id] = provider
        self.thread_models[thread_id] = model.id
        self.thread_model_aliases[thread_id] = self.provider_display_name(provider)
        return (
            f"Provider set to {self.provider_display_name(provider)} for this thread.\n"
            f"Model set to {self.model_short_id(model.id)}."
        )

    def resolve_provider(self, name: str, providers: list[str]) -> str | None:
        key = name.strip().lower()
        if key.isdigit():
            index = int(key) - 1
            if 0 <= index < len(providers):
                return providers[index]

        normalized = self.normalize_provider_key(key)
        for provider in providers:
            if normalized in {
                self.normalize_provider_key(provider),
                self.normalize_provider_key(self.provider_display_name(provider)),
            }:
                return provider

        family_matches = [
            provider
            for provider in providers
            if normalized == self.normalize_provider_key(self.provider_family(provider))
        ]
        if family_matches:
            for provider in family_matches:
                if provider.endswith("_1"):
                    return provider
            return family_matches[0]
        return None

    def matching_provider_filters(
        self,
        name: str,
        providers: list[str],
    ) -> list[str]:
        provider = self.resolve_provider(name, providers)
        if provider and self.normalize_provider_key(name) != self.normalize_provider_key(
            self.provider_family(provider)
        ):
            return [provider]

        normalized = self.normalize_provider_key(name)
        family_matches = [
            provider
            for provider in providers
            if normalized == self.normalize_provider_key(self.provider_family(provider))
        ]
        if family_matches:
            return family_matches
        return [provider] if provider else []

    async def fetch_model_options(self, include_all: bool = False) -> list[ModelOption]:
        models = await self.fetch_openwebui_models()

        if not include_all:
            models = [model for model in models if self.is_free_model(model)]

        return sorted(models, key=lambda model: model.id)

    async def fetch_openwebui_models(self) -> list[ModelOption]:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout_seconds)
        ) as session:
            provider_by_url_idx = await self.fetch_openwebui_provider_map(session)
            connection_models = await self.fetch_openwebui_connection_models(
                session,
                provider_by_url_idx,
            )
            api_models: list[ModelOption] = []
            for path in ("/api/models", "/api/v1/models"):
                try:
                    async with session.get(
                        f"{self.settings.openwebui_base_url}{path}",
                        headers=await self.openwebui_headers(session),
                    ) as response:
                        if response.status >= 400:
                            continue
                        body = await response.json(content_type=None)
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    continue

                api_models = self.parse_models_response(body, provider_by_url_idx)
                if api_models:
                    break

        if connection_models:
            connection_families = {
                self.provider_family(option.provider) for option in connection_models
            }
            api_models = [
                option
                for option in api_models
                if self.provider_family(self.option_provider(option))
                not in connection_families
            ]
            return self.dedupe_model_options(connection_models + api_models)

        return self.dedupe_model_options(api_models)

    async def fetch_openwebui_connection_models(
        self,
        session: aiohttp.ClientSession,
        provider_by_url_idx: dict[str, str],
    ) -> list[ModelOption]:
        models: list[ModelOption] = []
        for url_idx, provider in sorted(
            provider_by_url_idx.items(),
            key=lambda item: int(item[0]) if item[0].isdigit() else item[0],
        ):
            try:
                async with session.get(
                    f"{self.settings.openwebui_base_url}/openai/models/{url_idx}",
                    headers=await self.openwebui_headers(session),
                ) as response:
                    if response.status >= 400:
                        continue
                    body = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                continue

            parsed = self.parse_models_response(
                body,
                provider_by_url_idx={url_idx: provider},
                forced_provider=provider,
            )
            for option in parsed:
                models.append(
                    ModelOption(
                        id=self.provider_model_id(provider, option.id),
                        name=self.model_short_id(option.name),
                        provider=provider,
                        is_free=option.is_free,
                        pricing=option.pricing,
                    )
                )
        return self.dedupe_model_options(models)

    def dedupe_model_options(self, options: list[ModelOption]) -> list[ModelOption]:
        deduped: dict[str, ModelOption] = {}
        for option in options:
            deduped.setdefault(option.id, option)
        return sorted(deduped.values(), key=lambda model: model.id)

    async def fetch_openwebui_provider_map(
        self, session: aiohttp.ClientSession
    ) -> dict[str, str]:
        try:
            async with session.get(
                f"{self.settings.openwebui_base_url}/openai/config",
                headers=await self.openwebui_headers(session),
            ) as response:
                if response.status >= 400:
                    return {}
                body = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, TypeError):
            return {}

        if not isinstance(body, dict):
            return {}

        urls = body.get("OPENAI_API_BASE_URLS") or []
        configs = body.get("OPENAI_API_CONFIGS") or {}
        if not isinstance(urls, list) or not isinstance(configs, dict):
            return {}

        provider_by_url_idx: dict[str, str] = {}
        for index, url in enumerate(urls):
            url_text = str(url)
            config = configs.get(str(index), configs.get(url_text, {}))
            if not isinstance(config, dict):
                config = {}

            prefix_id = config.get("prefix_id")
            if isinstance(prefix_id, str) and prefix_id.strip():
                provider_by_url_idx[str(index)] = self.canonical_provider_id(prefix_id)
                continue

            provider_by_url_idx[str(index)] = self.provider_for_base_url(url_text)

        return provider_by_url_idx

    def parse_models_response(
        self,
        body: object,
        provider_by_url_idx: dict[str, str] | None = None,
        forced_provider: str | None = None,
    ) -> list[ModelOption]:
        provider_by_url_idx = provider_by_url_idx or {}
        if isinstance(body, dict):
            raw_models = body.get("data") or body.get("models") or []
        elif isinstance(body, list):
            raw_models = body
        else:
            raw_models = []

        models = []
        for raw_model in raw_models:
            if isinstance(raw_model, str):
                models.append(
                    ModelOption(
                        id=raw_model,
                        name=raw_model,
                        provider=forced_provider or self.provider_for_model(raw_model),
                        is_free=self.is_free_model_id(raw_model, raw_model),
                    )
                )
                continue

            if not isinstance(raw_model, dict):
                continue

            model_id = (
                raw_model.get("id")
                or raw_model.get("model")
                or raw_model.get("name")
                or raw_model.get("value")
            )
            if not model_id:
                continue

            name = raw_model.get("name") or raw_model.get("title") or model_id
            models.append(
                ModelOption(
                    id=str(model_id),
                    name=str(name),
                    provider=forced_provider
                    or self.provider_from_raw_model(
                        raw_model,
                        str(model_id),
                        provider_by_url_idx,
                    ),
                    is_free=self.is_free_raw_model(raw_model, str(model_id), str(name)),
                    pricing=(
                        raw_model.get("pricing")
                        if isinstance(raw_model.get("pricing"), dict)
                        else None
                    ),
                )
            )

        return models

    def provider_from_raw_model(
        self,
        raw_model: dict,
        model_id: str,
        provider_by_url_idx: dict[str, str],
    ) -> str:
        url_idx = raw_model.get("urlIdx")
        if url_idx is not None:
            provider = provider_by_url_idx.get(str(url_idx))
            if provider:
                return provider

        provider = raw_model.get("provider") or raw_model.get("source")
        if isinstance(provider, str) and provider.strip():
            return self.canonical_provider_id(provider)

        return self.provider_for_model(model_id)

    def provider_for_base_url(self, base_url: str) -> str:
        hostname = urlparse(base_url).hostname or ""
        hostname = hostname.lower()
        if "openrouter.ai" in hostname:
            return "openrouter"
        if hostname:
            return hostname.removeprefix("api.").split(".", 1)[0]
        return "openwebui"

    def provider_for_model(self, model_id: str) -> str:
        model_id = model_id.strip()
        if model_id.startswith("openrouter/"):
            return "openrouter"
        if "." in model_id:
            prefix = model_id.split(".", 1)[0].strip().lower()
            if self.is_provider_connection(prefix):
                return self.canonical_provider_id(prefix)
        return "openwebui"

    def option_provider(self, option: ModelOption) -> str:
        return option.provider or self.provider_for_model(option.id)

    def provider_display_name(self, provider: str) -> str:
        provider = self.canonical_provider_id(provider)
        return provider.replace("_", " ")

    def normalize_provider_key(self, provider: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", provider.lower())

    def canonical_provider_id(self, provider: str) -> str:
        provider = provider.strip().lower() or "openwebui"
        provider = re.sub(r"[^a-z0-9]+", "_", provider).strip("_")
        if provider in {"open_webui", "open_web_ui"}:
            return "openwebui"
        legacy_openrouter = re.fullmatch(r"or_?(\d+)", provider)
        if legacy_openrouter:
            return f"openrouter_{legacy_openrouter.group(1)}"
        return provider

    def is_provider_connection(self, provider: str) -> bool:
        provider = self.canonical_provider_id(provider)
        return bool(re.fullmatch(r"[a-z][a-z0-9]*_\d+", provider))

    def provider_family(self, provider: str) -> str:
        provider = self.canonical_provider_id(provider)
        match = re.fullmatch(r"([a-z][a-z0-9]*)_\d+", provider)
        if match:
            return match.group(1)
        return provider

    def provider_sort_key(self, provider: str) -> tuple[str, int, str]:
        provider = self.canonical_provider_id(provider)
        match = re.fullmatch(r"([a-z][a-z0-9]*)_(\d+)", provider)
        if match:
            return (match.group(1), int(match.group(2)), provider)
        return (provider, 0, provider)

    def provider_model_id(self, provider: str, model_id: str) -> str:
        provider = self.canonical_provider_id(provider)
        model_id = self.model_short_id(model_id.strip())
        if self.is_provider_connection(provider) and not model_id.startswith(
            f"{provider}."
        ):
            return f"{provider}.{model_id}"
        return model_id

    def is_free_model(self, model: ModelOption) -> bool:
        return model.is_free or self.is_free_model_id(model.id, model.name)

    def is_free_model_id(self, model_id: str, name: str) -> bool:
        model_id = self.model_short_id(model_id).lower()
        name = name.lower()
        return (
            model_id == "openrouter/free"
            or model_id.endswith(":free")
            or ":free" in model_id
            or name.startswith("free ")
            or name.endswith("(free)")
            or " free" in name
        )

    def is_free_raw_model(self, raw_model: dict, model_id: str, name: str) -> bool:
        if self.is_free_model_id(model_id, name):
            return True

        pricing = raw_model.get("pricing")
        if not isinstance(pricing, dict) or not pricing:
            return False

        numeric_prices = []
        for value in pricing.values():
            try:
                numeric_prices.append(float(value))
            except (TypeError, ValueError):
                pass

        return bool(numeric_prices) and all(price == 0 for price in numeric_prices)

    def is_reply_to_bot(self, message: Message) -> bool:
        replied_to = message.replied_to_message
        if replied_to and replied_to.sender_id == self.client.uid:
            return True

        replied_to_id = message.replied_to_message_id
        return bool(
            replied_to_id and replied_to_id in self.sent_message_ids[message.thread_id]
        )

    def reply_prompt(self, message: Message, text: str) -> str:
        replied_to = message.replied_to_message
        if replied_to and replied_to.text:
            return (
                "The user replied to your previous Messenger message:\n"
                f"{replied_to.text.strip()}\n\n"
                f"User reply:\n{text}"
            )
        return text

    def split_reply(self, text: str) -> list[str]:
        if len(text) <= self.settings.max_reply_chars:
            return [text]

        parts: list[str] = []
        remaining = text.strip()
        while remaining:
            chunk = remaining[: self.settings.max_reply_chars]
            cut = max(chunk.rfind("\n"), chunk.rfind(". "), chunk.rfind(" "))
            if cut < self.settings.max_reply_chars // 2:
                cut = self.settings.max_reply_chars
            parts.append(remaining[:cut].strip())
            remaining = remaining[cut:].strip()
        return [part for part in parts if part]

    async def ask_openwebui(self, thread_id: str, prompt: str, model: str) -> str:
        model = await self.resolve_chat_model(thread_id, model)
        messages = [{"role": "system", "content": self.settings.system_prompt}]
        messages.extend(self.history[thread_id])
        messages.append({"role": "user", "content": prompt})

        answer = await self.request_chat_completion(
            {
                "model": model,
                "messages": messages,
                "stream": False,
            }
        )

        self.history[thread_id].append({"role": "user", "content": prompt})
        self.history[thread_id].append({"role": "assistant", "content": answer})
        provider = self.provider_for_selected_model(thread_id, model)
        return f"{self.response_header(provider, model)}\n{answer}"

    async def generate_image_response(self, prompt: str) -> BotResponse:
        paths = await self.request_image_generation(prompt)
        model = self.image_model_label()
        size = f", {self.settings.image_size}" if self.settings.image_size else ""
        return BotResponse(
            text=f"[{self.image_provider_label()} - {model}{size}]",
            file_paths=paths,
            cleanup_paths=paths,
        )

    async def request_image_generation(self, prompt: str) -> list[str]:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout_seconds)
        ) as session:
            body, status = await self.post_image_generation(session, prompt)
            if status == 401 and not self.settings.openwebui_api_key:
                self.openwebui_token = None
                body, status = await self.post_image_generation(session, prompt)

            if status >= 400:
                raise UserVisibleError(self.image_error_message(prompt, status, body))

            image_refs = self.parse_image_generation_response(body)
            if not image_refs:
                raise RuntimeError(f"Unexpected Open WebUI image response: {body}")

            paths = []
            for image_ref in image_refs:
                paths.append(await self.materialize_generated_image(session, image_ref))
            return paths

    async def post_image_generation(
        self, session: aiohttp.ClientSession, prompt: str
    ) -> tuple[object, int]:
        payload: dict[str, object] = {"prompt": prompt, "n": 1}
        if self.settings.image_generation_model:
            payload["model"] = self.settings.image_generation_model
        if self.settings.image_size:
            payload["size"] = self.settings.image_size
        if self.settings.image_steps is not None:
            payload["steps"] = self.settings.image_steps

        async with session.post(
            f"{self.settings.openwebui_base_url}/api/v1/images/generations",
            headers=await self.openwebui_headers(session),
            json=payload,
        ) as response:
            try:
                body: object = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                body = await response.text()
            return body, response.status

    def image_error_message(self, prompt: str, status: int, body: object) -> str:
        raw_error = self.read_image_proxy_error(prompt)
        if raw_error:
            return "Image generation failed.\nRaw upstream error:\n" + raw_error
        return f"Image generation failed.\nOpen WebUI image error {status}: {body}"

    def read_image_proxy_error(self, prompt: str) -> str | None:
        prompt_id = hashlib.sha256(
            prompt.encode("utf-8", errors="replace")
        ).hexdigest()[:12]
        error_dir = Path(os.getenv("IMAGE_PROXY_ERROR_DIR", "/tmp/murmur-image-errors"))
        error_path = error_dir / f"{prompt_id}.json"
        try:
            payload = json.loads(error_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

        message = payload.get("message") if isinstance(payload, dict) else None
        return str(message) if message else None

    def parse_image_generation_response(self, body: object) -> list[dict[str, str]]:
        raw_items: list[object]
        if isinstance(body, dict):
            raw = body.get("data") or body.get("images") or body.get("image") or body
            raw_items = raw if isinstance(raw, list) else [raw]
        elif isinstance(body, list):
            raw_items = body
        else:
            raw_items = [body]

        refs: list[dict[str, str]] = []
        for item in raw_items:
            ref = self.parse_image_item(item)
            if ref:
                refs.append(ref)
        return refs

    def parse_image_item(self, item: object) -> dict[str, str] | None:
        if isinstance(item, str):
            value = item.strip()
            if value.startswith(("http://", "https://", "/")):
                return {"type": "url", "value": value}
            if value.startswith("data:image/"):
                return {"type": "data_url", "value": value}
            if self.looks_like_base64(value):
                return {"type": "base64", "value": value}
            return None

        if not isinstance(item, dict):
            return None

        for key in ("url", "image_url", "path"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return {"type": "url", "value": value}

        for key in ("b64_json", "base64", "image"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return self.parse_image_item(value)

        nested = item.get("data")
        if nested is not None and nested is not item:
            return self.parse_image_item(nested)

        return None

    def looks_like_base64(self, value: str) -> bool:
        if len(value) < 64:
            return False
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            return False
        return True

    async def materialize_generated_image(
        self, session: aiohttp.ClientSession, image_ref: dict[str, str]
    ) -> str:
        ref_type = image_ref["type"]
        value = image_ref["value"]

        if ref_type == "url":
            return await self.download_generated_image(session, value)

        if ref_type == "data_url":
            header, data = value.split(",", 1)
            content_type = header.split(";", 1)[0].removeprefix("data:")
            return self.write_generated_image(
                base64.b64decode(data), content_type or "image/png"
            )

        return self.write_generated_image(base64.b64decode(value), "image/png")

    async def download_generated_image(
        self, session: aiohttp.ClientSession, image_url: str
    ) -> str:
        url = urljoin(f"{self.settings.openwebui_base_url}/", image_url)
        async with session.get(
            url,
            headers=await self.openwebui_headers(session),
        ) as response:
            if response.status >= 400:
                body = await response.text()
                raise RuntimeError(f"Open WebUI image download {response.status}: {body}")
            content_type = response.headers.get("Content-Type", "image/png")
            return self.write_generated_image(await response.read(), content_type)

    def write_generated_image(self, content: bytes, content_type: str) -> str:
        suffix = ".jpg" if "jpeg" in content_type.lower() else ".png"
        with tempfile.NamedTemporaryFile(
            prefix="murmur-image-", suffix=suffix, delete=False
        ) as image_file:
            image_file.write(content)
            return image_file.name

    async def request_chat_completion(self, payload: dict) -> str:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout_seconds)
        ) as session:
            body, status = await self.post_chat_completion(session, payload)
            if status == 401 and not self.settings.openwebui_api_key:
                self.openwebui_token = None
                body, status = await self.post_chat_completion(session, payload)

        if status >= 400:
            raise UserVisibleError(self.openwebui_error_message(status, body))

        try:
            if not isinstance(body, dict):
                raise TypeError("Open WebUI returned a non-JSON object")
            return body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected Open WebUI response: {body}") from exc

    async def post_chat_completion(
        self, session: aiohttp.ClientSession, payload: dict
    ) -> tuple[object, int]:
        async with session.post(
            f"{self.settings.openwebui_base_url}/api/chat/completions",
            headers=await self.openwebui_headers(session),
            json=payload,
        ) as response:
            return await self.read_response_body(response), response.status

    async def read_response_body(self, response: aiohttp.ClientResponse) -> object:
        try:
            return await response.json(content_type=None)
        except Exception:
            return await response.text()

    def openwebui_error_message(self, status: int, body: object) -> str:
        message = f"Open WebUI error {status}: {body}"
        if self.is_rate_limit_error(body):
            message += (
                "\n\nRate limit hit. Try another provider/model: "
                f"{self.settings.bot_prefix} providers, then "
                f"{self.settings.bot_prefix} models <provider> [connection], then "
                f"{self.settings.bot_prefix} model <provider> [connection] <number>."
            )
        return message

    def is_rate_limit_error(self, body: object) -> bool:
        text = str(body).lower()
        return any(
            phrase in text
            for phrase in (
                "rate limit",
                "rate_limit",
                "free-models-per-day",
                "quota",
                "too many requests",
            )
        )

    async def openwebui_headers(self, session: aiohttp.ClientSession) -> dict[str, str]:
        token = self.settings.openwebui_api_key or await self.openwebui_jwt(session)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def openwebui_jwt(self, session: aiohttp.ClientSession) -> str | None:
        if self.openwebui_token:
            return self.openwebui_token

        if (
            not self.settings.openwebui_login_email
            or not self.settings.openwebui_login_password
        ):
            return None

        payload = {
            "email": self.settings.openwebui_login_email,
            "password": self.settings.openwebui_login_password,
        }

        async with session.post(
            f"{self.settings.openwebui_base_url}/api/v1/auths/signin",
            headers={"Content-Type": "application/json"},
            json=payload,
        ) as response:
            body = await response.json(content_type=None)
            if response.status >= 400:
                raise UserVisibleError(
                    f"Open WebUI sign-in error {response.status}: {body}"
                )

        try:
            self.openwebui_token = body["token"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(f"Unexpected Open WebUI sign-in response: {body}") from exc

        return self.openwebui_token


def main() -> None:
    Murmur(load_settings()).run()
