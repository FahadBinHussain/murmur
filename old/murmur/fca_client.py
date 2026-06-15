from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from asyncio.subprocess import PIPE
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


BRIDGE_DIR = Path(__file__).resolve().parent / "fca_unofficial"
GO_BRIDGE = BRIDGE_DIR / "messenger-bridge.exe"
NODE_BRIDGE = BRIDGE_DIR / "bridge.cjs"

REQUIRED_MESSENGER_COOKIES = {"xs", "c_user", "datr"}


@dataclass
class _FCAMessage:
    id: str
    text: str
    sender_id: str
    thread_id: str
    thread_type: int


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
        self._uid: str = ""
        self._name: str = ""
        self._listeners: dict[str, list[Callable]] = {}
        self._listening: bool = False
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._req_counter: int = 0
        self._use_go_bridge: bool = GO_BRIDGE.exists()
        self._cookies_tempfile: Optional[str] = None
        self.logger = logging.getLogger("fca_unofficial")
        self.on_listening: Optional[Callable] = None
        self.on_message: Optional[Callable] = None

    @property
    def uid(self) -> str:
        return self._uid

    @property
    def name(self) -> str:
        return self._name

    def _resolve_cookie_path(self, name: str = "") -> str:
        path = name or self._cookies_file_path
        if not os.path.isabs(path):
            path = str(Path(__file__).resolve().parent.parent / path)
        return path

    def _convert_cookies_to_flat(self, fbstate_path: str) -> str:
        with open(fbstate_path, "r") as f:
            cookies = json.load(f)

        flat = {}
        msg_cookies = {}
        fb_cookies = {}

        for c in cookies:
            if not isinstance(c, dict) or "name" not in c:
                continue
            name = c["name"]
            value = c.get("value", "")
            domain = c.get("domain", "")

            if ".messenger.com" in domain:
                msg_cookies[name] = value
            elif ".facebook.com" in domain:
                fb_cookies[name] = value

        src = msg_cookies if any(n in msg_cookies for n in ("xs", "c_user")) else fb_cookies
        missing = REQUIRED_MESSENGER_COOKIES - src.keys()
        if missing:
            raise RuntimeError(
                f"Cookies missing required Messenger fields: {', '.join(sorted(missing))}. "
                "Open https://messenger.com in a browser, click 'Continue as <you>', "
                "then export cookies to cookies.json."
            )

        for key in ("xs", "c_user", "datr", "sb", "fr", "oo"):
            if key in src:
                flat[key] = src[key]

        fd, path = tempfile.mkstemp(suffix=".json", prefix="messenger_cookies_")
        with os.fdopen(fd, "w") as f:
            json.dump(flat, f)
        self._cookies_tempfile = path
        return path

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
                        asyncio.run_coroutine_threadsafe(
                            cb(*args, **kwargs), self._loop
                        )
                    else:
                        import warnings

                        warnings.warn(
                            f"Cannot dispatch async {event_type} listener: "
                            "no running loop"
                        )
                else:
                    cb(*args, **kwargs)
            except Exception as exc:
                print(f"[fca] Error in {event_type} listener: {exc}")

    async def _send_cmd(self, cmd: dict) -> Any:
        self._req_counter += 1
        cmd_id = str(self._req_counter)
        cmd["id"] = cmd_id
        future: asyncio.Future = self._loop.create_future()
        self._pending[cmd_id] = future

        line = json.dumps(cmd, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))
        await self._proc.stdin.drain()

        result = await future
        if isinstance(result, Exception):
            raise result
        return result

    async def _read_stdout(self) -> None:
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8", errors="replace").strip())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            typ = msg.get("type", "")

            if typ == "response":
                rid = msg.get("id", "")
                fut = self._pending.pop(rid, None)
                if fut and not fut.done():
                    fut.set_result(msg.get("result"))

            elif typ == "sent":
                rid = msg.get("id", "")
                fut = self._pending.pop(rid, None)
                if fut and not fut.done():
                    fut.set_result({"message_id": msg.get("data", {}).get("otid", "")})

            elif typ == "uid":
                rid = msg.get("data", {}).get("id", "")
                fut = self._pending.pop(rid, None)
                if fut and not fut.done():
                    fut.set_result(msg.get("data", {}))

            elif typ == "error":
                if msg.get("id") == "login":
                    print(f"[fca] Login error: {msg.get('error', 'unknown')}")
                    self._listening = False
                    fut = self._pending.pop(self._req_counter, None)
                    if fut and not fut.done():
                        fut.set_exception(RuntimeError(f"fca login failed: {msg.get('error')}"))
                    break
                elif msg.get("id") == "listen":
                    print(f"[fca] Listen error: {msg.get('error', 'unknown')}")
                else:
                    rid = msg.get("id", "")
                    fut = self._pending.pop(rid, None)
                    if fut and not fut.done():
                        fut.set_exception(RuntimeError(msg.get("error", "unknown")))

            elif typ == "event":
                ev_name = msg.get("name", "")
                ev_data = msg.get("data")
                if ev_name == "message" and ev_data:
                    m = _FCAMessage(
                        id=ev_data.get("id", ""),
                        text=ev_data.get("text", ""),
                        sender_id=ev_data.get("sender_id", ""),
                        thread_id=ev_data.get("thread_id", ""),
                        thread_type=ev_data.get("thread_type", 0),
                    )
                    self._dispatch("message", m)
                elif ev_name == "ready" and ev_data:
                    self._uid = ev_data.get("uid", "")
                    self._name = ev_data.get("name", "")
                    self._listening = True
                    self._dispatch("listening")

            elif typ == "ready":
                data = msg.get("data") or {}
                self._uid = str(data.get("uid", ""))
                self._name = data.get("name", "")
                self._listening = True
                self._dispatch("listening")

            elif typ == "message":
                d = msg.get("data", msg)
                sender_id = str(d.get("sender_id", ""))
                if sender_id == self._uid:
                    continue
                m = _FCAMessage(
                    id=d.get("message_id", ""),
                    text=d.get("text", ""),
                    sender_id=sender_id,
                    thread_id=str(d.get("thread_id", "")),
                    thread_type=0,
                )
                self._dispatch("message", m)

    async def _runner(self) -> None:
        self._loop = asyncio.get_running_loop()

        if self._use_go_bridge:
            flat_cookies = self._resolve_cookie_path("messenger-cookies.json")
            if os.path.isfile(flat_cookies):
                cookie_path = flat_cookies
            else:
                cookie_path = self._convert_cookies_to_flat(self._resolve_cookie_path())
            self._proc = await asyncio.create_subprocess_exec(
                str(GO_BRIDGE),
                "--cookies", cookie_path,
                stdin=PIPE,
                stdout=PIPE,
                stderr=sys.stderr,
            )
        else:
            node_exe = shutil.which("node")
            if not node_exe:
                raise RuntimeError("Node.js not found on PATH")
            cookie_path = self._resolve_cookie_path()
            self._proc = await asyncio.create_subprocess_exec(
                node_exe,
                str(NODE_BRIDGE),
                cookie_path,
                stdin=PIPE,
                stdout=PIPE,
                stderr=sys.stderr,
                cwd=str(BRIDGE_DIR),
            )

        try:
            await self._read_stdout()
        finally:
            if self._cookies_tempfile:
                try:
                    os.unlink(self._cookies_tempfile)
                except OSError:
                    pass

    async def fetch_thread_list(self, limit: int = 100) -> list:
        result = await self._send_cmd(
            {"type": "fetch_thread_list", "limit": limit}
        )
        return result.get("threads", []) if result else []

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
        if file_path or file_url or files_ids:
            raise NotImplementedError(
                "File attachments require MURMUR_MESSENGER_BACKEND=fbchat_muqit"
            )

        if self._use_go_bridge:
            cmd = {
                "type": "send_message",
                "data": {
                    "thread_id": int(thread_id),
                    "text": str(text or ""),
                },
            }
        else:
            cmd = {
                "type": "send_message",
                "text": str(text or ""),
                "thread_id": str(thread_id),
            }

        result = await self._send_cmd(cmd)
        if isinstance(result, dict):
            return result.get("message_id")
        return None


def messenger_backend() -> str:
    return os.getenv("MURMUR_MESSENGER_BACKEND", "fbchat_muqit").strip().lower()


def is_fca_unofficial() -> bool:
    return messenger_backend() == "fca_unofficial"
