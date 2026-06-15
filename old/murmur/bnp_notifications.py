import asyncio
import os
from dataclasses import dataclass
from typing import Any

import aiohttp


@dataclass(frozen=True)
class BnpNotificationSettings:
    outbox_url: str
    token: str
    thread_id: str
    poll_seconds: int
    claim_limit: int
    request_timeout_seconds: int

    @property
    def enabled(self) -> bool:
        return bool(self.outbox_url and self.token and self.thread_id)


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, ""))
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


def load_bnp_notification_settings() -> BnpNotificationSettings:
    base_url = os.getenv("BNP_MESSENGER_OUTBOX_URL", "").strip().rstrip("/")
    return BnpNotificationSettings(
        outbox_url=base_url,
        token=os.getenv("BNP_MESSENGER_OUTBOX_TOKEN", "").strip(),
        thread_id=os.getenv("BNP_MESSENGER_THREAD_ID", "").strip(),
        poll_seconds=env_int("BNP_MESSENGER_POLL_SECONDS", 30, 5, 3600),
        claim_limit=env_int("BNP_MESSENGER_CLAIM_LIMIT", 2, 1, 10),
        request_timeout_seconds=env_int("BNP_MESSENGER_REQUEST_TIMEOUT_SECONDS", 30, 5, 120),
    )


class BnpNotificationWorker:
    def __init__(self, client: Any, settings: BnpNotificationSettings | None = None) -> None:
        self.client = client
        self.settings = settings or load_bnp_notification_settings()

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    async def run(self) -> None:
        if not self.enabled:
            return

        print(
            "BNP Messenger notifications enabled for thread "
            f"{self.settings.thread_id}."
        )
        timeout = aiohttp.ClientTimeout(total=self.settings.request_timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                try:
                    await self.process_once(session)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(f"BNP Messenger notification worker failed: {exc}")

                await asyncio.sleep(self.settings.poll_seconds)

    async def process_once(self, session: aiohttp.ClientSession) -> None:
        for item in await self.claim_items(session):
            await self.send_item(session, item)

    async def claim_items(self, session: aiohttp.ClientSession) -> list[dict[str, Any]]:
        async with session.get(
            self.settings.outbox_url,
            headers=self.auth_headers(),
            params={"limit": str(self.settings.claim_limit)},
        ) as response:
            body = await self.read_json(response)
            if response.status >= 400:
                raise RuntimeError(
                    f"Daily BNP outbox claim failed with HTTP {response.status}: {body}"
                )

        items = body.get("items") if isinstance(body, dict) else None
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def send_item(self, session: aiohttp.ClientSession, item: dict[str, Any]) -> None:
        item_id = str(item.get("id") or "").strip()
        message = str(item.get("message") or "").strip()

        if not item_id or not message:
            return

        try:
            sent_message_id = await self.client.send_message(
                text=message,
                thread_id=self.settings.thread_id,
            )
        except Exception as exc:
            await self.ack_item(session, item_id, "failed", error=str(exc))
            return

        await self.ack_item(
            session,
            item_id,
            "sent",
            message_id=str(sent_message_id) if sent_message_id else None,
        )

    async def ack_item(
        self,
        session: aiohttp.ClientSession,
        item_id: str,
        status: str,
        *,
        message_id: str | None = None,
        error: str | None = None,
    ) -> None:
        payload = {"id": item_id, "status": status}
        if message_id:
            payload["messageId"] = message_id
        if error:
            payload["error"] = error[:2000]

        async with session.post(
            self.settings.outbox_url,
            headers=self.auth_headers(),
            json=payload,
        ) as response:
            body = await self.read_json(response)
            if response.status >= 400:
                raise RuntimeError(
                    f"Daily BNP outbox ack failed with HTTP {response.status}: {body}"
                )

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.token}"}

    async def read_json(self, response: aiohttp.ClientResponse) -> Any:
        try:
            return await response.json(content_type=None)
        except Exception:
            return await response.text()
