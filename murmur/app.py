import asyncio
import base64
import os
import tempfile
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Deque
from urllib.parse import unquote, urlparse

import aiohttp
from dotenv import load_dotenv
from fbchat_muqit import Client, EventType, Message


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
    provider_base_url: str | None
    provider_api_key: str | None
    image_provider: str
    cloudflare_account_id: str | None
    cloudflare_api_token: str | None
    image_model: str
    vision_model: str
    image_aspect_ratio: str
    allow_paid_image_models: bool
    fb_cookies_path: str
    fb_user_agent: str | None
    fb_proxy: str | None
    fb_mqtt_proxy: str | None
    fb_mqtt_watchdog_seconds: int
    messenger_upload_retries: int
    messenger_upload_retry_seconds: float
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
    image_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelOption:
    id: str
    name: str
    provider: str = ""
    is_free: bool = False
    pricing: dict[str, str] | None = None
    output_modalities: tuple[str, ...] = ()


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


def load_settings() -> Settings:
    load_dotenv()

    port = os.getenv("PORT", "8080")
    openwebui_base_url = os.getenv("OPENWEBUI_BASE_URL") or f"http://127.0.0.1:{port}"
    openwebui_model = os.environ["OPENWEBUI_MODEL"]
    image_provider = os.getenv("IMAGE_PROVIDER", "openrouter").strip().lower()
    cloudflare_image_model = os.getenv(
        "CLOUDFLARE_IMAGE_MODEL",
        "@cf/black-forest-labs/flux-1-schnell",
    )
    image_model = os.getenv("IMAGE_MODEL")
    if image_provider == "cloudflare":
        image_model = cloudflare_image_model or image_model
    image_model = image_model or "openrouter/free"

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
        provider_base_url=(os.getenv("OPENAI_API_BASE_URL") or "").rstrip("/") or None,
        provider_api_key=os.getenv("OPENAI_API_KEY") or None,
        image_provider=image_provider,
        cloudflare_account_id=os.getenv("CF_ACCOUNT_ID")
        or os.getenv("CLOUDFLARE_ACCOUNT_ID")
        or None,
        cloudflare_api_token=os.getenv("CF_API_TOKEN")
        or os.getenv("CLOUDFLARE_API_TOKEN")
        or None,
        image_model=image_model,
        vision_model=os.getenv("VISION_MODEL", "openrouter/free"),
        image_aspect_ratio=os.getenv("IMAGE_ASPECT_RATIO", "1:1"),
        allow_paid_image_models=env_bool("ALLOW_PAID_IMAGE_MODELS", False),
        fb_cookies_path=os.getenv("FB_COOKIES_PATH", "cookies.json"),
        fb_user_agent=os.getenv("FB_USER_AGENT") or None,
        fb_proxy=os.getenv("FB_PROXY") or None,
        fb_mqtt_proxy=os.getenv("FB_MQTT_PROXY") or os.getenv("FB_PROXY") or None,
        fb_mqtt_watchdog_seconds=int(os.getenv("FB_MQTT_WATCHDOG_SECONDS", "15")),
        messenger_upload_retries=int(os.getenv("MESSENGER_UPLOAD_RETRIES", "3")),
        messenger_upload_retry_seconds=float(
            os.getenv("MESSENGER_UPLOAD_RETRY_SECONDS", "3")
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
        self.thread_image_providers: dict[str, str] = {}
        self.thread_image_models: dict[str, str] = {}
        self.thread_image_model_aliases: dict[str, str] = {}
        self.thread_image_model_options: dict[str, list[ModelOption]] = {}
        self.thread_image_ratios: dict[str, str] = {}
        self.openwebui_token: str | None = None
        self.mqtt_watchdog_task: asyncio.Task | None = None
        self.response_tasks: set[asyncio.Task] = set()
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
                payload = {
                    "model": self.settings.openwebui_model,
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
        image_urls = self.extract_image_urls(message)

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
            elif image_urls:
                answer = await self.ask_vision(
                    message.thread_id,
                    prompt,
                    image_urls,
                )
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
        replied = False
        image_errors = []

        try:
            if response.text:
                for index, part in enumerate(self.split_reply(response.text)):
                    sent_message_id = await self.client.send_message(
                        text=part,
                        thread_id=message.thread_id,
                        reply_to_message=message.id if index == 0 else None,
                    )
                    replied = True
                    if sent_message_id:
                        self.sent_message_ids[message.thread_id].append(sent_message_id)
                    await asyncio.sleep(0.5)

            for index, path in enumerate(response.image_paths):
                try:
                    sent_message_id = await self.send_image_path(
                        message,
                        path,
                        reply_to_message=message.id if not replied and index == 0 else None,
                    )
                    if sent_message_id:
                        self.sent_message_ids[message.thread_id].append(sent_message_id)
                    replied = True
                except Exception as exc:
                    image_errors.append(exc)
                    print(f"Failed to upload image for message {message.id}: {exc}")
                await asyncio.sleep(0.5)

            if image_errors:
                await self.send_upload_failure_message(
                    message,
                    image_errors[-1],
                    reply_to_message=None if replied else message.id,
                )
        finally:
            for path in response.image_paths:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass

    async def send_image_path(
        self,
        message: Message,
        path: str,
        reply_to_message: str | None,
    ) -> str | None:
        file_ids = await self.upload_image_path(path)
        return await self.client.send_message(
            text=None,
            thread_id=message.thread_id,
            files_ids=file_ids,
            reply_to_message=reply_to_message,
        )

    async def upload_image_path(self, path: str) -> list[int]:
        attempts = max(1, self.settings.messenger_upload_retries)
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                print(
                    "Uploading Messenger image "
                    f"{Path(path).name} (attempt {attempt}/{attempts})."
                )
                return await self.client.uploadFiles(
                    file_path=[path],
                    voice_clip=False,
                )
            except Exception as exc:
                last_error = exc
                print(
                    "Messenger image upload "
                    f"attempt {attempt}/{attempts} failed: {exc}"
                )
                if attempt < attempts:
                    await asyncio.sleep(self.settings.messenger_upload_retry_seconds)

        raise RuntimeError("Messenger image upload failed") from last_error

    async def send_upload_failure_message(
        self,
        message: Message,
        error: Exception,
        reply_to_message: str | None,
    ) -> None:
        sent_message_id = await self.client.send_message(
            text=self.upload_failure_text(error),
            thread_id=message.thread_id,
            reply_to_message=reply_to_message,
        )
        if sent_message_id:
            self.sent_message_ids[message.thread_id].append(sent_message_id)

    def upload_failure_text(self, error: Exception) -> str:
        message = str(error).lower()
        if "timeout" in message or "timed out" in message:
            return (
                "Generated the image, but Facebook's image upload endpoint timed out. "
                "Please try again in a moment."
            )
        return (
            "Generated the image, but Messenger failed to accept the upload. "
            "Please try again in a moment."
        )

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
            return await self.model_list_message(thread_id, include_all=include_all)

        if command in {"model", "use"}:
            if len(parts) == 1:
                alias = self.current_model_alias(thread_id)
                model = self.current_model(thread_id)
                return (
                    f"Current model: {alias} ({model})\n"
                    f"Use {self.settings.bot_prefix} models to see options."
                )

            return self.set_thread_model(thread_id, parts[1])

        if command.startswith("@"):
            if self.resolve_model(command[1:], thread_id) is None:
                return (
                    f"Unknown model alias: {command[1:]}\n"
                    f"Use {self.settings.bot_prefix} models to see available models."
                )
            if len(parts) == 1:
                return self.set_thread_model(thread_id, command[1:])

        return None

    async def handle_media_command(
        self,
        message: Message,
        prompt: str,
    ) -> BotResponse | None:
        parts = prompt.split(maxsplit=1)
        if not parts:
            return None

        command = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if command in {"image", "img", "draw"}:
            return await self.handle_image_command(message.thread_id, rest)

        if command in {"see", "vision", "look"}:
            image_urls = self.extract_image_urls(message)
            if not image_urls:
                return BotResponse(
                    text="Send an image with this command, or reply to an image."
                )
            question = rest or "Describe this image."
            answer = await self.ask_vision(message.thread_id, question, image_urls)
            return BotResponse(text=answer)

        return None

    def user_facing_error(self, exc: Exception) -> str:
        message = str(exc)
        lowered = message.lower()
        if "insufficient credits" in lowered or "provider error 402" in lowered:
            return (
                "OpenRouter refused this image endpoint because your account has no "
                "purchased credits. Some image models are free-priced but still require "
                f"credits/routing. Try another model from {self.settings.bot_prefix} "
                "image models, or add OpenRouter credits."
            )

        if "output modalities" in lowered:
            return (
                "That model does not support the image output mode Murmur requested. "
                f"Try {self.settings.bot_prefix} image models and select another model."
            )

        if "no free image generation models found" in lowered:
            return "I could not find a free image generation model from the provider."

        if "cloudflare image generation needs" in lowered:
            return "Cloudflare image generation needs CF_ACCOUNT_ID and CF_API_TOKEN."

        if "cloudflare provider error" in lowered:
            return (
                "Cloudflare refused that image request. The daily free allocation may "
                "be used up, or that model may need different inputs."
            )

        return "I hit an error while thinking. Check the bot logs."

    async def handle_image_command(
        self,
        thread_id: str,
        prompt: str,
    ) -> BotResponse:
        if not prompt:
            return BotResponse(text=self.image_help_message())

        parts = prompt.split(maxsplit=1)
        subcommand = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if subcommand in {"help", "commands", "?"}:
            return BotResponse(text=self.image_help_message())

        if subcommand == "models":
            include_all = rest.lower() == "all"
            return BotResponse(
                text=await self.image_model_list_message(thread_id, include_all)
            )

        if subcommand == "model":
            if not rest:
                alias = self.current_image_model_alias(thread_id)
                model = self.current_image_model(thread_id)
                provider = self.current_image_provider(thread_id)
                return BotResponse(
                    text=(
                        f"Current image model: {alias} "
                        f"({self.provider_label(provider)}: {model})"
                    )
                )
            return BotResponse(text=self.set_thread_image_model(thread_id, rest))

        if subcommand in {"ratio", "size"}:
            if not rest:
                return BotResponse(
                    text=f"Current image ratio: {self.current_image_ratio(thread_id)}"
                )
            return BotResponse(text=self.set_thread_image_ratio(thread_id, rest))

        model = self.current_image_model(thread_id)
        provider = self.current_image_provider(thread_id)
        image_prompt = prompt
        if subcommand.startswith("@"):
            option = self.resolve_image_option(thread_id, subcommand[1:])
            if option is None:
                return BotResponse(
                    text=(
                        f"Unknown image model: {subcommand[1:]}\n"
                        f"Use {self.settings.bot_prefix} image models."
                    )
                )
            model = option.id
            provider = option.provider or provider
            image_prompt = rest or "Generate an image."

        return await self.generate_image_response(
            thread_id,
            image_prompt,
            model,
            provider,
        )

    def image_help_message(self) -> str:
        prefix = self.settings.bot_prefix
        return "\n".join(
            [
                "Image commands:",
                f"{prefix} image models",
                f"{prefix} image models all",
                f"{prefix} image model <number>",
                f"{prefix} image ratio 16:9",
                f"{prefix} image a tiny robot drinking tea",
                f"{prefix} image @3 a cinematic night market",
                f"{prefix} see what is in this image?",
            ]
        )

    def help_message(self, thread_id: str) -> str:
        prefix = self.settings.bot_prefix
        return "\n".join(
            [
                "Murmur help",
                "",
                "Quick start",
                f"- {prefix} your question",
                "- Reply to one of my messages to continue without the prefix.",
                f"- /help or {prefix} help shows this guide.",
                f"- {prefix} status shows selected models and providers.",
                "",
                "Text chat",
                f"- {prefix} models: list free text models.",
                f"- {prefix} models all: list all detected text models.",
                f"- {prefix} model: show selected text model.",
                f"- {prefix} model <number|alias>: set text model for this thread.",
                f"- {prefix} @<number|alias> message: one-shot text model.",
                "",
                "Images",
                f"- {prefix} image models: list usable image models from all configured providers.",
                f"- {prefix} image models all: list all detected image models from all configured providers.",
                f"- {prefix} image model: show selected image model.",
                f"- {prefix} image model <number>: set image model for this thread.",
                f"- {prefix} image ratio <ratio>: set image ratio.",
                f"- {prefix} image prompt: generate an image.",
                f"- {prefix} image @<number> prompt: one-shot image model.",
                "",
                "Vision",
                f"- {prefix} see question: ask about an attached image.",
                f"- Reply to an image with {prefix} see question.",
                "- Sending an image with a normal prompt also routes to vision.",
                "",
                "Open WebUI bridge features",
                "- Text chat uses Open WebUI /api/chat/completions.",
                "- Auth uses OPENWEBUI_API_KEY or WebUI admin email/password.",
                "- Text model listing uses the configured provider /models, then Open WebUI /api/models as fallback.",
                "- Per-thread short memory is kept in Murmur before sending to Open WebUI.",
                "- Long replies are split for Messenger delivery.",
                "- Image generation currently uses the configured image provider directly.",
                "",
                "Current",
                self.status_message(thread_id),
            ]
        )

    def status_message(self, thread_id: str) -> str:
        text_provider = self.text_provider_label()
        image_provider = self.current_image_provider(thread_id)
        text_alias = self.current_model_alias(thread_id)
        image_alias = self.current_image_model_alias(thread_id)
        return "\n".join(
            [
                "Status",
                f"Text provider: {text_provider}",
                f"Text model: {text_alias} ({self.current_model(thread_id)})",
                f"Image provider: {self.provider_label(image_provider)}",
                f"Image model: {image_alias} ({self.current_image_model(thread_id)})",
                f"Image ratio: {self.current_image_ratio(thread_id)}",
                f"Vision model: {self.settings.vision_model}",
            ]
        )

    def text_provider_label(self) -> str:
        provider = self.text_provider_id()
        if provider == "provider" and self.settings.provider_base_url:
            host = urlparse(self.settings.provider_base_url).netloc
            return host or self.settings.provider_base_url
        return self.provider_label(provider)

    def text_provider_id(self) -> str:
        if self.settings.provider_base_url:
            return self.openai_image_provider_id()
        return "openwebui"

    def response_header(self, provider: str, model: str) -> str:
        return f"[{self.provider_label(provider)} - {model}]"

    async def image_model_list_message(
        self,
        thread_id: str,
        include_all: bool = False,
    ) -> str:
        options = await self.fetch_image_model_options(include_all=include_all)
        if not options:
            return (
                "No free image generation models found."
                if not include_all
                else "No image generation models found."
            )

        self.thread_image_model_options[thread_id] = options
        current_model = self.current_image_model(thread_id)
        current_provider = self.current_image_provider(thread_id)
        title = "Image models" if include_all else "Free image models"
        lines = [f"{title} ({len(options)}):"]
        providers = self.configured_image_providers()
        grouped = {
            provider: [
                option for option in options if option.provider == provider
            ]
            for provider in providers
        }

        index = 1
        for provider in providers:
            provider_options = grouped.get(provider, [])
            lines.append("")
            lines.append(self.provider_label(provider))
            if not provider_options:
                if include_all:
                    lines.append("- no image models found")
                else:
                    lines.append("- no free/usable image models found")
                continue

            for option in provider_options:
                marker = (
                    "*"
                    if option.id == current_model and option.provider == current_provider
                    else "-"
                )
                price = "free" if self.is_free_image_model(option) else "paid/unknown"
                outputs = (
                    ",".join(option.output_modalities)
                    if option.output_modalities
                    else "image"
                )
                if option.name and option.name != option.id:
                    lines.append(
                        f"{marker} {index}. {option.id} ({option.name}) [{price}; {outputs}]"
                    )
                else:
                    lines.append(f"{marker} {index}. {option.id} [{price}; {outputs}]")
                index += 1

        lines.append("")
        lines.append(f"Switch: {self.settings.bot_prefix} image model <number>")
        lines.append(f"Generate: {self.settings.bot_prefix} image your prompt")
        lines.append(f"All image models: {self.settings.bot_prefix} image models all")
        if "cloudflare" in providers:
            lines.append("Note: Cloudflare Workers AI free allocation resets daily.")
        if "openrouter" in providers:
            lines.append(
                "Note: OpenRouter can still credit-gate free-priced image endpoints."
            )
        return "\n".join(lines)

    async def fetch_image_model_options(
        self,
        include_all: bool = False,
    ) -> list[ModelOption]:
        options: list[ModelOption] = []
        if "cloudflare" in self.configured_image_providers():
            options.extend(await self.fetch_cloudflare_image_model_options(include_all))

        openai_provider = self.openai_image_provider_id()
        if openai_provider in self.configured_image_providers():
            models = [
                replace(model, provider=openai_provider)
                for model in await self.fetch_provider_models(output_modalities="image")
            ]
            if not include_all:
                models = [model for model in models if self.is_free_image_model(model)]
            options.extend(sorted(models, key=lambda model: model.id))

        return options

    def configured_image_providers(self) -> list[str]:
        providers = []
        if self.settings.cloudflare_account_id and self.settings.cloudflare_api_token:
            providers.append("cloudflare")
        if self.settings.provider_base_url and self.settings.provider_api_key:
            providers.append(self.openai_image_provider_id())
        return providers

    def openai_image_provider_id(self) -> str:
        host = urlparse(self.settings.provider_base_url or "").netloc.lower()
        if "openrouter.ai" in host:
            return "openrouter"
        return host or "provider"

    def provider_label(self, provider: str) -> str:
        labels = {
            "cloudflare": "Cloudflare",
            "openrouter": "OpenRouter",
            "openwebui": "OpenWebUI",
            "provider": "Provider",
        }
        return labels.get(provider, provider)

    def current_image_model(self, thread_id: str) -> str:
        return self.thread_image_models.get(thread_id, self.settings.image_model)

    def current_image_provider(self, thread_id: str) -> str:
        return self.thread_image_providers.get(thread_id, self.settings.image_provider)

    def current_image_model_alias(self, thread_id: str) -> str:
        return self.thread_image_model_aliases.get(thread_id, "default")

    def current_image_ratio(self, thread_id: str) -> str:
        return self.thread_image_ratios.get(thread_id, self.settings.image_aspect_ratio)

    def resolve_image_option(self, thread_id: str, name: str) -> ModelOption | None:
        key = name.strip().lower().lstrip("@")
        if key.isdigit():
            index = int(key) - 1
            options = self.thread_image_model_options.get(thread_id, [])
            if 0 <= index < len(options):
                return options[index]

        if key in {"default", "free"}:
            return ModelOption(
                id=self.settings.image_model,
                name=self.settings.image_model,
                provider=self.settings.image_provider,
                is_free=self.settings.image_provider == "cloudflare",
                output_modalities=("image",),
            )

        if "/" in name:
            return ModelOption(
                id=name.strip(),
                name=name.strip(),
                provider=self.current_image_provider(thread_id),
                output_modalities=("image",),
            )

        return None

    def set_thread_image_model(self, thread_id: str, name: str) -> str:
        option = self.resolve_image_option(thread_id, name)
        if option is None:
            return (
                f"Unknown image model: {name}\n"
                f"Use {self.settings.bot_prefix} image models to list options."
            )

        model = option.id
        provider = option.provider or self.current_image_provider(thread_id)
        if not self.settings.allow_paid_image_models:
            known_option = self.image_option_by_id(thread_id, model, provider)
            if not known_option and model != self.settings.image_model:
                return (
                    "I do not know whether that image model is free. Run "
                    f"{self.settings.bot_prefix} image models first, or set "
                    "ALLOW_PAID_IMAGE_MODELS=true."
                )
            if known_option and not self.is_free_image_model(known_option):
                return (
                    "That image model looks paid or unknown. Set "
                    "ALLOW_PAID_IMAGE_MODELS=true if you want Murmur to try it."
                )

        self.thread_image_providers[thread_id] = provider
        self.thread_image_models[thread_id] = model
        self.thread_image_model_aliases[thread_id] = name.strip().lower().lstrip("@")
        return (
            f"Image model set to {name} "
            f"({self.provider_label(provider)}: {model}) for this thread."
        )

    def image_option_by_id(
        self,
        thread_id: str,
        model_id: str,
        provider: str | None = None,
    ) -> ModelOption | None:
        for option in self.thread_image_model_options.get(thread_id, []):
            if option.id == model_id and (provider is None or option.provider == provider):
                return option
        return None

    def set_thread_image_ratio(self, thread_id: str, ratio: str) -> str:
        allowed = {
            "1:1",
            "2:3",
            "3:2",
            "3:4",
            "4:3",
            "4:5",
            "5:4",
            "9:16",
            "16:9",
            "21:9",
        }
        ratio = ratio.strip()
        if ratio not in allowed:
            return "Supported ratios: " + ", ".join(sorted(allowed))

        self.thread_image_ratios[thread_id] = ratio
        return f"Image ratio set to {ratio} for this thread."

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

    def resolve_model(self, name: str, thread_id: str | None = None) -> str | None:
        key = name.strip().lower().lstrip("@")
        if thread_id and key.isdigit():
            index = int(key) - 1
            options = self.thread_model_options.get(thread_id, [])
            if 0 <= index < len(options):
                return options[index].id

        return self.settings.openwebui_model_aliases.get(key)

    def set_thread_model(self, thread_id: str, name: str) -> str:
        alias = name.strip().lower().lstrip("@")
        model = self.resolve_model(alias, thread_id)
        if model is None:
            return (
                f"Unknown model: {name}\n"
                f"Use {self.settings.bot_prefix} models to see available models."
            )

        self.thread_models[thread_id] = model
        self.thread_model_aliases[thread_id] = self.model_label(thread_id, alias, model)
        return f"Model set to {alias} ({model}) for this thread."

    def model_label(self, thread_id: str, requested: str, model: str) -> str:
        if requested.isdigit():
            index = int(requested) - 1
            options = self.thread_model_options.get(thread_id, [])
            if 0 <= index < len(options):
                return str(index + 1)
        return requested or "default"

    async def model_list_message(self, thread_id: str, include_all: bool = False) -> str:
        options = await self.fetch_model_options(include_all=include_all)
        if options:
            self.thread_model_options[thread_id] = options
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
        title = "Available models" if include_all else "Free models"
        lines = [f"{title} ({len(options)}):"]

        for index, option in enumerate(options, start=1):
            marker = "*" if option.id == current_model else "-"
            if option.name and option.name != option.id:
                lines.append(f"{marker} {index}. {option.id} ({option.name})")
            else:
                lines.append(f"{marker} {index}. {option.id}")

        lines.append("")
        lines.append(f"Switch: {self.settings.bot_prefix} model <number>")
        lines.append(f"One-shot alias: {self.settings.bot_prefix} @free your message")
        lines.append(f"All models: {self.settings.bot_prefix} models all")
        return "\n".join(lines)

    async def fetch_model_options(self, include_all: bool = False) -> list[ModelOption]:
        models = await self.fetch_provider_models()
        if not models:
            models = await self.fetch_openwebui_models()

        if not include_all:
            models = [model for model in models if self.is_free_model(model)]

        return sorted(models, key=lambda model: model.id)

    async def fetch_provider_models(
        self,
        output_modalities: str | None = None,
    ) -> list[ModelOption]:
        if not self.settings.provider_base_url:
            return []

        headers = {}
        if self.settings.provider_api_key:
            headers["Authorization"] = f"Bearer {self.settings.provider_api_key}"

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout_seconds)
        ) as session:
            try:
                async with session.get(
                    f"{self.settings.provider_base_url}/models",
                    headers=headers,
                    params=(
                        {"output_modalities": output_modalities}
                        if output_modalities
                        else None
                    ),
                ) as response:
                    if response.status >= 400:
                        return []
                    body = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                return []

        return self.parse_models_response(body)

    async def fetch_openwebui_models(self) -> list[ModelOption]:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout_seconds)
        ) as session:
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

                models = self.parse_models_response(body)
                if models:
                    return models

        return []

    def parse_models_response(self, body: object) -> list[ModelOption]:
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
                    is_free=self.is_free_raw_model(raw_model, str(model_id), str(name)),
                    pricing=(
                        raw_model.get("pricing")
                        if isinstance(raw_model.get("pricing"), dict)
                        else None
                    ),
                    output_modalities=self.output_modalities_from_raw_model(raw_model),
                )
            )

        return models

    def is_free_model(self, model: ModelOption) -> bool:
        return model.is_free or self.is_free_model_id(model.id, model.name)

    def is_free_image_model(self, model: ModelOption) -> bool:
        if model.provider == "cloudflare":
            return True
        return self.is_free_model_id(model.id, model.name)

    def using_cloudflare_images(self, provider: str | None = None) -> bool:
        return (provider or self.settings.image_provider) == "cloudflare"

    def is_free_model_id(self, model_id: str, name: str) -> bool:
        model_id = model_id.lower()
        name = name.lower()
        return (
            model_id == "openrouter/free"
            or model_id.endswith(":free")
            or ":free" in model_id
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

    def output_modalities_from_raw_model(self, raw_model: dict) -> tuple[str, ...]:
        architecture = raw_model.get("architecture")
        if not isinstance(architecture, dict):
            return ()

        output_modalities = architecture.get("output_modalities")
        if not isinstance(output_modalities, list):
            return ()

        return tuple(
            str(modality).lower()
            for modality in output_modalities
            if isinstance(modality, str)
        )

    async def fetch_cloudflare_image_model_options(
        self,
        include_all: bool = False,
    ) -> list[ModelOption]:
        if (
            not self.settings.cloudflare_account_id
            or not self.settings.cloudflare_api_token
        ):
            return []

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout_seconds)
        ) as session:
            async with session.get(
                f"{self.cloudflare_ai_base_url()}/models/search",
                headers=self.cloudflare_headers(),
                params={
                    "per_page": 100,
                    "hide_experimental": "true",
                    "task": "Text-to-Image",
                },
            ) as response:
                body = await response.json(content_type=None)
                if response.status >= 400 or not body.get("success", False):
                    return []

            models = []
            for item in body.get("result", []):
                if not isinstance(item, dict):
                    continue
                model_name = item.get("name")
                if not isinstance(model_name, str) or not model_name:
                    continue

                simple = await self.cloudflare_supports_prompt_json(
                    session,
                    model_name,
                )
                if include_all or simple:
                    models.append(
                        ModelOption(
                            id=model_name,
                            name=model_name,
                            provider="cloudflare",
                            is_free=True,
                            output_modalities=("image",),
                        )
                    )

        return sorted(models, key=lambda model: model.id)

    async def cloudflare_supports_prompt_json(
        self,
        session: aiohttp.ClientSession,
        model: str,
    ) -> bool:
        schema = await self.cloudflare_model_schema(session, model)
        input_schema = schema.get("input") if isinstance(schema, dict) else None
        if not isinstance(input_schema, dict):
            return True

        properties = input_schema.get("properties")
        property_names = set(properties.keys()) if isinstance(properties, dict) else set()
        required = input_schema.get("required")
        required_names = set(required) if isinstance(required, list) else set()

        if "multipart" in property_names or "multipart" in required_names:
            return False

        return "prompt" in property_names or "prompt" in required_names

    async def cloudflare_model_schema(
        self,
        session: aiohttp.ClientSession,
        model: str,
    ) -> dict:
        async with session.get(
            f"{self.cloudflare_ai_base_url()}/models/schema",
            headers=self.cloudflare_headers(),
            params={"model": model},
        ) as response:
            body = await response.json(content_type=None)
            if response.status >= 400 or not body.get("success", False):
                return {}
            result = body.get("result")
            return result if isinstance(result, dict) else {}

    async def generate_image_response(
        self,
        thread_id: str,
        prompt: str,
        model: str,
        provider: str | None = None,
    ) -> BotResponse:
        provider = provider or self.current_image_provider(thread_id)
        if self.using_cloudflare_images(provider):
            return await self.generate_cloudflare_image_response(thread_id, prompt, model)

        if not self.settings.provider_base_url or not self.settings.provider_api_key:
            return BotResponse(text="Image generation needs OPENAI_API_BASE_URL and OPENAI_API_KEY.")

        option = self.image_option_by_id(thread_id, model, provider)
        if option and not self.is_free_image_model(option) and not self.settings.allow_paid_image_models:
            return BotResponse(
                text=(
                    "That image model looks paid or unknown. Set "
                    "ALLOW_PAID_IMAGE_MODELS=true if you want Murmur to try it."
                )
            )

        body = await self.request_provider_chat_completion(
            self.image_generation_payload(thread_id, prompt, model)
        )
        message = self.first_choice_message(body)
        image_urls = self.generated_image_urls(message)
        text = self.message_content_text(message)

        if not image_urls:
            return BotResponse(
                text=text
                or "The image model replied without an image. Try another image model."
            )

        image_paths = []
        for image_url in image_urls:
            image_paths.append(await self.image_url_to_temp_file(image_url))

        return BotResponse(
            text=f"{self.response_header(provider, model)}\n{text or 'Generated.'}",
            image_paths=tuple(image_paths),
        )

    async def generate_cloudflare_image_response(
        self,
        thread_id: str,
        prompt: str,
        model: str,
    ) -> BotResponse:
        if (
            not self.settings.cloudflare_account_id
            or not self.settings.cloudflare_api_token
        ):
            raise RuntimeError("Cloudflare image generation needs CF_ACCOUNT_ID and CF_API_TOKEN.")

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout_seconds)
        ) as session:
            if not await self.cloudflare_supports_prompt_json(session, model):
                return BotResponse(
                    text=(
                        "That Cloudflare image model needs multipart/image inputs, "
                        "so Murmur cannot use it for simple text prompts yet."
                    )
                )

            payload = await self.cloudflare_image_payload(session, thread_id, prompt, model)
            async with session.post(
                f"{self.cloudflare_ai_base_url()}/run/{model}",
                headers=self.cloudflare_headers(),
                json=payload,
            ) as response:
                content_type = response.headers.get("content-type", "image/png")
                if content_type.startswith("image/"):
                    if response.status >= 400:
                        text = await response.text()
                        raise RuntimeError(
                            f"Cloudflare provider error {response.status}: {text}"
                        )
                    image_path = self.write_temp_image(
                        await response.read(),
                        self.suffix_from_mime(content_type),
                    )
                    return BotResponse(
                        text=f"{self.response_header('cloudflare', model)}\nGenerated.",
                        image_paths=(image_path,),
                    )

                body = await response.json(content_type=None)
                if response.status >= 400 or not body.get("success", False):
                    raise RuntimeError(
                        f"Cloudflare provider error {response.status}: {body}"
                    )

        result = body.get("result") if isinstance(body, dict) else None
        image = result.get("image") if isinstance(result, dict) else None
        if not isinstance(image, str) or not image:
            return BotResponse(
                text="The Cloudflare image model replied without an image."
            )

        image_path = self.write_temp_image(
            base64.b64decode(image),
            self.suffix_from_mime("image/jpeg"),
        )
        return BotResponse(
            text=f"{self.response_header('cloudflare', model)}\nGenerated.",
            image_paths=(image_path,),
        )

    async def cloudflare_image_payload(
        self,
        session: aiohttp.ClientSession,
        thread_id: str,
        prompt: str,
        model: str,
    ) -> dict:
        schema = await self.cloudflare_model_schema(session, model)
        input_schema = schema.get("input") if isinstance(schema, dict) else None
        properties = (
            input_schema.get("properties")
            if isinstance(input_schema, dict)
            and isinstance(input_schema.get("properties"), dict)
            else {}
        )

        payload: dict[str, object] = {"prompt": prompt}
        if "steps" in properties:
            payload["steps"] = 4
        if "height" in properties and "width" in properties:
            width, height = self.cloudflare_dimensions(thread_id)
            payload["width"] = width
            payload["height"] = height
        return payload

    def cloudflare_dimensions(self, thread_id: str) -> tuple[int, int]:
        return {
            "1:1": (512, 512),
            "2:3": (512, 768),
            "3:2": (768, 512),
            "3:4": (576, 768),
            "4:3": (768, 576),
            "4:5": (512, 640),
            "5:4": (640, 512),
            "9:16": (432, 768),
            "16:9": (768, 432),
            "21:9": (896, 384),
        }.get(self.current_image_ratio(thread_id), (512, 512))

    def cloudflare_ai_base_url(self) -> str:
        return (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{self.settings.cloudflare_account_id}/ai"
        )

    def cloudflare_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.cloudflare_api_token}",
            "Content-Type": "application/json",
        }

    def image_generation_payload(
        self,
        thread_id: str,
        prompt: str,
        model: str,
    ) -> dict:
        return {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "modalities": self.image_output_modalities(thread_id, model),
            "stream": False,
            "image_config": {"aspect_ratio": self.current_image_ratio(thread_id)},
        }

    def image_output_modalities(self, thread_id: str, model: str) -> list[str]:
        option = self.image_option_by_id(thread_id, model)
        if option and option.output_modalities:
            modalities = [modality for modality in option.output_modalities if modality]
            if "image" in modalities:
                return modalities

        return ["image"]

    async def ask_vision(
        self,
        thread_id: str,
        prompt: str,
        image_urls: list[str],
    ) -> str:
        if not self.settings.provider_base_url or not self.settings.provider_api_key:
            raise RuntimeError("Vision needs OPENAI_API_BASE_URL and OPENAI_API_KEY.")

        content = [{"type": "text", "text": prompt}]
        content.extend(
            {"type": "image_url", "image_url": {"url": image_url}}
            for image_url in image_urls
        )

        body = await self.request_provider_chat_completion(
            {
                "model": self.settings.vision_model,
                "messages": [
                    {"role": "system", "content": self.settings.system_prompt},
                    *self.history[thread_id],
                    {"role": "user", "content": content},
                ],
                "stream": False,
            }
        )
        message = self.first_choice_message(body)
        answer = self.message_content_text(message)
        if not answer:
            raise RuntimeError(f"Unexpected vision response: {body}")

        self.history[thread_id].append(
            {"role": "user", "content": f"{prompt} [image attached]"}
        )
        self.history[thread_id].append({"role": "assistant", "content": answer})
        return f"{self.response_header(self.text_provider_id(), self.settings.vision_model)}\n{answer}"

    async def request_provider_chat_completion(self, payload: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.settings.provider_api_key:
            headers["Authorization"] = f"Bearer {self.settings.provider_api_key}"

        tried_payloads = [payload]
        modalities = payload.get("modalities")
        if modalities == ["image"]:
            tried_payloads.append({**payload, "modalities": ["image", "text"]})
        elif modalities == ["image", "text"] or modalities == ["text", "image"]:
            tried_payloads.append({**payload, "modalities": ["image"]})

        last_error = None
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout_seconds)
        ) as session:
            for candidate in tried_payloads:
                async with session.post(
                    f"{self.settings.provider_base_url}/chat/completions",
                    headers=headers,
                    json=candidate,
                ) as response:
                    body = await response.json(content_type=None)
                    if response.status < 400:
                        return body

                    last_error = (response.status, body)
                    if not self.is_modality_error(response.status, body):
                        break

        status, body = last_error or (500, {"error": "provider request failed"})
        raise RuntimeError(f"Provider error {status}: {body}")

    def is_modality_error(self, status: int, body: object) -> bool:
        if status != 404:
            return False
        if not isinstance(body, dict):
            return False
        error = body.get("error")
        message = error.get("message") if isinstance(error, dict) else str(error)
        return "output modalities" in (message or "").lower()

    def first_choice_message(self, body: dict) -> dict:
        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected provider response: {body}") from exc

        if not isinstance(message, dict):
            raise RuntimeError(f"Unexpected provider message: {message}")
        return message

    def generated_image_urls(self, message: dict) -> list[str]:
        images = message.get("images") or []
        urls = []
        for image in images:
            if not isinstance(image, dict):
                continue
            image_url = (
                image.get("image_url") or image.get("imageUrl") or image.get("url")
            )
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if isinstance(image_url, str) and image_url:
                urls.append(image_url)
        return urls

    def message_content_text(self, message: dict) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()

        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(parts).strip()

        return ""

    async def image_url_to_temp_file(self, image_url: str) -> str:
        if image_url.startswith("data:"):
            return self.data_url_to_temp_file(image_url)

        parsed = urlparse(image_url)
        if parsed.scheme not in {"http", "https"}:
            raise RuntimeError("Unsupported generated image URL format.")

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout_seconds)
        ) as session:
            async with session.get(image_url) as response:
                if response.status >= 400:
                    raise RuntimeError(f"Failed to download image: {response.status}")
                content_type = response.headers.get("content-type", "image/png")
                data = await response.read()

        return self.write_temp_image(data, self.suffix_from_mime(content_type))

    def data_url_to_temp_file(self, data_url: str) -> str:
        header, _, encoded = data_url.partition(",")
        if not encoded:
            raise RuntimeError("Malformed image data URL.")
        mime = header[5:].split(";", 1)[0] if header.startswith("data:") else "image/png"
        return self.write_temp_image(base64.b64decode(encoded), self.suffix_from_mime(mime))

    def write_temp_image(self, data: bytes, suffix: str) -> str:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as file:
            file.write(data)
            return file.name

    def suffix_from_mime(self, mime: str) -> str:
        mime = mime.lower().split(";", 1)[0].strip()
        return {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(mime, ".png")

    def extract_image_urls(self, message: Message) -> list[str]:
        urls = []
        seen = set()
        for source in (message, message.replied_to_message):
            if source is None:
                continue
            for attachment in source.attachments or []:
                url = self.image_url_from_attachment(attachment)
                if url and url not in seen:
                    seen.add(url)
                    urls.append(url)
        return urls

    def image_url_from_attachment(self, attachment: object) -> str | None:
        for attr_name in ("large_preview", "thumbnail", "preview", "animated_image"):
            image = getattr(attachment, attr_name, None)
            url = getattr(image, "url", None)
            if url:
                return url

        url = getattr(attachment, "url", None)
        if url:
            return url

        return None

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
        return f"{self.response_header(self.text_provider_id(), model)}\n{answer}"

    async def request_chat_completion(self, payload: dict) -> str:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout_seconds)
        ) as session:
            body, status = await self.post_chat_completion(session, payload)
            if status == 401 and not self.settings.openwebui_api_key:
                self.openwebui_token = None
                body, status = await self.post_chat_completion(session, payload)

        if status >= 400:
            raise RuntimeError(f"Open WebUI error {status}: {body}")

        try:
            return body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected Open WebUI response: {body}") from exc

    async def post_chat_completion(
        self, session: aiohttp.ClientSession, payload: dict
    ) -> tuple[dict, int]:
        async with session.post(
            f"{self.settings.openwebui_base_url}/api/chat/completions",
            headers=await self.openwebui_headers(session),
            json=payload,
        ) as response:
            return await response.json(content_type=None), response.status

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
                raise RuntimeError(f"Open WebUI sign-in error {response.status}: {body}")

        try:
            self.openwebui_token = body["token"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(f"Unexpected Open WebUI sign-in response: {body}") from exc

        return self.openwebui_token


def main() -> None:
    Murmur(load_settings()).run()
