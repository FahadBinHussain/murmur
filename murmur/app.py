import asyncio
import base64
import binascii
import hashlib
import json
import mimetypes
import os
import re
import socket
import tempfile
import time
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

from .bnp_notifications import BnpNotificationWorker
from .fbchat_patch import apply_fbchat_patches
from .admin_state import (
    read_thread_allowlist,
    read_thread_registry,
    thread_allowed,
    thread_allowlist_path,
    thread_registry_path,
    write_thread_registry,
)
from .runtime_state import (
    RuntimeStateMissing,
    RuntimeStateNotConfigured,
    load_facebook_proxy_state,
)
from .lobe_sync import LobeChatExchange, LobeFileAttachment, LobeSyncConfig, LobeSyncer
from .webshare_proxy import ensure_webshare_proxy_state, network_policy

apply_fbchat_patches()


DEFAULT_FB_UPLOAD_ENDPOINTS = [
    "https://upload.facebook.com/ajax/mercury/upload.php",
    "https://upload.messenger.com/ajax/mercury/upload.php",
]

FACEBOOK_COOKIE_EXPIRED_EXIT_CODE = 42
FACEBOOK_COOKIE_EXPIRED_SIGNATURES = (
    "async_get_token' not found",
    "fb_dtsg' token not found",
    "failed to load session from cookies",
    "failed to extract session data",
    "cookie json is missing required facebook session cookies",
    "invalid fbstate format",
    "not logged in - please authenticate",
    "please refresh your authentication cookies",
    "facebook returned checkpoint/login page",
    "cookies are not fully authenticated",
    "code: 1357001",
    "code: 1357004",
)
FACEBOOK_NETWORK_FAILURE_SIGNATURES = (
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


class UserVisibleError(RuntimeError):
    pass


class GatewayResponseError(UserVisibleError):
    def __init__(
        self,
        message: str,
        *,
        status: int,
        body: object,
        model: object | None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.model = model
        self.retryable = retryable


@dataclass(frozen=True)
class Settings:
    ai_backend: str
    litellm_base_url: str
    litellm_api_key: str | None
    litellm_model: str
    litellm_model_aliases: dict[str, str]
    litellm_warmup: bool
    litellm_warmup_chat: bool
    openwebui_base_url: str
    openwebui_api_key: str | None
    openwebui_login_email: str | None
    openwebui_login_password: str | None
    openwebui_model: str
    openwebui_model_aliases: dict[str, str]
    openwebui_warmup: bool
    openwebui_warmup_chat: bool
    lobe_sync_enabled: bool
    lobe_database_url: str
    lobe_user_email: str | None
    lobe_user_id: str | None
    lobe_agent_title: str
    lobe_agent_slug: str
    lobe_session_title: str
    lobe_session_slug: str
    lobe_topic_prefix: str
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
    fb_log_names: bool
    fb_log_names_keep_ids: bool
    fb_log_name_cache_path: str
    bot_prefix: str
    respond_only_on_prefix: bool
    respond_to_bot_replies: bool
    max_history_messages: int
    max_reply_chars: int
    request_timeout_seconds: int
    allowed_thread_ids: set[str]
    thread_registry_path: str
    thread_allowlist_path: str
    system_prompt: str


@dataclass(frozen=True)
class PromptRequest:
    text: str
    is_prefixed: bool
    display_text: str | None = None


@dataclass(frozen=True)
class ChatCommandResult:
    prompt: str | None = None
    response: str | None = None


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
    task: str | None = None
    capabilities: tuple[str, ...] = ()


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def exception_chain_texts(exc: BaseException) -> list[str]:
    texts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc

    while current is not None and id(current) not in seen:
        seen.add(id(current))
        texts.append(f"{type(current).__name__}: {current}".lower())
        current = current.__cause__ or current.__context__

    return texts


def facebook_network_failure_error(exc: BaseException) -> bool:
    text = "\n".join(exception_chain_texts(exc))
    return any(signature in text for signature in FACEBOOK_NETWORK_FAILURE_SIGNATURES)


def facebook_cookie_expired_error(exc: BaseException) -> bool:
    if facebook_network_failure_error(exc):
        return False

    text = "\n".join(exception_chain_texts(exc))
    return any(signature in text for signature in FACEBOOK_COOKIE_EXPIRED_SIGNATURES)


def parse_model_aliases(default_model: str, env_name: str) -> dict[str, str]:
    if not default_model:
        return {}
    aliases = {"default": default_model}
    raw_aliases = os.getenv(env_name, "")

    for item in raw_aliases.replace("\n", ",").split(","):
        item = item.strip()
        if not item:
            continue

        if "=" not in item:
            raise ValueError(
                f"{env_name} must use alias=model pairs, got {item!r}"
            )

        alias, model = item.split("=", 1)
        alias = alias.strip().lower().lstrip("@")
        model = model.strip()
        if not alias or not model:
            raise ValueError(
                f"{env_name} must use non-empty alias=model pairs"
            )
        aliases[alias] = model

    return aliases


def normalize_ai_backend(value: str | None) -> str:
    backend = (value or "litellm").strip().lower()
    backend = re.sub(r"[^a-z0-9]+", "_", backend).strip("_")
    if backend in {"openwebui", "open_webui", "open_web_ui", "webui"}:
        return "openwebui"
    if backend in {"litellm", "lite_llm", "openai", "openai_compatible"}:
        return "litellm"
    raise ValueError("MURMUR_AI_BACKEND must be either litellm or openwebui")


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


def normalize_litellm_base_url(value: str | None) -> str:
    raw = (value or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.path.rstrip("/").endswith("/v1"):
        return raw
    return f"{raw}/v1"


def env_proxy(name: str) -> str | None:
    value = os.getenv(name)
    return normalize_proxy(value)


def normalize_proxy(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if value.lower() in {"direct", "none", "off", "false"}:
        return None
    if "://" not in value:
        value = f"http://{value}"
    return value


def proxy_direct_requested(value: str | None) -> bool:
    return (value or "").strip().lower() in {"direct", "none", "off", "false"}


def load_runtime_proxy_state() -> dict[str, str]:
    if not env_bool("MURMUR_LOAD_PROXY_STATE", True):
        return {}
    try:
        return load_facebook_proxy_state()
    except (RuntimeStateMissing, RuntimeStateNotConfigured):
        return {}
    except Exception as exc:
        print(f"DB proxy state unavailable: {exc}")
        return {}


def setting_proxy(
    proxy_state: dict[str, str], key: str, fallback: str | None = None
) -> str | None:
    if key in proxy_state:
        raw = proxy_state.get(key)
    else:
        raw = os.getenv(key)
    if proxy_direct_requested(raw):
        return None
    return normalize_proxy(raw) or fallback


def load_settings() -> Settings:
    load_dotenv()
    facebook_network_policy = network_policy()
    proxy_state = {} if facebook_network_policy == "direct" else load_runtime_proxy_state()
    ai_backend = normalize_ai_backend(os.getenv("MURMUR_AI_BACKEND"))

    litellm_base_url = normalize_litellm_base_url(
        os.getenv("LITELLM_BASE_URL")
        or os.getenv("LITELLM_API_BASE_URL")
        or os.getenv("OPENAI_API_BASE_URL")
    )
    litellm_model = os.getenv("LITELLM_MODEL", "")
    if ai_backend == "litellm" and not litellm_model:
        litellm_model = os.environ["LITELLM_MODEL"]

    port = os.getenv("PORT", "8080")
    openwebui_base_url = (
        os.getenv("OPENWEBUI_BASE_URL") or f"http://127.0.0.1:{port}"
    ).rstrip("/")
    openwebui_model = os.getenv("OPENWEBUI_MODEL", "")
    if ai_backend == "openwebui" and not openwebui_model:
        openwebui_model = os.environ["OPENWEBUI_MODEL"]

    if facebook_network_policy == "direct":
        fb_proxy = None
        fb_upload_proxy = None
        fb_mqtt_proxy = None
    else:
        fb_proxy = setting_proxy(proxy_state, "FB_PROXY")
        fb_upload_proxy = setting_proxy(proxy_state, "FB_UPLOAD_PROXY", fb_proxy)
        fb_mqtt_proxy = setting_proxy(proxy_state, "FB_MQTT_PROXY", fb_proxy)

    allowed_thread_ids = {
        thread_id.strip()
        for thread_id in os.getenv("ALLOWED_THREAD_IDS", "").split(",")
        if thread_id.strip()
    }

    thread_registry_file = os.getenv("MURMUR_THREAD_REGISTRY_PATH") or str(
        thread_registry_path()
    )
    return Settings(
        ai_backend=ai_backend,
        litellm_base_url=litellm_base_url,
        litellm_api_key=os.getenv("LITELLM_API_KEY") or None,
        litellm_model=litellm_model,
        litellm_model_aliases=parse_model_aliases(
            litellm_model,
            "LITELLM_MODEL_ALIASES",
        ),
        litellm_warmup=env_bool("LITELLM_WARMUP", True),
        litellm_warmup_chat=env_bool("LITELLM_WARMUP_CHAT", True),
        openwebui_base_url=openwebui_base_url,
        openwebui_api_key=os.getenv("OPENWEBUI_API_KEY") or None,
        openwebui_login_email=os.getenv("OPENWEBUI_LOGIN_EMAIL")
        or os.getenv("WEBUI_ADMIN_EMAIL")
        or None,
        openwebui_login_password=os.getenv("OPENWEBUI_LOGIN_PASSWORD")
        or os.getenv("WEBUI_ADMIN_PASSWORD")
        or None,
        openwebui_model=openwebui_model,
        openwebui_model_aliases=parse_model_aliases(
            openwebui_model,
            "OPENWEBUI_MODEL_ALIASES",
        ),
        openwebui_warmup=env_bool("OPENWEBUI_WARMUP", True),
        openwebui_warmup_chat=env_bool("OPENWEBUI_WARMUP_CHAT", True),
        lobe_sync_enabled=env_bool("LOBE_SYNC_ENABLED", False),
        lobe_database_url=os.getenv("LOBE_DATABASE_URL", "").strip(),
        lobe_user_email=os.getenv("LOBE_SYNC_USER_EMAIL") or None,
        lobe_user_id=os.getenv("LOBE_SYNC_USER_ID") or None,
        lobe_agent_title=os.getenv("LOBE_SYNC_AGENT_TITLE", "Murmur"),
        lobe_agent_slug=os.getenv("LOBE_SYNC_AGENT_SLUG", "murmur"),
        lobe_session_title=os.getenv("LOBE_SYNC_SESSION_TITLE", "Murmur"),
        lobe_session_slug=os.getenv("LOBE_SYNC_SESSION_SLUG", "murmur"),
        lobe_topic_prefix=os.getenv("LOBE_SYNC_TOPIC_PREFIX", "Messenger"),
        image_generation_model=os.getenv("IMAGE_GENERATION_MODEL") or None,
        image_size=os.getenv("IMAGE_SIZE") or None,
        image_steps=env_int("IMAGE_STEPS"),
        fb_cookies_path=os.getenv("FB_COOKIES_PATH", "cookies.json"),
        fb_user_agent=os.getenv("FB_USER_AGENT") or None,
        fb_proxy=fb_proxy,
        fb_mqtt_proxy=fb_mqtt_proxy,
        fb_mqtt_watchdog_seconds=int(os.getenv("FB_MQTT_WATCHDOG_SECONDS", "15")),
        fb_http_timeout_seconds=int(os.getenv("FB_HTTP_TIMEOUT_SECONDS", "120")),
        fb_upload_proxy=fb_upload_proxy,
        fb_upload_retries=int(os.getenv("FB_UPLOAD_RETRIES", "3")),
        fb_upload_endpoints=env_csv(
            "FB_UPLOAD_ENDPOINTS", DEFAULT_FB_UPLOAD_ENDPOINTS
        ),
        fb_log_names=env_bool("FB_LOG_NAMES", True),
        fb_log_names_keep_ids=env_bool("FB_LOG_NAMES_KEEP_IDS", True),
        fb_log_name_cache_path=os.getenv("FB_LOG_NAME_CACHE_PATH")
        or str(Path(tempfile.gettempdir()) / "murmur-fb-names.json"),
        bot_prefix=os.getenv("BOT_PREFIX", "/ai").strip(),
        respond_only_on_prefix=env_bool("RESPOND_ONLY_ON_PREFIX", True),
        respond_to_bot_replies=env_bool("RESPOND_TO_BOT_REPLIES", True),
        max_history_messages=int(os.getenv("MAX_HISTORY_MESSAGES", "12")),
        max_reply_chars=int(os.getenv("MAX_REPLY_CHARS", "1800")),
        request_timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "120")),
        allowed_thread_ids=allowed_thread_ids,
        thread_registry_path=thread_registry_file,
        thread_allowlist_path=os.getenv("MURMUR_THREAD_ALLOWLIST_PATH")
        or str(thread_allowlist_path()),
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
        self.thread_image_models: dict[str, str] = {}
        self.thread_image_model_aliases: dict[str, str] = {}
        self.thread_image_model_options: dict[str, list[ModelOption]] = {}
        self.thread_model_options: dict[str, list[ModelOption]] = {}
        self.thread_providers: dict[str, str] = {}
        self.thread_provider_options: dict[str, list[str]] = {}
        self.thread_provider_model_options: dict[str, dict[str, list[ModelOption]]] = {}
        self.resolved_model_cache: dict[str, str] = {}
        self.openwebui_token: str | None = None
        self.lobe_sync = LobeSyncer(
            LobeSyncConfig(
                enabled=settings.lobe_sync_enabled,
                database_url=settings.lobe_database_url,
                user_email=settings.lobe_user_email,
                user_id=settings.lobe_user_id,
                agent_title=settings.lobe_agent_title,
                agent_slug=settings.lobe_agent_slug,
                session_title=settings.lobe_session_title,
                session_slug=settings.lobe_session_slug,
                topic_prefix=settings.lobe_topic_prefix,
            )
        )
        self.mqtt_watchdog_task: asyncio.Task | None = None
        self.thread_registry_refresh_task: asyncio.Task | None = None
        self.bnp_notification_task: asyncio.Task | None = None
        self.response_tasks: set[asyncio.Task] = set()
        self.file_upload_lock = asyncio.Lock()
        self.fb_user_names: dict[str, str] = {}
        self.fb_thread_names: dict[str, str] = {}
        self.fb_thread_name_tasks: dict[str, asyncio.Task] = {}
        self.fb_user_name_tasks: dict[str, asyncio.Task] = {}
        self.fb_name_cache_path = Path(settings.fb_log_name_cache_path)
        self.thread_registry_path = Path(settings.thread_registry_path)
        self.thread_allowlist_path = Path(settings.thread_allowlist_path)
        self.thread_registry = read_thread_registry(self.thread_registry_path)
        self.thread_allowlist_mtime: float | None = None
        self.thread_allowlist_mode = "allowlist" if settings.allowed_thread_ids else "allow_all"
        self.thread_allowlist_ids: set[str] = set(settings.allowed_thread_ids)
        self.load_facebook_name_cache()
        self.configure_facebook_http_timeout()
        self.configure_mqtt_proxy()
        self.client = Client(
            cookies_file_path=settings.fb_cookies_path,
            userAgent=settings.fb_user_agent,
            proxy=settings.fb_proxy,
        )
        self.patch_facebook_event_logs()
        self.client.event(EventType.LISTENING)(self.on_listening)
        self.client.event(EventType.MESSAGE)(self.on_message)
        self.bnp_notification_worker = BnpNotificationWorker(self.client)

    def patch_facebook_event_logs(self) -> None:
        if not self.settings.fb_log_names:
            return

        self.client.on_listening = self.log_facebook_listening_event
        self.client.on_message = self.log_facebook_message_event
        self.client.on_message_seen = self.log_facebook_seen_event
        self.client.on_message_reaction = self.log_facebook_reaction_event
        self.client.on_message_unsent = self.log_facebook_unsent_event
        self.client.on_message_delivered = self.log_facebook_delivered_event
        self.client.on_mark_read = self.log_facebook_mark_read_event
        self.client.on_typing = self.log_facebook_typing_event

    def load_facebook_name_cache(self) -> None:
        try:
            raw = json.loads(self.fb_name_cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        users = raw.get("users", {}) if isinstance(raw, dict) else {}
        threads = raw.get("threads", {}) if isinstance(raw, dict) else {}
        if isinstance(users, dict):
            self.fb_user_names = {
                str(user_id): str(name)
                for user_id, name in users.items()
                if user_id and name
            }
        if isinstance(threads, dict):
            self.fb_thread_names = {
                str(thread_id): str(name)
                for thread_id, name in threads.items()
                if thread_id and name
            }

    def write_facebook_name_cache(self) -> None:
        if not self.settings.fb_log_names:
            return

        try:
            self.fb_name_cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "users": dict(sorted(self.fb_user_names.items())),
                "threads": dict(sorted(self.fb_thread_names.items())),
            }
            temp_path = self.fb_name_cache_path.with_suffix(
                f"{self.fb_name_cache_path.suffix}.tmp"
            )
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            temp_path.replace(self.fb_name_cache_path)
        except OSError as exc:
            print(f"Messenger name cache write failed: {exc}")

    @staticmethod
    def facebook_id(value: object) -> str:
        if value is None:
            return ""
        return str(value).strip()

    def remember_facebook_user_name(self, user_id: object, name: object) -> bool:
        user_id_text = self.facebook_id(user_id)
        name_text = str(name or "").strip()
        if not user_id_text or not name_text or name_text == user_id_text:
            return False
        if self.fb_user_names.get(user_id_text) == name_text:
            return False
        self.fb_user_names[user_id_text] = name_text
        return True

    def remember_facebook_thread_name(self, thread_id: object, name: object) -> bool:
        thread_id_text = self.facebook_id(thread_id)
        name_text = str(name or "").strip()
        if not thread_id_text or not name_text or name_text == thread_id_text:
            return False
        if self.fb_thread_names.get(thread_id_text) == name_text:
            return False
        self.fb_thread_names[thread_id_text] = name_text
        return True

    def remember_thread_registry_entry(
        self,
        thread_id: object,
        name: object = None,
        thread_type: object = None,
        last_sender_id: object = None,
        participants: object = None,
    ) -> None:
        thread_id_text = self.facebook_id(thread_id)
        if not thread_id_text:
            return

        entry = self.thread_registry.setdefault(thread_id_text, {"id": thread_id_text})
        entry["id"] = thread_id_text
        name_text = str(name or "").strip()
        if name_text and name_text != thread_id_text:
            entry["name"] = name_text
        elif self.fb_thread_names.get(thread_id_text):
            entry["name"] = self.fb_thread_names[thread_id_text]
        if thread_type is not None:
            entry["type"] = getattr(thread_type, "name", str(thread_type))
        sender_id_text = self.facebook_id(last_sender_id)
        if sender_id_text:
            entry["last_sender_id"] = sender_id_text
            if self.fb_user_names.get(sender_id_text):
                entry["last_sender_name"] = self.fb_user_names[sender_id_text]
        participant_entries = self.facebook_participant_entries(participants)
        if not participant_entries and name_text:
            participant_entries = [{"id": thread_id_text, "name": name_text}]
        if participant_entries:
            entry["participants"] = participant_entries
        entry["last_seen"] = int(time.time())
        self.write_thread_registry()

    def facebook_participant_entries(self, participants: object) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        seen: set[str] = set()
        if not participants:
            return entries

        if isinstance(participants, (list, tuple, set)):
            participant_values = participants
        else:
            participant_values = [participants]

        for participant in participant_values:
            if isinstance(participant, dict):
                participant_id = self.facebook_id(
                    participant.get("id")
                    or participant.get("uid")
                    or participant.get("user_id")
                )
                name = str(
                    participant.get("name")
                    or participant.get("short_name")
                    or ""
                ).strip()
            else:
                participant_id = self.facebook_id(
                    getattr(participant, "id", "")
                    or getattr(participant, "uid", "")
                    or getattr(participant, "user_id", "")
                    or participant
                )
                name = str(
                    getattr(participant, "name", "")
                    or getattr(participant, "short_name", "")
                    or ""
                ).strip()

            if not participant_id or participant_id in seen:
                continue
            seen.add(participant_id)
            entry = {"id": participant_id}
            if name and name != participant_id:
                entry["name"] = name
            entries.append(entry)
        return entries

    def write_thread_registry(self) -> None:
        try:
            write_thread_registry(self.thread_registry, self.thread_registry_path)
        except OSError as exc:
            print(f"Messenger thread registry write failed: {exc}")

    def remember_self_identity(self) -> None:
        changed = self.remember_facebook_user_name(self.client.uid, self.client.name)
        if changed:
            self.write_facebook_name_cache()

    async def ensure_facebook_identity_context(
        self,
        thread_id: object = None,
        user_ids: list[object] | tuple[object, ...] = (),
        timeout: float = 2.5,
    ) -> None:
        if not self.settings.fb_log_names:
            return

        clean_thread_id = self.facebook_id(thread_id)
        clean_user_ids = [
            user_id
            for user_id in (self.facebook_id(value) for value in user_ids)
            if user_id
        ]

        if clean_thread_id and (
            clean_thread_id not in self.fb_thread_names
            or any(user_id not in self.fb_user_names for user_id in clean_user_ids)
        ):
            await self.ensure_facebook_thread_cache(clean_thread_id, timeout=timeout)

        unresolved_user_ids = [
            user_id
            for user_id in clean_user_ids
            if user_id not in self.fb_user_names
        ]
        per_user_timeout = max(0.5, min(1.5, timeout))
        for user_id in unresolved_user_ids[:4]:
            await self.ensure_facebook_user_cache(user_id, timeout=per_user_timeout)

    async def ensure_facebook_thread_cache(
        self,
        thread_id: str,
        timeout: float = 2.5,
    ) -> None:
        task = self.fb_thread_name_tasks.get(thread_id)
        if task is None or task.done():
            task = asyncio.create_task(self.fetch_facebook_thread_names(thread_id))
            self.fb_thread_name_tasks[thread_id] = task

        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return

    async def ensure_facebook_user_cache(
        self,
        user_id: str,
        timeout: float = 1.5,
    ) -> None:
        if user_id in self.fb_user_names:
            return

        task = self.fb_user_name_tasks.get(user_id)
        if task is None or task.done():
            task = asyncio.create_task(self.fetch_facebook_user_name(user_id))
            self.fb_user_name_tasks[user_id] = task

        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return

    async def fetch_facebook_thread_names(self, thread_id: str) -> None:
        try:
            threads = await self.client.fetch_thread_info([thread_id])
        except Exception as exc:
            print(f"Messenger thread name lookup failed for {thread_id}: {exc}")
            return

        changed = False
        for thread in threads:
            participants = getattr(thread, "all_participants", ()) or ()
            self.remember_thread_registry_entry(
                getattr(thread, "thread_id", thread_id),
                getattr(thread, "name", ""),
                getattr(thread, "thread_type", None),
                participants=participants,
            )
            changed |= self.remember_facebook_thread_name(
                getattr(thread, "thread_id", thread_id),
                getattr(thread, "name", ""),
            )
            if not participants:
                changed |= self.remember_facebook_user_name(
                    getattr(thread, "thread_id", ""),
                    getattr(thread, "name", ""),
                )
            for participant in participants:
                changed |= self.remember_facebook_user_name(
                    getattr(participant, "id", ""),
                    getattr(participant, "name", ""),
                )

        if changed:
            self.write_facebook_name_cache()

    async def refresh_thread_registry(self) -> None:
        try:
            limit = int(os.getenv("MURMUR_THREAD_FETCH_LIMIT", "100"))
            threads = await self.client.fetch_thread_list(limit=max(1, limit))
        except Exception as exc:
            print(f"Messenger thread registry refresh failed: {exc}")
            return

        changed_names = False
        for thread in threads:
            thread_id = getattr(thread, "thread_id", "")
            name = getattr(thread, "name", "")
            self.remember_thread_registry_entry(
                thread_id,
                name,
                getattr(thread, "thread_type", None),
                participants=getattr(thread, "all_participants", ()) or (),
            )
            changed_names |= self.remember_facebook_thread_name(thread_id, name)
            for participant in getattr(thread, "all_participants", ()) or ():
                changed_names |= self.remember_facebook_user_name(
                    getattr(participant, "id", ""),
                    getattr(participant, "name", ""),
                )

        if changed_names:
            self.write_facebook_name_cache()

    async def fetch_facebook_user_name(self, user_id: str) -> None:
        try:
            users = await self.client.fetch_user_info(user_id)
        except Exception as exc:
            print(f"Messenger user name lookup failed for {user_id}: {exc}")
            return

        user = users.get(user_id) if isinstance(users, dict) else None
        changed = False
        if user is not None:
            changed = self.remember_facebook_user_name(
                getattr(user, "id", user_id),
                getattr(user, "name", ""),
            )
        if changed:
            self.write_facebook_name_cache()

    def facebook_user_label(self, user_id: object) -> str:
        user_id_text = self.facebook_id(user_id)
        if not user_id_text:
            return "unknown user"
        return self.facebook_label(user_id_text, self.fb_user_names.get(user_id_text))

    def facebook_thread_label(self, thread_id: object) -> str:
        thread_id_text = self.facebook_id(thread_id)
        if not thread_id_text:
            return "unknown thread"
        return self.facebook_label(
            thread_id_text,
            self.fb_thread_names.get(thread_id_text),
        )

    def facebook_label(self, item_id: str, name: str | None) -> str:
        if not name:
            return item_id
        if self.settings.fb_log_names_keep_ids:
            return f"{name} ({item_id})"
        return name

    def facebook_thread_name(self, thread_id: object) -> str:
        thread_id_text = self.facebook_id(thread_id)
        if not thread_id_text:
            return "unknown thread"
        entry = self.thread_registry.get(thread_id_text, {})
        name = self.fb_thread_names.get(thread_id_text) or str(
            entry.get("name") or ""
        ).strip()
        return name or "Messenger thread"

    def facebook_thread_people_names(
        self,
        thread_id: object,
        max_people: int = 12,
    ) -> str:
        thread_id_text = self.facebook_id(thread_id)
        entry = self.thread_registry.get(thread_id_text, {})
        participants = entry.get("participants", [])
        names: list[str] = []
        seen: set[str] = set()

        if isinstance(participants, list):
            for participant in participants:
                if isinstance(participant, dict):
                    participant_id = self.facebook_id(participant.get("id"))
                    name = str(
                        participant.get("name")
                        or self.fb_user_names.get(participant_id)
                        or ""
                    ).strip()
                else:
                    participant_id = self.facebook_id(participant)
                    name = self.fb_user_names.get(participant_id, "")

                label = name or participant_id
                if not label or label in seen:
                    continue
                seen.add(label)
                names.append(label)

        if not names:
            last_sender_name = str(entry.get("last_sender_name") or "").strip()
            last_sender_id = self.facebook_id(entry.get("last_sender_id"))
            fallback = last_sender_name or self.fb_user_names.get(last_sender_id, "")
            if fallback:
                names.append(fallback)

        if not names:
            return ""

        visible = names[:max_people]
        if len(names) > max_people:
            visible.append(f"+{len(names) - max_people} more")
        return ", ".join(visible)

    async def log_facebook_listening_event(self) -> None:
        return

    async def log_facebook_message_event(self, event_data) -> None:
        await self.ensure_facebook_identity_context(
            event_data.thread_id,
            [event_data.sender_id],
        )
        self.client.logger.info(
            f"{self.facebook_user_label(event_data.sender_id)} "
            f"has sent a message to thread {self.facebook_thread_label(event_data.thread_id)}"
        )

    async def log_facebook_typing_event(self, event_data) -> None:
        await self.ensure_facebook_identity_context(
            event_data.thread_id,
            [event_data.sender_id],
        )
        state = "is typing" if event_data.state else "stopped typing"
        self.client.logger.info(
            f"{self.facebook_user_label(event_data.sender_id)} {state} "
            f"in thread {self.facebook_thread_label(event_data.thread_id)}"
        )

    async def log_facebook_seen_event(self, event_data) -> None:
        await self.ensure_facebook_identity_context(
            event_data.thread_id,
            [event_data.user_id],
        )
        self.client.logger.info(
            f"{self.facebook_user_label(event_data.user_id)} has seen messages "
            f"in thread {self.facebook_thread_label(event_data.thread_id)}"
        )

    async def log_facebook_reaction_event(self, event_data) -> None:
        await self.ensure_facebook_identity_context(
            event_data.thread_id,
            [event_data.reactor, event_data.reacted_message_sender],
        )
        if getattr(event_data.reaction_type, "value", 0):
            action = f"removed reaction {event_data.reaction or ''} from"
        else:
            action = f"reacted with {event_data.reaction or ''} to"
        self.client.logger.info(
            f"{self.facebook_user_label(event_data.reactor)} {action} "
            f"the message {event_data.id} in thread {self.facebook_thread_label(event_data.thread_id)}"
        )

    async def log_facebook_unsent_event(self, event_data) -> None:
        await self.ensure_facebook_identity_context(
            event_data.thread_id,
            [event_data.sender_id],
        )
        self.client.logger.info(
            f"{self.facebook_user_label(event_data.sender_id)} unsent the message "
            f"{event_data.id} in thread {self.facebook_thread_label(event_data.thread_id)}"
        )

    async def log_facebook_delivered_event(self, event_data) -> None:
        await self.ensure_facebook_identity_context(
            event_data.thread_id,
            [event_data.user_id],
        )
        self.client.logger.info(
            f"The message {event_data.message_id} is delivered to "
            f"{self.facebook_user_label(event_data.user_id)} in thread "
            f"{self.facebook_thread_label(event_data.thread_id)}"
        )

    async def log_facebook_mark_read_event(self, event_data) -> None:
        for thread_id in event_data.thread_ids:
            await self.ensure_facebook_identity_context(thread_id)
        threads = [
            self.facebook_thread_label(thread_id)
            for thread_id in event_data.thread_ids
        ]
        self.client.logger.info(f"The Thread {threads} marked as Read.")

    def run(self) -> None:
        asyncio.run(self.run_async())

    async def run_async(self) -> None:
        if self.gateway_warmup_enabled():
            try:
                await self.warmup_gateway()
            except Exception as exc:
                print(f"{self.gateway_label()} warmup failed: {exc}")
        self.client._initial_state = await self.preflight_facebook_cookie_login()
        await self.client._runner()

    async def preflight_facebook_cookie_login(self):
        state = None
        try:
            state = await fb_state.State.from_json_cookies(
                self.settings.fb_cookies_path,
                self.settings.fb_user_agent,
                self.settings.fb_proxy,
            )
            who = state.user_name or state.user_id
            print(f"Messenger cookie preflight verified as {who} ({state.user_id}).")
            from fbchat_muqit.muqit import Mqtt

            sequence_id = await Mqtt._fetch_sequence_id(state)
            print(f"Messenger sequence preflight verified as {sequence_id}.")
            return state
        except Exception:
            if state is not None:
                await state.close()
            raise

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

    def use_openwebui(self) -> bool:
        return self.settings.ai_backend == "openwebui"

    def gateway_provider_id(self) -> str:
        return "openwebui" if self.use_openwebui() else "litellm"

    def gateway_label(self) -> str:
        return "Open WebUI" if self.use_openwebui() else "LiteLLM gateway"

    def gateway_default_label(self) -> str:
        return f"{self.gateway_label()} configured default"

    def gateway_warmup_enabled(self) -> bool:
        if self.use_openwebui():
            return self.settings.openwebui_warmup
        return self.settings.litellm_warmup

    def gateway_warmup_chat_enabled(self) -> bool:
        if self.use_openwebui():
            return self.settings.openwebui_warmup_chat
        return self.settings.litellm_warmup_chat

    def default_chat_model(self) -> str:
        return (
            self.settings.openwebui_model
            if self.use_openwebui()
            else self.settings.litellm_model
        )

    def model_aliases(self) -> dict[str, str]:
        return (
            self.settings.openwebui_model_aliases
            if self.use_openwebui()
            else self.settings.litellm_model_aliases
        )

    def preferred_chat_models_env(self) -> str:
        return (
            "OPENWEBUI_PREFERRED_CHAT_MODELS"
            if self.use_openwebui()
            else "LITELLM_PREFERRED_CHAT_MODELS"
        )

    def gateway_model_paths(self) -> tuple[str, ...]:
        if self.use_openwebui():
            return ("/api/models", "/api/v1/models")
        return ("/models",)

    async def warmup_gateway(self) -> None:
        print(f"Warming {self.gateway_label()} before Messenger listener starts...")
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout_seconds)
        ) as session:
            headers = await self.gateway_headers(session)

            for path in self.gateway_model_paths():
                try:
                    async with session.get(
                        self.gateway_url(path),
                        headers=headers,
                    ) as response:
                        if response.status < 400:
                            await response.json(content_type=None)
                            print(f"{self.gateway_label()} model endpoint warmed via {path}.")
                            break
                        body = await response.text()
                        print(f"{self.gateway_label()} model warmup {path} returned {response.status}: {body[:200]}")
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    print(f"{self.gateway_label()} model warmup {path} failed: {exc}")

            if self.gateway_warmup_chat_enabled():
                try:
                    await self.ask_gateway(
                        "__warmup__",
                        "Reply with OK.",
                        self.default_chat_model(),
                    )
                    print(f"{self.gateway_label()} chat completion warmed.")
                except Exception as exc:
                    print(f"{self.gateway_label()} chat warmup failed: {exc}")

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
        self.remember_self_identity()
        print(f"Murmur online as {self.client.name} ({self.client.uid})")
        if self.mqtt_watchdog_task is None or self.mqtt_watchdog_task.done():
            self.mqtt_watchdog_task = asyncio.create_task(self.watch_mqtt_listener())
        if (
            self.thread_registry_refresh_task is None
            or self.thread_registry_refresh_task.done()
        ):
            self.thread_registry_refresh_task = asyncio.create_task(
                self.refresh_thread_registry()
            )
        if (
            self.bnp_notification_worker.enabled
            and (
                self.bnp_notification_task is None
                or self.bnp_notification_task.done()
            )
        ):
            self.bnp_notification_task = asyncio.create_task(
                self.bnp_notification_worker.run()
            )
            self.bnp_notification_task.add_done_callback(
                self.log_bnp_notification_task_error
            )

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
        self.remember_thread_registry_entry(
            message.thread_id,
            thread_type=message.thread_type,
            last_sender_id=message.sender_id,
        )

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

    def log_bnp_notification_task_error(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return

        try:
            task.result()
        except Exception as exc:
            print(f"Unhandled BNP Messenger notification task failed: {exc}")

    async def answer_message(self, message: Message, request: PromptRequest) -> None:
        bot_response = None
        control_response = None
        prompt = request.text
        display_prompt = request.display_text or prompt
        model = self.current_model(message.thread_id)

        try:
            await self.client.typing(message.thread_id, True, message.thread_type)

            if request.is_prefixed:
                chat_command = await self.handle_chat_command(
                    message.thread_id,
                    prompt,
                )
                if chat_command is not None:
                    if chat_command.response is not None:
                        control_response = chat_command.response
                    elif chat_command.prompt is not None:
                        prompt = chat_command.prompt
                        display_prompt = chat_command.prompt

                if chat_command is None:
                    bot_response = await self.handle_media_command(message, prompt)

                if bot_response is None:
                    if control_response is None and chat_command is None:
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
                        display_prompt = prompt

            if bot_response is not None:
                pass
            elif control_response is not None:
                answer = control_response
                bot_response = BotResponse(text=answer)
            else:
                answer = await self.ask_gateway(
                    message.thread_id,
                    prompt,
                    model,
                    display_prompt=display_prompt,
                    source_message_id=message.id,
                    source_sender_id=message.sender_id,
                    source_sender_name=self.facebook_user_label(message.sender_id),
                )
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
        mode, allowed_ids = self.current_thread_allowlist()
        return thread_allowed(thread_id, mode, allowed_ids)

    def current_thread_allowlist(self) -> tuple[str, set[str]]:
        try:
            stat = self.thread_allowlist_path.stat()
            mtime = stat.st_mtime
        except OSError:
            self.thread_allowlist_mtime = None
            if self.settings.allowed_thread_ids:
                return "env_allowlist", set(self.settings.allowed_thread_ids)
            return "allow_all", set()

        if self.thread_allowlist_mtime != mtime:
            mode, allowed_ids = read_thread_allowlist(self.thread_allowlist_path)
            self.thread_allowlist_mode = mode
            self.thread_allowlist_ids = allowed_ids
            self.thread_allowlist_mtime = mtime

        return self.thread_allowlist_mode, set(self.thread_allowlist_ids)

    def get_request(self, message: Message) -> PromptRequest | None:
        text = (message.text or "").strip()
        if not text:
            return None

        prefix = self.settings.bot_prefix
        if prefix and text.lower().startswith(prefix.lower()):
            prompt = text[len(prefix) :].strip()
            prompt = prompt or "Hello"
            return PromptRequest(prompt, is_prefixed=True, display_text=prompt)

        inline_prompt = self.inline_prefix_prompt(text, prefix)
        if inline_prompt is not None:
            return PromptRequest(
                inline_prompt,
                is_prefixed=True,
                display_text=inline_prompt,
            )

        if text.lower() in {"/help", "help"}:
            return PromptRequest("help", is_prefixed=True, display_text=text)

        if self.settings.respond_to_bot_replies and self.is_reply_to_bot(message):
            return PromptRequest(
                self.reply_prompt(message, text),
                is_prefixed=False,
                display_text=text,
            )

        if not self.settings.respond_only_on_prefix:
            return PromptRequest(text, is_prefixed=False, display_text=text)

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
            include_all, free_only, provider_filter, page = self.model_list_args(parts[1:])
            return await self.model_list_message(
                thread_id,
                include_all=include_all,
                free_only=free_only,
                provider_filter=provider_filter,
                page=page,
            )

        if command == "providers":
            return await self.provider_list_message(thread_id)

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

    async def handle_chat_command(
        self,
        thread_id: str,
        prompt: str,
    ) -> ChatCommandResult | None:
        parts = prompt.split(maxsplit=1)
        if not parts or parts[0].lower() not in {"chat", "text"}:
            return None

        if len(parts) == 1 or not parts[1].strip():
            return ChatCommandResult(prompt="Hello")

        rest = parts[1].strip()
        rest_parts = rest.split()
        subcommand = rest_parts[0].lower()

        if subcommand in {"help", "commands", "?"}:
            return ChatCommandResult(response=self.help_message(thread_id))

        if subcommand in {"status", "current"}:
            return ChatCommandResult(response=self.status_message(thread_id))

        if subcommand == "models":
            include_all, free_only, provider_filter, page = self.model_list_args(
                rest_parts[1:]
            )
            return ChatCommandResult(
                response=await self.model_list_message(
                    thread_id,
                    include_all=include_all,
                    free_only=free_only,
                    provider_filter=provider_filter,
                    page=page,
                )
            )

        if subcommand == "providers":
            return ChatCommandResult(
                response=await self.provider_list_message(thread_id)
            )

        if subcommand == "provider":
            if len(rest_parts) == 1:
                provider = self.current_provider(thread_id)
                return ChatCommandResult(
                    response=(
                        f"Current provider: {self.provider_display_name(provider)}\n"
                        f"Use {self.settings.bot_prefix} providers to see options."
                    )
                )
            return ChatCommandResult(
                response=await self.set_thread_provider(
                    thread_id,
                    " ".join(rest_parts[1:]),
                )
            )

        if subcommand in {"model", "use"}:
            if len(rest_parts) == 1:
                alias = self.current_model_alias(thread_id)
                model = self.current_model(thread_id)
                return ChatCommandResult(
                    response=(
                        f"Current chat model: {alias} ({self.model_short_id(model)})\n"
                        f"Use {self.settings.bot_prefix} chat models to see options."
                    )
                )
            return ChatCommandResult(
                response=await self.set_thread_model(
                    thread_id,
                    " ".join(rest_parts[1:]),
                )
            )

        return ChatCommandResult(prompt=rest)

    def model_list_args(self, args: list[str]) -> tuple[bool, bool, str, int]:
        mode = args[0].lower() if args else ""
        free_only = mode == "free"
        include_all = not free_only
        filter_parts = list(args[1:] if free_only else args)
        page = 1
        if filter_parts and filter_parts[-1].isdigit():
            page = max(1, int(filter_parts.pop()))
        provider_filter = " ".join(filter_parts)
        return include_all, free_only, provider_filter, page

    async def handle_media_command(
        self,
        message: Message,
        prompt: str,
    ) -> BotResponse | None:
        parts = prompt.split(maxsplit=1)
        if not parts:
            return None

        command = parts[0].lower()

        if command in {"image", "img", "draw"}:
            if len(parts) == 1 or not parts[1].strip():
                return BotResponse(
                    text=(
                        f"Usage: {self.settings.bot_prefix} image <prompt>\n"
                        f"List image models: {self.settings.bot_prefix} image models\n"
                        f"Or: {self.settings.bot_prefix} image <model number> <prompt>\n"
                        f"Set image model: {self.settings.bot_prefix} image model <number>"
                    )
                )
            image_args = parts[1].strip()
            image_arg_parts = image_args.split(maxsplit=1)
            if image_arg_parts and image_arg_parts[0].lower() == "models":
                include_all, free_only, provider_filter, page = self.model_list_args(
                    image_args.split()[1:]
                )
                return BotResponse(
                    text=await self.image_model_list_message(
                        message.thread_id,
                        include_all=include_all,
                        free_only=free_only,
                        provider_filter=provider_filter,
                        page=page,
                    )
                )

            if image_arg_parts and image_arg_parts[0].lower() in {"model", "use"}:
                if len(image_arg_parts) == 1:
                    return BotResponse(text=self.current_image_model_message(message.thread_id))
                return BotResponse(
                    text=await self.set_thread_image_model(
                        message.thread_id,
                        image_arg_parts[1],
                    )
                )

            image_prompt, image_model, image_selector = await self.extract_image_request(
                message.thread_id,
                image_args,
            )
            if image_model:
                self.set_thread_image_model_value(
                    message.thread_id,
                    image_model,
                    image_selector or image_model,
                )
                if not image_prompt:
                    return BotResponse(text=self.current_image_model_message(message.thread_id))

            return await self.generate_image_response(
                message.thread_id,
                image_prompt,
                source_message_id=message.id,
                source_sender_id=message.sender_id,
                source_sender_name=self.facebook_user_label(message.sender_id),
            )

        if command in {"see", "vision", "look"}:
            return BotResponse(text=self.gateway_only_message("vision"))

        return None

    def gateway_only_message(self, feature: str) -> str:
        return (
            f"{feature.capitalize()} is disabled in Murmur until it can be "
            f"bridged through {self.gateway_label()}. Murmur is bridge-only."
        )

    def user_facing_error(self, exc: Exception) -> str:
        if isinstance(exc, UserVisibleError):
            return str(exc)
        message = str(exc).strip()
        if message.startswith(("LiteLLM gateway ", "Open WebUI ")):
            return message
        return "I hit an error while thinking. Check the bot logs."

    def help_message(self, thread_id: str) -> str:
        prefix = self.settings.bot_prefix
        return "\n".join(
            [
                "Murmur help",
                "",
                "Chat",
                f"{prefix} <message>",
                f"{prefix} model <number|model-id>",
                "",
                "Image",
                f"{prefix} image <prompt>",
                f"{prefix} image models",
                f"{prefix} image model <number|model-id>",
                "",
                "Models",
                f"{prefix} models",
                f"{prefix} models free",
                f"{prefix} providers",
                f"{prefix} status",
            ]
        )

    def status_message(self, thread_id: str) -> str:
        chat_model = self.current_model(thread_id)
        chat_provider = self.provider_for_selected_model(thread_id, chat_model)
        chat_alias = self.current_model_alias(thread_id)
        image_model = self.current_image_model(thread_id)
        image_alias = self.current_image_model_alias(thread_id)
        image_provider = (
            self.provider_for_selected_model(thread_id, image_model)
            if image_model
            else self.gateway_provider_id()
        )
        image_model_label = (
            self.model_short_id(image_model)
            if image_model
            else self.gateway_default_label()
        )
        image_size = self.settings.image_size or self.gateway_default_label()
        return "\n".join(
            [
                "Status",
                f"Bridge: {self.gateway_label()}",
                f"Lobe mirror: {self.lobe_sync.status_label()}",
                f"OpenWebUI route: {'active' if self.use_openwebui() else 'kept, inactive'}",
                "",
                "Chat",
                f"Provider: {self.provider_display_name(chat_provider)}",
                f"Model: {self.model_short_id(chat_model)}",
                f"Selection: {chat_alias}",
                "",
                "Image",
                f"Provider: {self.provider_display_name(image_provider)}",
                f"Model: {image_model_label}",
                f"Selection: {image_alias}",
                f"Size: {image_size}",
            ]
        )

    def image_model_status(self, thread_id: str) -> str:
        model = self.current_image_model(thread_id)
        if not model:
            return self.gateway_default_label()
        provider = self.provider_for_selected_model(thread_id, model)
        size = f", {self.settings.image_size}" if self.settings.image_size else ""
        header = self.response_header(provider, model)
        return f"{header[:-1]}{size}]" if size else header

    def image_model_label(self) -> str:
        return self.settings.image_generation_model or self.gateway_default_label()

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

    async def extract_image_request(
        self,
        thread_id: str,
        prompt: str,
    ) -> tuple[str, str | None, str | None]:
        prompt = prompt.strip()
        parts = prompt.split()
        if len(parts) < 2:
            return prompt, None, None

        options = await self.fetch_model_options(include_all=True)
        if options:
            self.thread_model_options[thread_id] = options
            groups = self.group_model_options(options)
            self.thread_provider_model_options[thread_id] = groups
            self.thread_provider_options[thread_id] = sorted(
                groups,
                key=self.provider_sort_key,
            )

        full_selector_model = self.resolve_provider_model(thread_id, prompt)
        if full_selector_model:
            return "", full_selector_model, prompt

        max_selector_parts = min(len(parts) - 1, 5)
        for selector_length in range(max_selector_parts, 1, -1):
            selector = " ".join(parts[:selector_length])
            model = self.resolve_provider_model(thread_id, selector)
            if model:
                image_prompt = " ".join(parts[selector_length:]).strip()
                if image_prompt:
                    return image_prompt, model, selector

        return prompt, None, None

    def current_model(self, thread_id: str) -> str:
        return self.thread_models.get(thread_id, self.default_chat_model())

    def current_model_alias(self, thread_id: str) -> str:
        return self.thread_model_aliases.get(thread_id, "default")

    def current_image_model(self, thread_id: str) -> str | None:
        return self.thread_image_models.get(
            thread_id,
            self.settings.image_generation_model,
        )

    def current_image_model_alias(self, thread_id: str) -> str:
        return self.thread_image_model_aliases.get(thread_id, "default")

    def current_image_model_message(self, thread_id: str) -> str:
        alias = self.current_image_model_alias(thread_id)
        model = self.current_image_model(thread_id)
        if not model:
            return (
                f"Current image model: {self.gateway_default_label()}\n"
                f"Use {self.settings.bot_prefix} image model <number|model-id>."
            )
        return (
            f"Current image model: {alias} ({self.model_short_id(model)})\n"
            f"Use {self.settings.bot_prefix} image model <number|model-id>."
        )

    def current_provider(self, thread_id: str) -> str:
        return self.thread_providers.get(
            thread_id,
            self.provider_for_model(self.current_model(thread_id)),
        )

    async def resolve_chat_model(self, thread_id: str, model: str) -> str:
        model = model.strip()
        if not model:
            raise UserVisibleError("No chat model is configured.")

        cached = self.resolved_model_cache.get(model)
        if cached:
            return cached

        options = await self.fetch_gateway_models()
        if not options:
            options = self.thread_model_options.get(thread_id, [])

        if options:
            display_options = self.with_configured_models(options)
            self.thread_model_options[thread_id] = display_options
            groups = self.group_model_options(display_options)
            self.thread_provider_model_options[thread_id] = groups
            self.thread_provider_options[thread_id] = sorted(
                groups,
                key=self.provider_sort_key,
            )

        resolved = self.resolve_model_id_from_options(model, options)
        resolved_option = self.model_option_from_options(resolved, options)
        if resolved_option and not self.is_probably_chat_model(resolved_option):
            fallback = self.fallback_chat_model(model, options)
            if fallback:
                print(
                    "Configured chat model "
                    f"{model!r} is not a chat model; using {fallback.id!r}."
                )
                resolved = fallback.id
            else:
                raise UserVisibleError(
                    "Configured model is not a chat model: "
                    f"{self.model_display(resolved_option)}\n"
                    f"Use {self.settings.bot_prefix} model <number|model-id> "
                    "with a chat/text model."
                )

        if options and not self.model_id_in_options(resolved, options):
            fallback = self.fallback_chat_model(model, options)
            if fallback:
                print(
                    "Configured chat model "
                    f"{model!r} was not returned by {self.gateway_label()} model APIs; "
                    f"using {fallback.id!r}."
                )
                resolved = fallback.id
            else:
                raise UserVisibleError(
                    f"Configured chat model is not available in {self.gateway_label()}: "
                    f"{model}\n"
                    f"Use {self.settings.bot_prefix} models to pick a model."
                )

        self.resolved_model_cache[model] = resolved
        return resolved

    def model_id_in_options(self, model_id: str, options: list[ModelOption]) -> bool:
        return self.model_option_from_options(model_id, options) is not None

    def model_option_from_options(
        self,
        model_id: str,
        options: list[ModelOption],
    ) -> ModelOption | None:
        for option in options:
            if self.equivalent_model_id(option.id, model_id):
                return option
        return None

    def fallback_chat_model(
        self,
        requested: str,
        options: list[ModelOption],
    ) -> ModelOption | None:
        provider = self.provider_for_model(requested)
        family = self.provider_family(provider)
        candidates = [
            option
            for option in options
            if family == self.gateway_provider_id()
            or self.provider_family(self.option_provider(option)) == family
        ]
        if not candidates:
            candidates = list(options)

        chat_candidates = [
            option for option in candidates if self.is_probably_chat_model(option)
        ]
        if not chat_candidates:
            chat_candidates = candidates

        preferred_ids = self.preferred_chat_model_ids()

        for pool in (
            [option for option in chat_candidates if self.is_free_model(option)],
            chat_candidates,
        ):
            preferred = self.preferred_chat_model_from_options(pool, preferred_ids)
            if preferred:
                return preferred
            if pool:
                return sorted(pool, key=self.chat_model_sort_key)[0]

        return None

    def preferred_chat_model_ids(self) -> tuple[str, ...]:
        configured = [
            self.default_chat_model(),
            *self.model_aliases().values(),
        ]
        raw = os.getenv(self.preferred_chat_models_env(), "")
        preferred = [
            item.strip()
            for item in raw.replace("\n", ",").split(",")
            if item.strip()
        ]
        return tuple(dict.fromkeys([*configured, *preferred]))

    def preferred_chat_model_from_options(
        self,
        options: list[ModelOption],
        preferred_ids: tuple[str, ...] | None = None,
    ) -> ModelOption | None:
        preferred_ids = preferred_ids or self.preferred_chat_model_ids()
        for preferred_id in preferred_ids:
            preferred_key = preferred_id.strip().lower()
            if not preferred_key:
                continue
            for option in options:
                if self.equivalent_model_id(option.id, preferred_key):
                    return option
                if self.model_short_id(option.id).lower() == self.model_short_id(
                    preferred_key
                ).lower():
                    return option
        return None

    def chat_model_sort_key(self, option: ModelOption) -> tuple[bool, str, str]:
        return (
            not self.is_free_model(option),
            self.model_short_id(option.id).lower(),
            option.id.lower(),
        )

    def is_probably_chat_model(self, option: ModelOption) -> bool:
        capability_set = set(option.capabilities)
        non_chat_capabilities = {
            "image",
            "embeddings",
            "speech",
            "speech-to-text",
            "video",
            "music",
            "classify-image",
            "summarize",
            "classify",
            "detect",
            "translate",
            "voice-activity",
        }
        if capability_set & non_chat_capabilities:
            return False

        text = f"{option.id} {option.name} {option.task or ''}".lower()
        non_chat_terms = (
            "flux",
            "stable-diffusion",
            "sdxl",
            "dall-e",
            "embedding",
            "whisper",
            "text-to-speech",
            "tts",
            "lyria",
            "ocr",
            "indictrans",
            "translate",
            "translation",
            "classification",
            "summarization",
            "object-detection",
            "rerank",
        )
        return not any(term in text for term in non_chat_terms)

    def is_probably_image_model(self, option: ModelOption) -> bool:
        capability_set = set(option.capabilities)
        if "image" in capability_set:
            return True

        text = f"{option.id} {option.name} {option.task or ''}".lower()
        image_terms = (
            "black-forest-labs",
            "dall-e",
            "dalle",
            "flux",
            "gpt-image",
            "image-generation",
            "image_generation",
            "image-preview",
            "imagen",
            "ideogram",
            "kandinsky",
            "kolors",
            "midjourney",
            "playground-v",
            "recraft",
            "sdxl",
            "seedream",
            "stable-diffusion",
            "text-to-image",
        )
        return any(term in text for term in image_terms)

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

        return self.model_aliases().get(key)

    async def set_thread_model(self, thread_id: str, name: str) -> str:
        alias = name.strip().lower().lstrip("@")
        if not self.thread_model_options.get(thread_id):
            options = await self.fetch_model_options(include_all=True)
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
            options = self.thread_model_options.get(thread_id, [])
            resolved = self.resolve_model_id_from_options(alias, options)
            if self.model_id_in_options(resolved, options):
                model = resolved
        if model is None and len(alias.split()) >= 2:
            options = await self.fetch_model_options(include_all=True)
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
                f"Model syntax: {self.settings.bot_prefix} model <number|model-id>"
            )

        option = await self.ensure_thread_model_option(thread_id, model)
        if option and not self.is_probably_chat_model(option):
            return (
                "That model is not a chat model:\n"
                f"{self.model_display(option)}\n"
                f"Use {self.settings.bot_prefix} model <number|model-id> "
                "with a chat/text model."
            )

        if option is None and not self.is_probably_chat_model(
            ModelOption(
                id=model,
                name=self.model_short_id(model),
                provider=self.provider_for_model(model),
            )
        ):
            return (
                "That model does not look like a chat model:\n"
                f"{self.model_short_id(model)}\n"
                f"Use {self.settings.bot_prefix} models to pick a chat/text model."
            )

        self.thread_models[thread_id] = model
        self.thread_model_aliases[thread_id] = self.model_label(thread_id, alias, model)
        self.thread_providers[thread_id] = self.provider_for_selected_model(
            thread_id, model
        )
        return f"Model set to {alias} ({self.model_short_id(model)}) for this thread."

    async def ensure_thread_model_option(
        self,
        thread_id: str,
        model_id: str,
    ) -> ModelOption | None:
        options = self.thread_model_options.get(thread_id)
        if not options:
            options = await self.fetch_model_options(include_all=True)
            self.thread_model_options[thread_id] = options
            groups = self.group_model_options(options)
            self.thread_provider_model_options[thread_id] = groups
            self.thread_provider_options[thread_id] = sorted(
                groups,
                key=self.provider_sort_key,
            )
        return self.model_option_from_options(model_id, options)

    async def set_thread_image_model(self, thread_id: str, name: str) -> str:
        alias = name.strip().lower().lstrip("@")
        unverified = False
        options = self.thread_image_model_options.get(thread_id)
        if not options:
            options = await self.fetch_image_model_options(include_all=True)
            self.thread_image_model_options[thread_id] = options
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
            options = self.thread_image_model_options.get(thread_id, [])
            resolved = self.resolve_model_id_from_options(alias, options)
            if self.model_id_in_options(resolved, options):
                model = resolved
        if model is None and len(alias.split()) >= 2:
            options = await self.fetch_image_model_options(include_all=True)
            self.thread_image_model_options[thread_id] = options
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
            all_options = await self.fetch_model_options(include_all=True)
            resolved = self.resolve_model_id_from_options(alias, all_options)
            option = self.model_option_from_options(resolved, all_options)
            if option:
                model = resolved
                unverified = not self.is_probably_image_model(option)
            elif not alias.isdigit() and self.looks_like_model_id(name):
                model = name.strip()
                unverified = True

        if model is None:
            return (
                f"Unknown image model: {name}\n"
                f"Use {self.settings.bot_prefix} image models to see available image models.\n"
                f"Image model syntax: {self.settings.bot_prefix} image model <number|model-id>"
            )

        self.set_thread_image_model_value(thread_id, model, alias)
        response = (
            f"Image model set to {alias} ({self.model_short_id(model)}) "
            "for this thread."
        )
        if unverified:
            response += (
                f"\n{self.gateway_label()} did not mark this model as image-capable; "
                "generation may fail if the image backend rejects it."
            )
        return response

    def set_thread_image_model_value(
        self,
        thread_id: str,
        model: str,
        requested: str,
    ) -> None:
        self.thread_image_models[thread_id] = model
        self.thread_image_model_aliases[thread_id] = self.model_label(
            thread_id,
            requested.strip().lower().lstrip("@"),
            model,
        )

    def resolve_provider_model(self, thread_id: str, name: str) -> str | None:
        parts = name.split()
        if len(parts) < 2:
            return None

        provider_text = " ".join(parts[:-1])
        model_text = parts[-1]
        groups = self.thread_provider_model_options.get(thread_id, {})
        providers = sorted(groups, key=self.provider_sort_key)
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

    def looks_like_model_id(self, value: str) -> bool:
        value = value.strip()
        return bool(value) and any(marker in value for marker in ("/", ":", ".", "@", "-"))

    def provider_for_selected_model(self, thread_id: str, model_id: str) -> str:
        for provider, options in self.thread_provider_model_options.get(
            thread_id, {}
        ).items():
            if any(self.equivalent_model_id(option.id, model_id) for option in options):
                return provider
        for option in self.thread_model_options.get(thread_id, []):
            if self.equivalent_model_id(option.id, model_id):
                return self.option_provider(option)
        return self.provider_for_model(model_id)

    async def model_list_message(
        self,
        thread_id: str,
        include_all: bool = False,
        free_only: bool = False,
        provider_filter: str = "",
        page: int = 1,
    ) -> str:
        include_all = include_all or not free_only
        options = await self.fetch_model_options(
            include_all=include_all,
            strict_free=free_only,
        )
        compact_connections = False
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
            return self.dynamic_model_list_message(
                thread_id,
                options,
                include_all,
                free_only,
                compact_connections=compact_connections,
                title_override="Free models" if free_only else "Models",
                page=page,
                page_size=self.model_list_page_size(),
                page_command=self.model_list_page_command(
                    image=False,
                    free_only=free_only,
                    provider_filter=provider_filter,
                ),
                footer_lines=[
                    f"Set chat: {self.settings.bot_prefix} model <number|model-id>",
                    f"Image models: {self.settings.bot_prefix} image models",
                    f"Free only: {self.settings.bot_prefix} models free",
                    f"Status: {self.settings.bot_prefix} status",
                ],
            )

        return (
            f"{self.gateway_label()} model endpoint returned no usable models.\n"
            "That alias-only fallback is disabled now because it hides the real problem.\n"
            f"Try again, or check logs/status: {self.settings.bot_prefix} status"
        )

    async def image_model_list_message(
        self,
        thread_id: str,
        include_all: bool = False,
        free_only: bool = False,
        provider_filter: str = "",
        page: int = 1,
    ) -> str:
        include_all = include_all or not free_only
        options = await self.fetch_image_model_options(
            include_all=include_all,
            strict_free=free_only,
        )
        if not options:
            return (
                f"No image-capable models were marked in {self.gateway_label()}'s model list.\n"
                "Murmur looks for image_generation metadata, "
                "image_generation mode, and known image-model IDs.\n"
                f"Set exact model anyway: {self.settings.bot_prefix} image model <model-id>"
            )

        compact_connections = False
        self.thread_image_model_options[thread_id] = options
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
            self.thread_image_model_options[thread_id] = options
            self.thread_model_options[thread_id] = options

        response = self.dynamic_model_list_message(
            thread_id,
            options,
            include_all=include_all,
            free_only=free_only,
            compact_connections=compact_connections,
            title_override="Free image models" if free_only else "Image models",
            page=page,
            page_size=self.model_list_page_size(),
            page_command=self.model_list_page_command(
                image=True,
                free_only=free_only,
                provider_filter=provider_filter,
            ),
            footer_lines=[
                f"Set image: {self.settings.bot_prefix} image model <number|model-id>",
                f"Chat models: {self.settings.bot_prefix} models",
                f"Status: {self.settings.bot_prefix} status",
            ],
        )
        self.thread_image_model_options[thread_id] = list(
            self.thread_model_options.get(thread_id, [])
        )
        return response

    def dynamic_model_list_message(
        self,
        thread_id: str,
        options: list[ModelOption],
        include_all: bool,
        free_only: bool = False,
        compact_connections: bool = False,
        title_override: str | None = None,
        footer_lines: list[str] | None = None,
        max_display: int | None = None,
        page: int = 1,
        page_size: int | None = None,
        page_command: str | None = None,
    ) -> str:
        current_model = self.current_model(thread_id)
        current_image_model = self.current_image_model(thread_id)
        if title_override:
            title = title_override
        elif free_only:
            title = "Free models"
        else:
            title = "Models"
        groups = self.group_model_options(options)
        connection_counts = self.model_group_connection_counts(groups)
        if compact_connections:
            groups = self.compact_model_groups_by_family(thread_id, groups)
        total_display_count = sum(
            len(provider_options) for provider_options in groups.values()
        )
        all_groups = groups
        all_display_options = self.flatten_model_groups(all_groups)
        display_start = 0
        total_pages = 1
        if page_size is not None and page_size > 0 and total_display_count > page_size:
            total_pages = max(1, (total_display_count + page_size - 1) // page_size)
            page = min(max(1, page), total_pages)
            display_start = (page - 1) * page_size
            groups = self.slice_model_groups(groups, display_start, page_size)
        elif max_display is not None and max_display > 0:
            groups = self.limit_model_groups(groups, max_display)
        display_options = self.flatten_model_groups(groups)
        self.thread_model_options[thread_id] = all_display_options
        self.thread_provider_model_options[thread_id] = all_groups
        self.thread_provider_options[thread_id] = sorted(
            all_groups,
            key=self.provider_sort_key,
        )
        display_count = len(display_options)
        count_label = (
            f"{display_start + 1}-{display_start + display_count} of {total_display_count}"
            if display_count < total_display_count
            else str(display_count)
        )
        lines = [f"{title} ({count_label}):"]

        display_index = display_start + 1
        for provider, provider_options in groups.items():
            lines.append("")
            lines.append(f"[{self.model_group_header(provider, connection_counts)}]")
            for option in provider_options:
                current_labels = []
                if self.equivalent_model_id(option.id, current_model):
                    current_labels.append("chat current")
                if current_image_model and self.equivalent_model_id(
                    option.id,
                    current_image_model,
                ):
                    current_labels.append("image current")
                current = f" ({', '.join(current_labels)})" if current_labels else ""
                lines.append(f"{display_index}. {self.model_display(option)}{current}")
                display_index += 1

        lines.append("")
        if footer_lines is None:
            footer_lines = [
                f"Chat: {self.settings.bot_prefix} model <number|model-id>",
                f"Image: {self.settings.bot_prefix} image model <number|model-id>",
                f"Free only: {self.settings.bot_prefix} models free",
                f"Status: {self.settings.bot_prefix} status",
            ]
        if total_pages > 1 and page_command:
            lines.append(f"Page {page}/{total_pages}")
            if page < total_pages:
                lines.append(f"Next: {page_command} {page + 1}")
            if page > 1:
                lines.append(f"Prev: {page_command} {page - 1}")
        lines.extend(footer_lines)
        return "\n".join(lines)

    def model_list_page_size(self) -> int:
        raw = os.getenv("MODEL_LIST_PAGE_SIZE", "25")
        try:
            return max(5, min(int(raw), 100))
        except ValueError:
            return 25

    def model_list_page_command(
        self,
        *,
        image: bool,
        free_only: bool,
        provider_filter: str,
    ) -> str:
        parts = [self.settings.bot_prefix]
        if image:
            parts.extend(["image", "models"])
        else:
            parts.append("models")
        if free_only:
            parts.append("free")
        if provider_filter.strip():
            parts.append(provider_filter.strip())
        return " ".join(parts)

    def limit_model_groups(
        self,
        groups: dict[str, list[ModelOption]],
        max_display: int,
    ) -> dict[str, list[ModelOption]]:
        limited: dict[str, list[ModelOption]] = {}
        remaining = max_display
        for provider, provider_options in groups.items():
            if remaining <= 0:
                break
            kept = provider_options[:remaining]
            if kept:
                limited[provider] = kept
                remaining -= len(kept)
        return limited

    def slice_model_groups(
        self,
        groups: dict[str, list[ModelOption]],
        start: int,
        limit: int,
    ) -> dict[str, list[ModelOption]]:
        sliced: dict[str, list[ModelOption]] = {}
        end = start + limit
        index = 0
        for provider, provider_options in groups.items():
            provider_start = index
            provider_end = index + len(provider_options)
            index = provider_end
            if provider_end <= start:
                continue
            if provider_start >= end:
                break
            local_start = max(0, start - provider_start)
            local_end = min(len(provider_options), end - provider_start)
            kept = provider_options[local_start:local_end]
            if kept:
                sliced[provider] = kept
        return sliced

    def flatten_model_groups(
        self,
        groups: dict[str, list[ModelOption]],
    ) -> list[ModelOption]:
        return [
            option
            for provider_options in groups.values()
            for option in provider_options
        ]

    def model_group_connection_counts(
        self,
        groups: dict[str, list[ModelOption]],
    ) -> dict[str, int]:
        families: dict[str, set[str]] = defaultdict(set)
        counts: dict[str, int] = {}
        for provider in groups:
            family = self.provider_family(provider)
            families[family].add(provider)
            counts[provider] = 1

        for family, providers in families.items():
            counts[family] = max(1, len(providers))

        return counts

    def model_group_header(
        self,
        provider: str,
        connection_counts: dict[str, int],
    ) -> str:
        count = connection_counts.get(provider, 1)
        noun = "connection" if count == 1 else "connections"
        if self.provider_family(provider) == "litellm":
            return self.provider_display_name(provider)
        return f"{self.provider_display_name(provider)} - {count} {noun}"

    def compact_model_groups_by_family(
        self,
        thread_id: str,
        groups: dict[str, list[ModelOption]],
    ) -> dict[str, list[ModelOption]]:
        current_provider = self.provider_for_selected_model(
            thread_id,
            self.current_model(thread_id),
        )
        current_image_model = self.current_image_model(thread_id)
        current_image_provider = (
            self.provider_for_selected_model(thread_id, current_image_model)
            if current_image_model
            else ""
        )

        families: dict[str, list[str]] = defaultdict(list)
        for provider in groups:
            families[self.provider_family(provider)].append(provider)

        compact: dict[str, list[ModelOption]] = {}
        for family in sorted(families, key=self.provider_sort_key):
            providers = sorted(families[family], key=self.provider_sort_key)
            representative = self.representative_provider_for_family(
                providers,
                current_provider,
                current_image_provider,
            )
            by_model: dict[str, ModelOption] = {}
            for provider in providers:
                for option in groups.get(provider, []):
                    key = self.model_short_id(option.id).lower()
                    current = by_model.get(key)
                    if current is None or self.option_provider(option) == representative:
                        by_model[key] = option

            compact[family] = sorted(
                by_model.values(),
                key=lambda option: self.model_short_id(option.id).lower(),
            )

        return dict(
            sorted(compact.items(), key=lambda item: self.provider_sort_key(item[0]))
        )

    def representative_provider_for_family(
        self,
        providers: list[str],
        current_provider: str,
        current_image_provider: str,
    ) -> str:
        for provider in (current_provider, current_image_provider):
            if provider in providers:
                return provider
        for provider in providers:
            if provider.endswith("_1"):
                return provider
        return providers[0]

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
        capabilities = self.model_capability_suffix(option)
        if name and name != model_id:
            return f"{model_id}{capabilities} ({name})"
        return f"{model_id}{capabilities}"

    def model_capability_suffix(self, option: ModelOption) -> str:
        if not option.capabilities:
            return ""
        return " [" + ", ".join(option.capabilities) + "]"

    def model_short_id(self, model_id: str) -> str:
        if "." in model_id:
            prefix, suffix = model_id.split(".", 1)
            if self.is_provider_connection(prefix):
                return suffix
        return model_id

    async def provider_list_message(self, thread_id: str) -> str:
        all_options = await self.fetch_model_options(include_all=True)
        if not all_options:
            return (
                f"No providers returned from the {self.gateway_label()} model API.\n"
                f"Exact source: {self.gateway_label()} model endpoints returned no usable models."
            )

        all_grouped = self.group_model_options(all_options)
        self.thread_model_options[thread_id] = all_options

        provider_keys = sorted(all_grouped, key=self.provider_sort_key)
        self.thread_provider_options[thread_id] = provider_keys
        self.thread_provider_model_options[thread_id] = all_grouped
        current_provider = self.current_provider(thread_id)
        lines = [f"Providers ({len(provider_keys)}):"]
        for index, provider in enumerate(provider_keys, start=1):
            models = all_grouped.get(provider, [])
            marker = " (current)" if provider == current_provider else ""
            sample = ", ".join(self.model_short_id(model.id) for model in models[:3])
            suffix = f" - {sample}" if sample else ""
            count = f"{len(models)} model(s)"
            lines.append(
                f"{index}. {self.provider_display_name(provider)}"
                f" - {count}{marker}{suffix}"
            )

        lines.append("")
        lines.append(f"Pick model: {self.settings.bot_prefix} model <number|model-id>")
        lines.append(f"Models: {self.settings.bot_prefix} models")
        return "\n".join(lines)

    async def set_thread_provider(self, thread_id: str, name: str) -> str:
        options = self.thread_model_options.get(thread_id)
        providers = self.thread_provider_options.get(thread_id)
        if not options or not providers:
            options = await self.fetch_model_options(include_all=True)
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
                f"Use {self.settings.bot_prefix} providers to refresh provider data."
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

    async def fetch_model_options(
        self,
        include_all: bool = False,
        strict_free: bool = False,
    ) -> list[ModelOption]:
        models = await self.fetch_gateway_models()

        free_models = [model for model in models if self.is_free_model(model)]
        if strict_free:
            return sorted(free_models, key=lambda model: model.id)

        return sorted(models, key=lambda model: model.id)

    async def fetch_image_model_options(
        self,
        include_all: bool = False,
        strict_free: bool = False,
    ) -> list[ModelOption]:
        models = await self.fetch_model_options(
            include_all=include_all,
            strict_free=strict_free,
        )
        image_models = [model for model in models if self.is_probably_image_model(model)]
        configured_model = self.configured_image_model_option()
        if configured_model and (not strict_free or configured_model.is_free):
            image_models.insert(0, configured_model)

        return sorted(
            self.dedupe_model_options(image_models),
            key=lambda model: model.id,
        )

    def configured_image_model_option(self) -> ModelOption | None:
        model = (self.settings.image_generation_model or "").strip()
        if not model:
            return None

        return ModelOption(
            id=model,
            name=model,
            provider=self.provider_for_model(model),
            is_free=self.is_free_model_id(model, model),
            task="image-generation",
            capabilities=("image",),
        )

    def with_configured_models(self, models: list[ModelOption]) -> list[ModelOption]:
        return self.dedupe_model_options(models)

    def equivalent_model_id(self, left: str | None, right: str | None) -> bool:
        if not left or not right:
            return False
        left_key = left.strip().lower()
        right_key = right.strip().lower()
        if left_key == right_key:
            return True

        left_provider = self.provider_connection_prefix(left_key)
        right_provider = self.provider_connection_prefix(right_key)
        if left_provider and right_provider:
            return False
        if left_provider or right_provider:
            return self.model_short_id(left_key) == self.model_short_id(right_key)
        return False

    def provider_connection_prefix(self, model_id: str) -> str | None:
        if "." not in model_id:
            return None
        prefix = model_id.split(".", 1)[0]
        return prefix if self.is_provider_connection(prefix) else None

    async def fetch_gateway_models(self) -> list[ModelOption]:
        if self.use_openwebui():
            return await self.fetch_openwebui_models()
        return await self.fetch_litellm_models()

    async def fetch_litellm_models(self) -> list[ModelOption]:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout_seconds)
        ) as session:
            for path in ("/models",):
                try:
                    async with session.get(
                        self.litellm_url(path),
                        headers=await self.litellm_headers(),
                    ) as response:
                        if response.status >= 400:
                            body = await response.text()
                            print(
                                f"{self.gateway_label()} model fetch {path} "
                                f"returned {response.status}: {body[:200]}"
                            )
                            continue
                        body = await response.json(content_type=None)
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    print(
                        f"{self.gateway_label()} model fetch {path} failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue

                models = self.parse_models_response(body, forced_provider="litellm")
                if models:
                    return self.dedupe_model_options(models)
                print(f"{self.gateway_label()} model fetch {path} returned no usable models.")

        return []

    async def fetch_openwebui_models(self) -> list[ModelOption]:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout_seconds)
        ) as session:
            (
                provider_by_url_idx,
                configured_model_ids,
            ) = await self.fetch_openwebui_provider_metadata(session)
            connection_models = self.configured_connection_models(
                provider_by_url_idx,
                configured_model_ids,
            )
            connection_models.extend(
                await self.fetch_openwebui_connection_models(
                    session,
                    provider_by_url_idx,
                    skip_url_idxs=set(configured_model_ids),
                )
            )

            api_models: list[ModelOption] = []
            for path in self.gateway_model_paths():
                try:
                    async with session.get(
                        self.openwebui_url(path),
                        headers=await self.openwebui_headers(session),
                    ) as response:
                        if response.status >= 400:
                            continue
                        body = await response.json(content_type=None)
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    continue

                api_models = self.parse_models_response(body, provider_by_url_idx)
                if api_models:
                    break

        connection_models = self.dedupe_model_options(connection_models)
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

    def configured_connection_models(
        self,
        provider_by_url_idx: dict[str, str],
        configured_model_ids: dict[str, list[str]],
    ) -> list[ModelOption]:
        models: list[ModelOption] = []
        for url_idx, model_ids in configured_model_ids.items():
            provider = provider_by_url_idx.get(url_idx)
            if not provider:
                continue
            for model_id in model_ids:
                model_name = self.model_short_id(model_id)
                models.append(
                    ModelOption(
                        id=self.provider_model_id(provider, model_id),
                        name=model_name,
                        provider=provider,
                        is_free=self.is_free_model_id(model_id, model_name),
                        task=self.configured_model_task(model_id),
                        capabilities=self.configured_model_capabilities(model_id),
                    )
                )
        return self.dedupe_model_options(models)

    async def fetch_openwebui_connection_models(
        self,
        session: aiohttp.ClientSession,
        provider_by_url_idx: dict[str, str],
        skip_url_idxs: set[str] | None = None,
    ) -> list[ModelOption]:
        skip_url_idxs = skip_url_idxs or set()
        models: list[ModelOption] = []
        for url_idx, provider in sorted(
            provider_by_url_idx.items(),
            key=lambda item: int(item[0]) if item[0].isdigit() else item[0],
        ):
            if url_idx in skip_url_idxs:
                continue
            try:
                async with session.get(
                    self.openwebui_url(f"/openai/models/{url_idx}"),
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
                        task=option.task,
                        capabilities=option.capabilities,
                    )
                )
        return self.dedupe_model_options(models)

    async def fetch_openwebui_provider_metadata(
        self, session: aiohttp.ClientSession
    ) -> tuple[dict[str, str], dict[str, list[str]]]:
        try:
            async with session.get(
                self.openwebui_url("/openai/config"),
                headers=await self.openwebui_headers(session),
            ) as response:
                if response.status >= 400:
                    return {}, {}
                body = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, TypeError, ValueError):
            return {}, {}

        if not isinstance(body, dict):
            return {}, {}

        urls = body.get("OPENAI_API_BASE_URLS") or []
        configs = body.get("OPENAI_API_CONFIGS") or {}
        if not isinstance(urls, list) or not isinstance(configs, dict):
            return {}, {}

        provider_by_url_idx: dict[str, str] = {}
        configured_model_ids: dict[str, list[str]] = {}
        for index, url in enumerate(urls):
            url_text = str(url)
            config = configs.get(str(index), configs.get(url_text, {}))
            if not isinstance(config, dict):
                config = {}

            prefix_id = config.get("prefix_id")
            if isinstance(prefix_id, str) and prefix_id.strip():
                provider_by_url_idx[str(index)] = self.canonical_provider_id(prefix_id)
            else:
                provider_by_url_idx[str(index)] = self.provider_for_base_url(url_text)

            model_ids = config.get("model_ids")
            if isinstance(model_ids, list):
                configured_model_ids[str(index)] = [
                    str(model_id).strip()
                    for model_id in model_ids
                    if str(model_id).strip()
                ]

        return provider_by_url_idx, configured_model_ids

    def dedupe_model_options(self, options: list[ModelOption]) -> list[ModelOption]:
        deduped: dict[str, ModelOption] = {}
        for option in options:
            if not self.is_usable_model_option(option):
                continue
            deduped.setdefault(option.id, option)
        return sorted(deduped.values(), key=lambda model: model.id)

    def is_usable_model_option(self, option: ModelOption) -> bool:
        model_id = self.model_short_id(option.id).strip().lower()
        return model_id not in {"arena-model"}

    def parse_models_response(
        self,
        body: object,
        provider_by_url_idx: dict[str, str] | None = None,
        forced_provider: str | None = None,
    ) -> list[ModelOption]:
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
                        provider=forced_provider or self.gateway_provider_id(),
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
            provider = forced_provider or self.provider_from_raw_model(
                raw_model,
                str(model_id),
                provider_by_url_idx or {},
            )
            models.append(
                ModelOption(
                    id=str(model_id),
                    name=str(name),
                    provider=provider,
                    is_free=self.is_free_raw_model(raw_model, str(model_id), str(name)),
                    pricing=(
                        raw_model.get("pricing")
                        if isinstance(raw_model.get("pricing"), dict)
                        else None
                    ),
                    task=self.raw_model_task(raw_model),
                    capabilities=self.raw_model_capabilities(raw_model),
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
        if "pollinations.ai" in hostname:
            return "pollinations"
        if hostname:
            return hostname.removeprefix("api.").split(".", 1)[0]
        return self.gateway_provider_id()

    def configured_model_task(self, model_id: str) -> str | None:
        model_id = model_id.lower()
        if self.is_probably_image_model(
            ModelOption(id=model_id, name=model_id, provider=self.gateway_provider_id())
        ):
            return "image"
        return None

    def configured_model_capabilities(self, model_id: str) -> tuple[str, ...]:
        task = self.configured_model_task(model_id)
        return tuple(self.capabilities_from_task(task))

    def raw_model_task(self, raw_model: dict) -> str | None:
        for mapping in self.raw_model_metadata_dicts(raw_model):
            task = mapping.get("task")
            if isinstance(task, dict):
                name = task.get("name")
                return str(name).strip() if name else None
            if isinstance(task, str) and task.strip():
                return task.strip()
            for key in ("task_name", "type", "mode", "sample_spec"):
                value = mapping.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def raw_model_capabilities(self, raw_model: dict) -> tuple[str, ...]:
        task = self.raw_model_task(raw_model)
        capabilities = self.capabilities_from_task(task)

        for mapping in self.raw_model_metadata_dicts(raw_model):
            raw_capabilities = mapping.get("capabilities")
            if isinstance(raw_capabilities, list):
                for capability in raw_capabilities:
                    label = self.normalize_capability_label(str(capability))
                    if label:
                        capabilities.append(label)
            elif isinstance(raw_capabilities, dict):
                for capability, enabled in raw_capabilities.items():
                    if not self.capability_value_enabled(enabled):
                        continue
                    label = self.normalize_capability_label(str(capability))
                    if label:
                        capabilities.append(label)

            for key in (
                "image_generation",
                "supports_image_generation",
                "supports_image_output",
            ):
                if self.capability_value_enabled(mapping.get(key)):
                    capabilities.append("image")

            output_modalities = mapping.get("output_modalities")
            if isinstance(output_modalities, list):
                if any(str(value).lower() == "image" for value in output_modalities):
                    capabilities.append("image")

            supported_parameters = mapping.get("supported_parameters")
            if isinstance(supported_parameters, list):
                for parameter in supported_parameters:
                    label = self.capability_from_parameter(str(parameter))
                    if label:
                        capabilities.append(label)

        return tuple(dict.fromkeys(capabilities))

    def raw_model_metadata_dicts(self, raw_model: dict) -> list[dict]:
        seen: set[int] = set()
        result: list[dict] = []

        def visit(value: object, depth: int = 0) -> None:
            if depth > 4 or not isinstance(value, dict):
                return
            identity = id(value)
            if identity in seen:
                return
            seen.add(identity)
            result.append(value)
            for key in ("info", "meta", "model_info"):
                visit(value.get(key), depth + 1)

        visit(raw_model)
        return result

    def capability_value_enabled(self, value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return False

    def capabilities_from_task(self, task: str | None) -> list[str]:
        if not task:
            return []
        normalized = self.normalize_provider_key(task)
        task_map = {
            "textgeneration": "text",
            "texttoimage": "image",
            "imagegeneration": "image",
            "imagetotext": "vision",
            "textembeddings": "embeddings",
            "automaticspeechrecognition": "speech-to-text",
            "texttospeech": "speech",
            "texttovideo": "video",
            "musicgeneration": "music",
            "summarization": "summarize",
            "textclassification": "classify",
            "objectdetection": "detect",
            "translation": "translate",
            "imageclassification": "classify-image",
            "voiceactivitydetection": "voice-activity",
        }
        label = task_map.get(normalized)
        return [label] if label else []

    def capability_from_parameter(self, parameter: str) -> str | None:
        normalized = self.normalize_provider_key(parameter)
        if normalized in {"tools", "toolchoice", "functioncalling"}:
            return "function-calling"
        if normalized in {"responseformat", "structuredoutputs"}:
            return "structured-output"
        return None

    def normalize_capability_label(self, capability: str) -> str | None:
        normalized = self.normalize_provider_key(capability)
        capability_map = {
            "functioncalling": "function-calling",
            "structuredoutputs": "structured-output",
            "responseformat": "structured-output",
            "reasoning": "reasoning",
            "vision": "vision",
            "image": "image",
            "imagegeneration": "image",
            "generatesimages": "image",
            "texttoimage": "image",
            "fileupload": "file-upload",
            "websearch": "web-search",
            "codeinterpreter": "code-interpreter",
            "citations": "citations",
            "statusupdates": "status-updates",
            "usage": "usage",
            "lora": "lora",
            "batch": "batch",
            "realtime": "real-time",
            "asyncqueue": "async",
        }
        return capability_map.get(normalized)

    def provider_for_model(self, model_id: str) -> str:
        model_id = model_id.strip()
        if not self.use_openwebui():
            return "litellm"
        if model_id.startswith("@cf/"):
            return "cloudflare"
        if model_id.startswith("openrouter/"):
            return "openrouter"
        if model_id.startswith("pollinations/"):
            return "pollinations"
        if "." in model_id:
            prefix = model_id.split(".", 1)[0].strip().lower()
            if self.is_provider_connection(prefix):
                return self.canonical_provider_id(prefix)
        return "openwebui"

    def option_provider(self, option: ModelOption) -> str:
        return option.provider or self.provider_for_model(option.id)

    def provider_display_name(self, provider: str) -> str:
        provider = self.canonical_provider_id(provider)
        if provider == "litellm":
            return "LiteLLM gateway"
        if provider == "openwebui":
            return "Open WebUI"
        return provider.replace("_", " ")

    def normalize_provider_key(self, provider: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", provider.lower())

    def canonical_provider_id(self, provider: str) -> str:
        provider = provider.strip().lower() or self.gateway_provider_id()
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
            model_id.endswith(":free")
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

    def openwebui_chat_metadata(self, thread_id: str) -> dict[str, str]:
        digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:24]
        message_id = f"murmur-{digest}-{time.time_ns()}"
        return {
            "chat_id": f"local:murmur:{digest}",
            "id": message_id,
        }

    async def ask_gateway(
        self,
        thread_id: str,
        prompt: str,
        model: str,
        display_prompt: str | None = None,
        source_message_id: str | None = None,
        source_sender_id: str | None = None,
        source_sender_name: str | None = None,
    ) -> str:
        model = await self.resolve_chat_model(thread_id, model)
        chat_prompt = display_prompt or prompt
        messages = [{"role": "system", "content": self.settings.system_prompt}]
        messages.extend(self.history[thread_id])
        messages.append({"role": "user", "content": prompt})

        tried_models: set[str] = set()
        errors: list[str] = []
        while True:
            tried_models.add(model)
            payload = {
                "model": model,
                "messages": messages,
                "stream": False,
            }
            if self.use_openwebui():
                payload.update(self.openwebui_chat_metadata(thread_id))
            try:
                answer = await self.request_chat_completion(payload)
                break
            except GatewayResponseError as exc:
                errors.append(str(exc))
                retry_model = await self.next_chat_retry_model(
                    thread_id,
                    model,
                    tried_models,
                )
                if not exc.retryable or retry_model is None:
                    raise UserVisibleError("\n\n".join(errors)) from exc

                print(
                    f"{self.gateway_label()} provider error for "
                    f"{model!r}; retrying with {retry_model!r}."
                )
                model = retry_model

        self.history[thread_id].append({"role": "user", "content": chat_prompt})
        self.history[thread_id].append({"role": "assistant", "content": answer})
        provider = self.provider_for_selected_model(thread_id, model)
        await self.sync_lobe_chat(
            thread_id=thread_id,
            user_prompt=chat_prompt,
            assistant_answer=answer,
            source_message_id=source_message_id,
            source_sender_id=source_sender_id,
            source_sender_name=source_sender_name,
            provider=provider,
            model=model,
        )
        return f"{self.response_header(provider, model)}\n{answer}"

    async def sync_lobe_chat(
        self,
        *,
        thread_id: str,
        user_prompt: str,
        assistant_answer: str,
        source_message_id: str | None,
        source_sender_id: str | None,
        source_sender_name: str | None,
        provider: str,
        model: str,
    ) -> None:
        if not self.lobe_sync.enabled:
            return

        try:
            thread_name = self.lobe_thread_name(thread_id)
            await self.lobe_sync.sync_exchange(
                LobeChatExchange(
                    thread_id=thread_id,
                    thread_name=thread_name,
                    topic_title=self.lobe_topic_title(thread_name),
                    user_prompt=user_prompt,
                    assistant_answer=assistant_answer,
                    source_message_id=source_message_id,
                    sender_id=source_sender_id,
                    sender_name=source_sender_name,
                    provider=self.provider_display_name(provider),
                    model=self.model_short_id(model),
                    gateway=self.gateway_provider_id(),
                )
            )
        except Exception as exc:
            print(f"Lobe sync failed for Messenger thread {thread_id}: {exc}")

    def lobe_thread_name(self, thread_id: str) -> str:
        name = self.facebook_thread_name(thread_id).strip()
        if name and name != "Messenger thread":
            return self.compact_lobe_title(name)

        people = self.facebook_thread_people_names(thread_id)
        if people and people != "unknown":
            return self.compact_lobe_title(people)

        return f"thread {thread_id}"

    def lobe_topic_title(self, thread_name: str) -> str:
        prefix = self.settings.lobe_topic_prefix.strip()
        title = f"{thread_name} | {prefix}" if prefix else thread_name
        return self.compact_lobe_title(title, max_length=120)

    @staticmethod
    def compact_lobe_title(value: str, max_length: int = 80) -> str:
        title = re.sub(r"\s+", " ", value).strip()
        if len(title) <= max_length:
            return title
        return title[: max_length - 3].rstrip() + "..."

    async def next_chat_retry_model(
        self,
        thread_id: str,
        failed_model: str,
        tried_models: set[str],
    ) -> str | None:
        options = self.thread_model_options.get(thread_id)
        if not options:
            options = self.with_configured_models(await self.fetch_gateway_models())
            self.thread_model_options[thread_id] = options
            groups = self.group_model_options(options)
            self.thread_provider_model_options[thread_id] = groups
            self.thread_provider_options[thread_id] = sorted(
                groups,
                key=self.provider_sort_key,
            )

        failed_short = self.model_short_id(failed_model).lower()
        failed_family = self.provider_family(self.provider_for_model(failed_model))
        candidates = [
            option
            for option in options
            if self.model_short_id(option.id).lower() == failed_short
            and option.id not in tried_models
            and self.is_probably_chat_model(option)
            and self.provider_family(self.option_provider(option)) == failed_family
        ]
        if not candidates:
            candidates = [
                option
                for option in options
                if option.id not in tried_models
                and self.is_probably_chat_model(option)
                and self.provider_family(self.option_provider(option)) == failed_family
                and self.is_free_model(option)
            ]

        if not candidates:
            return None

        preferred = self.preferred_chat_model_from_options(candidates)
        if preferred:
            return preferred.id

        return sorted(candidates, key=self.chat_model_sort_key)[0].id

    async def generate_image_response(
        self,
        thread_id: str,
        prompt: str,
        source_message_id: str | None = None,
        source_sender_id: str | None = None,
        source_sender_name: str | None = None,
    ) -> BotResponse:
        model = self.current_image_model(thread_id)
        paths = await self.request_image_generation(prompt, model)
        model_label = model or self.image_model_label()
        provider = self.provider_for_selected_model(thread_id, model_label)
        size = f", {self.settings.image_size}" if self.settings.image_size else ""
        header = self.response_header(provider, model_label)
        if size:
            header = f"{header[:-1]}{size}]"
        await self.sync_lobe_image(
            thread_id=thread_id,
            user_prompt=f"image {prompt}",
            assistant_answer=header,
            image_paths=paths,
            source_message_id=source_message_id,
            source_sender_id=source_sender_id,
            source_sender_name=source_sender_name,
            provider=provider,
            model=model_label,
        )
        return BotResponse(
            text=header,
            file_paths=paths,
            cleanup_paths=paths,
        )

    async def sync_lobe_image(
        self,
        *,
        thread_id: str,
        user_prompt: str,
        assistant_answer: str,
        image_paths: list[str],
        source_message_id: str | None,
        source_sender_id: str | None,
        source_sender_name: str | None,
        provider: str,
        model: str,
    ) -> None:
        if not self.lobe_sync.enabled:
            return

        try:
            attachments = await asyncio.to_thread(
                self.lobe_image_attachments,
                image_paths,
            )
            thread_name = self.lobe_thread_name(thread_id)
            await self.lobe_sync.sync_exchange(
                LobeChatExchange(
                    thread_id=thread_id,
                    thread_name=thread_name,
                    topic_title=self.lobe_topic_title(thread_name),
                    user_prompt=user_prompt,
                    assistant_answer=assistant_answer,
                    source_message_id=source_message_id,
                    sender_id=source_sender_id,
                    sender_name=source_sender_name,
                    provider=self.provider_display_name(provider),
                    model=self.model_short_id(model),
                    gateway=self.gateway_provider_id(),
                    assistant_files=tuple(attachments),
                )
            )
        except Exception as exc:
            print(f"Lobe image sync failed for Messenger thread {thread_id}: {exc}")

    def lobe_image_attachments(self, image_paths: list[str]) -> list[LobeFileAttachment]:
        attachments: list[LobeFileAttachment] = []
        for image_path in image_paths:
            path = Path(image_path)
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()[:12]
            content_type = self.image_content_type(path, content)
            suffix = ".jpg" if content_type == "image/jpeg" else ".png"
            attachments.append(
                LobeFileAttachment(
                    name=f"murmur-image-{digest}{suffix}",
                    content_type=content_type,
                    content=content,
                )
            )
        return attachments

    def image_content_type(self, path: Path, content: bytes) -> str:
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        guessed, _ = mimetypes.guess_type(path.name)
        if guessed and guessed.startswith("image/"):
            return guessed
        return "image/png"

    async def request_image_generation(
        self,
        prompt: str,
        model: str | None = None,
    ) -> list[str]:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout_seconds)
        ) as session:
            body, status = await self.post_image_generation(session, prompt, model)
            if self.use_openwebui() and status == 401 and not self.settings.openwebui_api_key:
                self.openwebui_token = None
                body, status = await self.post_image_generation(session, prompt, model)
            if status >= 400:
                raise UserVisibleError(self.image_error_message(prompt, status, body))

            image_refs = self.parse_image_generation_response(body)
            if not image_refs:
                raise RuntimeError(f"Unexpected {self.gateway_label()} image response: {body}")

            paths = []
            for image_ref in image_refs:
                paths.append(await self.materialize_generated_image(session, image_ref))
            return paths

    async def post_image_generation(
        self,
        session: aiohttp.ClientSession,
        prompt: str,
        model: str | None = None,
    ) -> tuple[object, int]:
        payload: dict[str, object] = {"prompt": prompt, "n": 1}
        model = model or self.settings.image_generation_model
        if model:
            payload["model"] = self.model_short_id(model)
        if self.settings.image_size:
            payload["size"] = self.settings.image_size
        if self.settings.image_steps is not None:
            payload["steps"] = self.settings.image_steps

        async with session.post(
            self.image_generation_url(),
            headers=await self.gateway_headers(session),
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
        if self.is_gateway_provider_auth_error(status, body):
            return (
                "Image generation failed.\n"
                f"{self.gateway_label()}'s image provider returned Unauthorized. "
                f"The Murmur request reached {self.gateway_label()}, but the configured image "
                f"backend/API key inside {self.gateway_label()} rejected it.\n"
                f"{self.gateway_label()} image error {status}: {body}"
            )
        return f"Image generation failed.\n{self.gateway_label()} image error {status}: {body}"

    def is_gateway_provider_auth_error(self, status: int, body: object) -> bool:
        if status != 400:
            return False
        text = str(body).lower()
        return any(
            marker in text
            for marker in (
                "unauthorized",
                "not authorized",
                "forbidden",
                "permission",
                "access denied",
            )
        )

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
                return self.parse_image_item(value)

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
        self,
        session: aiohttp.ClientSession,
        image_ref: dict[str, str],
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
        self,
        session: aiohttp.ClientSession,
        image_url: str,
    ) -> str:
        if image_url.startswith(("http://", "https://")):
            url = image_url
        else:
            url = urljoin(f"{self.gateway_url('/')}", image_url)
        async with session.get(
            url,
            headers=await self.gateway_headers(session),
        ) as response:
            if response.status >= 400:
                body = await response.text()
                raise RuntimeError(f"{self.gateway_label()} image download {response.status}: {body}")
            content_type = response.headers.get("Content-Type", "image/png")
            return self.write_generated_image(await response.read(), content_type)

    def write_generated_image(self, content: bytes, content_type: str) -> str:
        suffix = ".jpg" if "jpeg" in content_type.lower() else ".png"
        with tempfile.NamedTemporaryFile(
            prefix="murmur-image-", suffix=suffix, delete=False
        ) as image_file:
            image_file.write(content)
            return image_file.name

    def litellm_url(self, path: str) -> str:
        if not self.settings.litellm_base_url:
            raise UserVisibleError("LITELLM_BASE_URL is not configured.")
        return f"{self.settings.litellm_base_url}{path}"

    def openwebui_url(self, path: str) -> str:
        if not self.settings.openwebui_base_url:
            raise UserVisibleError("OPENWEBUI_BASE_URL is not configured.")
        return f"{self.settings.openwebui_base_url}{path}"

    def gateway_url(self, path: str) -> str:
        if self.use_openwebui():
            return self.openwebui_url(path)
        return self.litellm_url(path)

    def image_generation_url(self) -> str:
        if self.use_openwebui():
            return self.openwebui_url("/api/v1/images/generations")
        return self.litellm_url("/images/generations")

    def chat_completion_url(self) -> str:
        if self.use_openwebui():
            return self.openwebui_url("/api/chat/completions")
        return self.litellm_url("/chat/completions")

    async def request_chat_completion(self, payload: dict) -> str:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout_seconds)
        ) as session:
            body, status = await self.post_chat_completion(session, payload)
            if self.use_openwebui() and status == 401 and not self.settings.openwebui_api_key:
                self.openwebui_token = None
                body, status = await self.post_chat_completion(session, payload)

        return self.chat_completion_answer_from_body(payload, body, status)

    def chat_completion_answer_from_body(
        self,
        payload: dict,
        body: object,
        status: int,
    ) -> str:
        if status >= 400:
            model = payload.get("model")
            raise GatewayResponseError(
                self.gateway_error_message(status, body, model),
                status=status,
                body=body,
                model=model,
                retryable=self.is_retryable_gateway_body(status, body),
            )

        model = payload.get("model")
        if body is None:
            raise GatewayResponseError(
                self.gateway_null_response_message(status, model),
                status=status,
                body=body,
                model=model,
                retryable=True,
            )

        if not isinstance(body, dict):
            raise GatewayResponseError(
                self.gateway_error_message(status, body, model),
                status=status,
                body=body,
                model=model,
            )

        return self.extract_chat_response_content(status, body, model)

    def extract_chat_response_content(
        self,
        status: int,
        body: dict,
        model: object | None,
    ) -> str:
        try:
            choice = body["choices"][0]
            message = choice.get("message") if isinstance(choice, dict) else None
            if not isinstance(message, dict):
                raise KeyError("choices[0].message")
            content = message.get("content")
        except (KeyError, IndexError, TypeError) as exc:
            retryable = self.is_retryable_gateway_body(status, body)
            raise GatewayResponseError(
                self.gateway_error_message(status, body, model),
                status=status,
                body=body,
                model=model,
                retryable=retryable,
            ) from exc

        text_parts: list[str] = []
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, str):
                    text_parts.append(item)
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    text_parts.append(item["text"])

        for key in ("reasoning_content", "reasoning", "text"):
            value = message.get(key)
            if isinstance(value, str):
                text_parts.append(value)

        text = "\n".join(part.strip() for part in text_parts if part and part.strip())
        if text:
            return text

        raise GatewayResponseError(
            (
                f"{self.gateway_label()} error {status} for model {model}: response message "
                f"content was empty or null. Raw response: {body}"
            ),
            status=status,
            body=body,
            model=model,
            retryable=True,
        )

    async def post_chat_completion(
        self, session: aiohttp.ClientSession, payload: dict
    ) -> tuple[object, int]:
        async with session.post(
            self.chat_completion_url(),
            headers=await self.gateway_headers(session),
            json=payload,
        ) as response:
            return await self.read_response_body(response), response.status

    async def read_response_body(self, response: aiohttp.ClientResponse) -> object:
        try:
            return await response.json(content_type=None)
        except Exception:
            return await response.text()

    def gateway_error_message(
        self,
        status: int,
        body: object,
        model: object | None = None,
    ) -> str:
        model_text = f" for model {model}" if model else ""
        message = f"{self.gateway_label()} error {status}{model_text}: {body}"
        if self.is_rate_limit_error(body):
            message += (
                f"\n\nRate limit hit. Pick another {self.gateway_label()} model: "
                f"{self.settings.bot_prefix} models, then "
                f"{self.settings.bot_prefix} model <number|model-id>."
            )
        return message

    def gateway_null_response_message(
        self,
        status: int,
        model: object | None = None,
    ) -> str:
        message = self.gateway_error_message(status, None, model)
        return (
            f"{message}\n"
            f"{self.gateway_label()} logged: Provider returned error\n"
            f"{self.gateway_label()} returned HTTP 200 with a null JSON body, so Murmur "
            "could not read a deeper provider payload from the response."
        )

    def is_retryable_gateway_body(self, status: int, body: object) -> bool:
        if status in {408, 409, 425, 429} or status >= 500:
            return True
        text = str(body).lower()
        return any(
            phrase in text
            for phrase in (
                "provider returned error",
                "rate limit",
                "rate_limit",
                "quota",
                "too many requests",
                "temporarily unavailable",
                "timeout",
            )
        )

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

    async def litellm_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.litellm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.litellm_api_key}"
        return headers

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
            self.openwebui_url("/api/v1/auths/signin"),
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

    async def gateway_headers(self, session: aiohttp.ClientSession) -> dict[str, str]:
        if self.use_openwebui():
            return await self.openwebui_headers(session)
        return await self.litellm_headers()


def main() -> None:
    try:
        load_dotenv()
        asyncio.run(ensure_webshare_proxy_state())
        Murmur(load_settings()).run()
    except Exception as exc:
        if facebook_cookie_expired_error(exc):
            print(
                "FACEBOOK_COOKIE_EXPIRED_DETECTED "
                f"exit_code={FACEBOOK_COOKIE_EXPIRED_EXIT_CODE} "
                f"error={type(exc).__name__}: {exc}",
                flush=True,
            )
            raise SystemExit(FACEBOOK_COOKIE_EXPIRED_EXIT_CODE) from exc
        raise
