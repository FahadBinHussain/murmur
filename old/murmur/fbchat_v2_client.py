from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass
class _FBV2Message:
    id: str
    text: str
    sender_id: str
    thread_id: str
    thread_type: int

    @classmethod
    def from_delta(cls, delta: dict) -> Optional["_FBV2Message"]:
        try:
            md = delta.get("messageMetadata") or {}
            body = delta.get("body", "")
            actor = str(md.get("actorFbId", ""))
            msg_id = str(md.get("messageId", ""))
            tk = md.get("threadKey") or {}
            other_user = tk.get("otherUserFbId")
            thread_fb = tk.get("threadFbId")
            reply_to = str(other_user or thread_fb or "")
            is_group = other_user is None
            return cls(
                id=msg_id,
                text=body,
                sender_id=actor,
                thread_id=reply_to,
                thread_type=1 if is_group else 0,
            )
        except Exception:
            return None


class Client:
    def __init__(
        self,
        cookies_file_path: str,
        userAgent: Optional[str] = None,
        proxy: Optional[str] = None,
    ):
        self._cookies_file_path = cookies_file_path
        self._userAgent = userAgent
        self._proxy = proxy
        self._dataFB: Optional[dict] = None
        self._uid: str = ""
        self._name: str = ""
        self._listeners: dict[str, list[Callable]] = {}
        self._listening: bool = False
        self._mqtt_thread: Optional[threading.Thread] = None
        self._state: Optional[object] = None
        self._initial_state: Optional[object] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.logger = logging.getLogger("fbchat_v2")

        self.on_listening: Optional[Callable] = None
        self.on_message: Optional[Callable] = None

    @property
    def uid(self) -> str:
        return self._uid

    @property
    def name(self) -> str:
        return self._name

    @staticmethod
    def _fbstate_to_cookie_string(cookies_path: str) -> str:
        raw = json.loads(Path(cookies_path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = raw.get("cookies") or raw.get("state") or []
        pairs: list[str] = []
        seen: set[str] = set()
        for entry in raw:
            domain = str(entry.get("domain", ""))
            if "facebook" not in domain and "messenger" not in domain:
                continue
            name = str(entry.get("name", ""))
            value = str(entry.get("value", ""))
            if name and value and name not in seen:
                pairs.append(f"{name}={value}")
                seen.add(name)
        return "; ".join(pairs)

    def _resolve_fbstate_path(self) -> str:
        path = self._cookies_file_path
        if not os.path.isabs(path):
            path = str(Path(__file__).resolve().parent.parent / path)
        return path

    def bootstrap(self) -> dict:
        from fbchat_v2 import dataGetHome

        cookie_string = self._fbstate_to_cookie_string(self._resolve_fbstate_path())
        self._dataFB = dataGetHome(cookie_string)
        self._uid = str(self._dataFB.get("FacebookID", ""))
        return self._dataFB

    def event(self, event_type=None):
        if callable(event_type):
            func = event_type
            return self.event(None)(func)

        def decorator(func: Callable) -> Callable:
            resolved: str
            if event_type is None:
                name = func.__name__
                if not name.startswith("on_"):
                    raise ValueError(
                        f"Event handler {name} must start with 'on_'"
                    )
                resolved = name[3:]
            else:
                resolved = (
                    event_type.value
                    if hasattr(event_type, "value")
                    else str(event_type)
                )
            self._listeners.setdefault(resolved, []).append(func)
            return func

        return decorator

    def _dispatch(self, event_type: str, *args, **kwargs) -> None:
        for cb in self._listeners.get(event_type, []):
            try:
                if asyncio.iscoroutinefunction(cb):
                    if self._loop is not None and self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(cb(*args, **kwargs), self._loop)
                    else:
                        import warnings
                        warnings.warn(f"Cannot dispatch async {event_type} listener: no running loop")
                else:
                    cb(*args, **kwargs)
            except Exception as exc:
                print(f"[fbchat_v2] Error in {event_type} listener: {exc}")

    def add_listener(self, event_type, func) -> None:
        resolved = (
            event_type.value if hasattr(event_type, "value") else str(event_type)
        )
        self._listeners.setdefault(resolved, []).append(func)

    def _mqtt_loop(self) -> None:
        if not self._dataFB:
            return

        from fbchat_v2._messaging._listening import listeningEvent

        listener = listeningEvent(self._dataFB)
        orig_on_msg = listener.mqtt.on_message

        def wrapped_on_message(client, userdata, msg):
            if orig_on_msg:
                orig_on_msg(client, userdata, msg)
            try:
                j = json.loads(msg.payload.decode())
            except (UnicodeDecodeError, json.JSONDecodeError):
                return

            if j.get("deltas") is not None:
                try:
                    m = _FBV2Message.from_delta(j["deltas"][0])
                    if m is not None:
                        self._dispatch("message", m)
                except Exception as exc:
                    print(f"[fbchat_v2] Error processing delta: {exc}")

            if "syncToken" in j and "firstDeltaSeqId" in j:
                listener.syncToken = j["syncToken"]
                listener.lastSeqID = j["firstDeltaSeqId"]
            if "lastIssuedSeqId" in j:
                listener.lastSeqID = j["lastIssuedSeqId"]

        listener.mqtt.on_message = wrapped_on_message
        self._listening = True
        self._dispatch("listening")

        try:
            listener.connect_mqtt()
        except Exception as exc:
            print(f"[fbchat_v2] MQTT listener error: {exc}")
        finally:
            self._listening = False

    async def _runner(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.bootstrap()

        self._mqtt_thread = threading.Thread(
            target=self._mqtt_loop,
            daemon=True,
        )
        self._mqtt_thread.start()

        while self._listening:
            await asyncio.sleep(1)

    async def fetch_thread_list(self, limit: int = 100) -> list:
        return []

    async def typing(self, thread_id: str, on: bool, thread_type: object = None) -> None:
        pass

    async def send_message(
        self,
        text: Optional[str],
        thread_id: str,
        file_path: Optional[list[str]] = None,
        file_url: Optional[list[str]] = None,
        sticker: Optional[str] = None,
        reply_to_message: Optional[str] = None,
        files_ids: Optional[str] = None,
        mentions: Optional[list] = None,
    ) -> Optional[str]:
        if not self._dataFB:
            raise RuntimeError("Client not bootstrapped.")

        if file_path or file_url or files_ids:
            raise NotImplementedError(
                "File attachments require MURMUR_MESSENGER_BACKEND=fbchat_muqit"
            )

        from fbchat_v2._messaging._send import api as _SendAPI

        sender = _SendAPI()
        result = sender.send(
            dataFB=self._dataFB,
            contentSend=str(text or ""),
            threadID=thread_id,
        )

        if result.get("error"):
            desc = result.get("payload", {}).get("error-decription", "unknown")
            raise RuntimeError(f"fbchat_v2 send failed: {desc}")

        return str(result.get("payload", {}).get("messageID") or "") or None


def messenger_backend() -> str:
    return os.getenv("MURMUR_MESSENGER_BACKEND", "fbchat_muqit").strip().lower()


def is_fbchat_v2() -> bool:
    return messenger_backend() == "fbchat_v2"
