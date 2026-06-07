from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runtime_state import (
    RuntimeStateMissing,
    RuntimeStateNotConfigured,
    decode_json_state,
    encode_plain_json_state,
    fetch_runtime_state,
    state_database_url,
    upsert_runtime_state,
)


STATE_KEY = "automation_notifications"
PENDING_STATUSES = {"pending", "failed"}


@dataclass(frozen=True)
class AutomationNotificationSettings:
    enabled: bool
    poll_seconds: int
    claim_limit: int
    max_attempts: int


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, ""))
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


def automation_notification_token() -> str:
    return os.getenv("MURMUR_AUTOMATION_NOTIFICATION_TOKEN", "").strip()


def automation_notification_endpoint_path() -> str:
    path = (
        os.getenv("MURMUR_AUTOMATION_NOTIFICATION_PATH")
        or "/api/automation/notifications"
    ).strip()
    return "/" + path.strip("/")


def automation_notification_default_thread_id() -> str:
    return os.getenv("MURMUR_AUTOMATION_NOTIFICATION_DEFAULT_THREAD_ID", "").strip()


def automation_notification_queue_path() -> Path:
    return Path(
        os.getenv(
            "MURMUR_AUTOMATION_NOTIFICATION_QUEUE_PATH",
            "/tmp/murmur-automation-notifications.json",
        )
    )


def notification_settings() -> AutomationNotificationSettings:
    return AutomationNotificationSettings(
        enabled=env_bool("MURMUR_AUTOMATION_NOTIFICATIONS", True)
        and bool(automation_notification_token()),
        poll_seconds=env_int("MURMUR_AUTOMATION_NOTIFICATION_POLL_SECONDS", 10, 2, 3600),
        claim_limit=env_int("MURMUR_AUTOMATION_NOTIFICATION_CLAIM_LIMIT", 5, 1, 25),
        max_attempts=env_int("MURMUR_AUTOMATION_NOTIFICATION_MAX_ATTEMPTS", 12, 1, 100),
    )


def max_message_chars() -> int:
    return env_int(
        "MURMUR_AUTOMATION_NOTIFICATION_MAX_MESSAGE_CHARS",
        3500,
        200,
        12000,
    )


def now_seconds() -> int:
    return int(time.time())


def blank_state() -> dict[str, Any]:
    return {"version": 1, "items": []}


def normalize_state(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return blank_state()
    items = payload.get("items")
    if not isinstance(items, list):
        items = []
    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        message = str(item.get("message") or "").strip()
        thread_id = str(item.get("thread_id") or "").strip()
        if not item_id or not message or not thread_id:
            continue
        normalized = dict(item)
        normalized["id"] = item_id
        normalized["message"] = message
        normalized["thread_id"] = thread_id
        normalized["status"] = str(item.get("status") or "pending")
        normalized["attempts"] = int(item.get("attempts") or 0)
        normalized["created_at"] = int(item.get("created_at") or now_seconds())
        normalized["updated_at"] = int(item.get("updated_at") or normalized["created_at"])
        normalized["next_attempt_at"] = int(item.get("next_attempt_at") or 0)
        cleaned.append(normalized)
    return {"version": 1, "items": cleaned}


def load_notification_state() -> tuple[dict[str, Any], str]:
    if state_database_url():
        try:
            value, encoding = fetch_runtime_state(STATE_KEY)
            return normalize_state(json.loads(decode_json_state(value, encoding))), "db"
        except RuntimeStateMissing:
            return blank_state(), "db"
        except RuntimeStateNotConfigured:
            pass

    path = automation_notification_queue_path()
    try:
        return normalize_state(json.loads(path.read_text(encoding="utf-8"))), "file"
    except (OSError, json.JSONDecodeError):
        return blank_state(), "file"


def prune_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = now_seconds()
    sent_ttl = env_int(
        "MURMUR_AUTOMATION_NOTIFICATION_SENT_TTL_SECONDS",
        604800,
        3600,
        2678400,
    )
    return [
        item
        for item in items
        if item.get("status") != "sent"
        or now - int(item.get("updated_at") or now) <= sent_ttl
    ]


def save_notification_state(state: dict[str, Any], backend: str | None = None) -> str:
    state = normalize_state({"items": prune_items(list(state.get("items") or []))})
    if backend == "db" or (backend is None and state_database_url()):
        value, encoding = encode_plain_json_state(state)
        upsert_runtime_state(STATE_KEY, value, encoding)
        return "db"

    path = automation_notification_queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)
    return "file"


def item_identity(source: str, thread_id: str, dedupe_key: str, message: str) -> str:
    seed = f"{now_seconds()}:{source}:{thread_id}:{dedupe_key}:{message}:{os.urandom(8).hex()}"
    return "ntf_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def clean_text(value: object, max_length: int) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()[:max_length]


def enqueue_notification(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("notification payload must be a JSON object")

    source = clean_text(payload.get("source") or "automation", 80) or "automation"
    thread_id = clean_text(
        payload.get("thread_id")
        or payload.get("threadId")
        or automation_notification_default_thread_id(),
        80,
    )
    title = clean_text(payload.get("title"), 200)
    message = clean_text(payload.get("message") or payload.get("text"), max_message_chars())
    dedupe_key = clean_text(payload.get("dedupe_key") or payload.get("dedupeKey"), 200)

    if title and message:
        message = f"{title}\n\n{message}"
    elif title:
        message = title

    if not thread_id:
        raise ValueError("thread_id is required when no default thread is configured")
    if not message:
        raise ValueError("message is required")

    state, backend = load_notification_state()
    items = list(state.get("items") or [])
    if dedupe_key:
        for item in items:
            if (
                item.get("dedupe_key") == dedupe_key
                and item.get("thread_id") == thread_id
                and item.get("source") == source
                and item.get("status") != "dead"
            ):
                return {
                    "id": item["id"],
                    "status": item.get("status"),
                    "queued": False,
                    "duplicate": True,
                    "backend": backend,
                }

    timestamp = now_seconds()
    item = {
        "id": item_identity(source, thread_id, dedupe_key, message),
        "source": source,
        "thread_id": thread_id,
        "message": message,
        "dedupe_key": dedupe_key,
        "status": "pending",
        "attempts": 0,
        "created_at": timestamp,
        "updated_at": timestamp,
        "next_attempt_at": 0,
    }
    items.append(item)
    state["items"] = items
    backend = save_notification_state(state, backend)
    return {
        "id": item["id"],
        "status": "pending",
        "queued": True,
        "duplicate": False,
        "backend": backend,
    }


def retry_delay_seconds(attempts: int) -> int:
    base = env_int("MURMUR_AUTOMATION_NOTIFICATION_RETRY_BASE_SECONDS", 30, 5, 3600)
    maximum = env_int("MURMUR_AUTOMATION_NOTIFICATION_RETRY_MAX_SECONDS", 3600, 30, 86400)
    return min(maximum, base * (2 ** max(0, attempts - 1)))


def claim_notifications(limit: int, max_attempts: int) -> list[dict[str, Any]]:
    state, backend = load_notification_state()
    items = list(state.get("items") or [])
    now = now_seconds()
    stale_seconds = env_int(
        "MURMUR_AUTOMATION_NOTIFICATION_SENDING_STALE_SECONDS",
        600,
        60,
        86400,
    )
    claimed = []

    for item in items:
        if len(claimed) >= limit:
            break
        status = str(item.get("status") or "")
        attempts = int(item.get("attempts") or 0)
        updated_at = int(item.get("updated_at") or 0)
        next_attempt_at = int(item.get("next_attempt_at") or 0)
        eligible = (
            status == "pending"
            or (status == "failed" and next_attempt_at <= now)
            or (status == "sending" and now - updated_at >= stale_seconds)
        )
        if not eligible:
            continue
        if attempts >= max_attempts:
            item["status"] = "dead"
            item["updated_at"] = now
            continue
        item["status"] = "sending"
        item["attempts"] = attempts + 1
        item["updated_at"] = now
        claimed.append(dict(item))

    if claimed:
        state["items"] = items
        save_notification_state(state, backend)
    return claimed


def finish_notification(
    item_id: str,
    status: str,
    *,
    message_id: str | None = None,
    error: str | None = None,
    max_attempts: int | None = None,
) -> None:
    state, backend = load_notification_state()
    items = list(state.get("items") or [])
    now = now_seconds()
    for item in items:
        if item.get("id") != item_id:
            continue
        attempts = int(item.get("attempts") or 0)
        item["updated_at"] = now
        if status == "sent":
            item["status"] = "sent"
            item["sent_at"] = now
            if message_id:
                item["message_id"] = str(message_id)
            item.pop("last_error", None)
            item["next_attempt_at"] = 0
        else:
            if max_attempts is not None and attempts >= max_attempts:
                item["status"] = "dead"
                item["next_attempt_at"] = 0
            else:
                item["status"] = "failed"
                item["next_attempt_at"] = now + retry_delay_seconds(attempts)
            item["last_error"] = str(error or "send failed")[:2000]
        break
    state["items"] = items
    save_notification_state(state, backend)


def notification_summary() -> dict[str, Any]:
    state, backend = load_notification_state()
    counts: dict[str, int] = {}
    for item in state.get("items") or []:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {"backend": backend, "counts": counts}


class AutomationNotificationWorker:
    def __init__(
        self,
        client: Any,
        settings: AutomationNotificationSettings | None = None,
    ) -> None:
        self.client = client
        self.settings = settings or notification_settings()

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    async def run(self) -> None:
        if not self.enabled:
            return

        print("Automation Messenger notifications enabled.")
        while True:
            try:
                await self.process_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"Automation Messenger notification worker failed: {exc}")
            await asyncio.sleep(self.settings.poll_seconds)

    async def process_once(self) -> None:
        for item in claim_notifications(
            self.settings.claim_limit,
            self.settings.max_attempts,
        ):
            await self.send_item(item)

    async def send_item(self, item: dict[str, Any]) -> None:
        item_id = str(item.get("id") or "")
        thread_id = str(item.get("thread_id") or "")
        message = str(item.get("message") or "")
        try:
            sent_message_id = await self.client.send_message(
                text=message,
                thread_id=thread_id,
            )
        except Exception as exc:
            finish_notification(
                item_id,
                "failed",
                error=str(exc),
                max_attempts=self.settings.max_attempts,
            )
            return

        finish_notification(
            item_id,
            "sent",
            message_id=str(sent_message_id) if sent_message_id else None,
            max_attempts=self.settings.max_attempts,
        )
