import asyncio
import os
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque

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
    fb_cookies_path: str
    fb_user_agent: str | None
    fb_proxy: str | None
    bot_prefix: str
    respond_only_on_prefix: bool
    respond_to_bot_replies: bool
    max_history_messages: int
    max_reply_chars: int
    request_timeout_seconds: int
    allowed_thread_ids: set[str]
    system_prompt: str


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    load_dotenv()

    port = os.getenv("PORT", "8080")
    openwebui_base_url = os.getenv("OPENWEBUI_BASE_URL") or f"http://127.0.0.1:{port}"

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
        openwebui_model=os.environ["OPENWEBUI_MODEL"],
        fb_cookies_path=os.getenv("FB_COOKIES_PATH", "cookies.json"),
        fb_user_agent=os.getenv("FB_USER_AGENT") or None,
        fb_proxy=os.getenv("FB_PROXY") or None,
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
        self.openwebui_token: str | None = None
        self.client = Client(
            cookies_file_path=settings.fb_cookies_path,
            userAgent=settings.fb_user_agent,
            proxy=settings.fb_proxy,
        )
        self.client.event(EventType.LISTENING)(self.on_listening)
        self.client.event(EventType.MESSAGE)(self.on_message)

    def run(self) -> None:
        self.client.run()

    async def on_listening(self) -> None:
        print(f"Murmur online as {self.client.name} ({self.client.uid})")

    async def on_message(self, message: Message) -> None:
        if message.sender_id == self.client.uid:
            return

        if not self.is_allowed_thread(message.thread_id):
            return

        prompt = self.get_prompt(message)
        if not prompt:
            return

        try:
            await self.client.typing(message.thread_id, True, message.thread_type)
            answer = await self.ask_openwebui(message.thread_id, prompt)
        except Exception as exc:
            print(f"Failed to answer message {message.id}: {exc}")
            answer = "I hit an error while thinking. Check the bot logs."
        finally:
            try:
                await self.client.typing(message.thread_id, False, message.thread_type)
            except Exception:
                pass

        for index, part in enumerate(self.split_reply(answer)):
            sent_message_id = await self.client.send_message(
                text=part,
                thread_id=message.thread_id,
                reply_to_message=message.id if index == 0 else None,
            )
            if sent_message_id:
                self.sent_message_ids[message.thread_id].append(sent_message_id)
            await asyncio.sleep(0.5)

    def is_allowed_thread(self, thread_id: str) -> bool:
        return (
            not self.settings.allowed_thread_ids
            or thread_id in self.settings.allowed_thread_ids
        )

    def get_prompt(self, message: Message) -> str | None:
        text = (message.text or "").strip()
        if not text:
            return None

        prefix = self.settings.bot_prefix
        if prefix and text.lower().startswith(prefix.lower()):
            prompt = text[len(prefix) :].strip()
            return prompt or "Hello"

        if self.settings.respond_to_bot_replies and self.is_reply_to_bot(message):
            return self.reply_prompt(message, text)

        if not self.settings.respond_only_on_prefix:
            return text

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

    async def ask_openwebui(self, thread_id: str, prompt: str) -> str:
        messages = [{"role": "system", "content": self.settings.system_prompt}]
        messages.extend(self.history[thread_id])
        messages.append({"role": "user", "content": prompt})

        answer = await self.request_chat_completion(
            {
                "model": self.settings.openwebui_model,
                "messages": messages,
                "stream": False,
            }
        )

        self.history[thread_id].append({"role": "user", "content": prompt})
        self.history[thread_id].append({"role": "assistant", "content": answer})
        return answer

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
