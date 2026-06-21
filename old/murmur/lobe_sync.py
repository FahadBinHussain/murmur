from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


class LobeSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class LobeSyncConfig:
    enabled: bool
    database_url: str
    user_email: str | None = None
    user_id: str | None = None
    agent_title: str = "Murmur"
    agent_slug: str = "murmur"
    session_title: str = "Murmur"
    session_slug: str = "murmur"
    topic_prefix: str = "Messenger"


@dataclass(frozen=True)
class LobeFileAttachment:
    name: str
    content_type: str
    content: bytes


@dataclass(frozen=True)
class LobeChatExchange:
    thread_id: str
    thread_name: str
    topic_title: str
    user_prompt: str
    assistant_answer: str
    source_message_id: str | None
    sender_id: str | None
    sender_name: str | None
    provider: str
    model: str
    gateway: str
    assistant_files: tuple[LobeFileAttachment, ...] = ()


class LobeSyncer:
    def __init__(self, config: LobeSyncConfig) -> None:
        self.config = config

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def status_label(self) -> str:
        if not self.config.enabled:
            return "off"
        if not self.config.database_url:
            return "enabled, missing LOBE_DATABASE_URL"
        if not self.config.user_id and not self.config.user_email:
            return "enabled, missing LOBE_SYNC_USER_EMAIL or LOBE_SYNC_USER_ID"
        return "enabled"

    async def sync_exchange(self, exchange: LobeChatExchange) -> None:
        if not self.config.enabled:
            return
        if not self.config.database_url:
            raise LobeSyncError("LOBE_DATABASE_URL is not configured")
        if not self.config.user_id and not self.config.user_email:
            raise LobeSyncError("LOBE_SYNC_USER_EMAIL or LOBE_SYNC_USER_ID is not configured")

        try:
            await asyncio.to_thread(self._sync_exchange, exchange)
        except LobeSyncError:
            raise
        except Exception as exc:
            raise LobeSyncError(self.redact_secret_text(str(exc))) from exc

    def _sync_exchange(self, exchange: LobeChatExchange) -> None:
        import psycopg

        with psycopg.connect(self.config.database_url) as connection:
            with connection.cursor() as cursor:
                user_id = self.resolve_user_id(cursor)
                agent_id = self.ensure_agent(cursor, user_id, exchange)
                session_id = self.ensure_session(cursor, user_id, exchange)
                self.ensure_agent_session_link(cursor, user_id, agent_id, session_id)
                topic_id = self.ensure_topic(cursor, user_id, agent_id, session_id, exchange)
                self.insert_messages(cursor, user_id, agent_id, session_id, topic_id, exchange)

    def resolve_user_id(self, cursor: Any) -> str:
        if self.config.user_id:
            cursor.execute("SELECT id FROM users WHERE id = %s LIMIT 1", (self.config.user_id,))
            row = cursor.fetchone()
            if row:
                return str(row[0])
            raise LobeSyncError("LOBE_SYNC_USER_ID does not match a Lobe user")

        email = (self.config.user_email or "").strip().lower()
        cursor.execute(
            """
            SELECT id
            FROM users
            WHERE lower(email) = %s OR lower(normalized_email) = %s
            LIMIT 1
            """,
            (email, email),
        )
        row = cursor.fetchone()
        if row:
            return str(row[0])
        raise LobeSyncError("LOBE_SYNC_USER_EMAIL does not match a Lobe user")

    def ensure_agent(self, cursor: Any, user_id: str, exchange: LobeChatExchange) -> str:
        agent_id = f"agt_{self.slug_id('murmur-agent', user_id, length=16)}"
        cursor.execute(
            """
            INSERT INTO agents (
              id, slug, title, description, tags, avatar, user_id, chat_config,
              params, model, provider, system_role, virtual,
              created_at, updated_at, accessed_at
            )
            VALUES (
              %s, %s, %s, %s, '[]'::jsonb, %s, %s, '{}'::jsonb,
              '{}'::jsonb, %s, %s, %s, false,
              NOW(), NOW(), NOW()
            )
            ON CONFLICT (slug, user_id) DO UPDATE
            SET title = EXCLUDED.title,
                description = EXCLUDED.description,
                avatar = EXCLUDED.avatar,
                model = EXCLUDED.model,
                provider = EXCLUDED.provider,
                updated_at = NOW(),
                accessed_at = NOW()
            RETURNING id
            """,
            (
                agent_id,
                self.config.agent_slug,
                self.config.agent_title,
                "Messenger conversations mirrored from Murmur.",
                "M",
                user_id,
                exchange.model,
                exchange.provider,
                "You are viewing a Messenger thread mirrored from Murmur.",
            ),
        )
        return str(cursor.fetchone()[0])

    def ensure_session(self, cursor: Any, user_id: str, exchange: LobeChatExchange) -> str:
        session_id = f"ssn_{self.slug_id('murmur-session', user_id, length=16)}"
        cursor.execute(
            """
            INSERT INTO sessions (
              id, slug, title, description, avatar, background_color, type, user_id,
              pinned, created_at, updated_at, accessed_at
            )
            VALUES (
              %s, %s, %s, %s, %s, %s, 'agent', %s,
              false, NOW(), NOW(), NOW()
            )
            ON CONFLICT (slug, user_id) DO UPDATE
            SET title = EXCLUDED.title,
                description = EXCLUDED.description,
                avatar = EXCLUDED.avatar,
                background_color = EXCLUDED.background_color,
                updated_at = NOW(),
                accessed_at = NOW()
            RETURNING id
            """,
            (
                session_id,
                self.config.session_slug,
                self.config.session_title,
                "Messenger conversations mirrored from Murmur.",
                "M",
                "#0ea5e9",
                user_id,
            ),
        )
        return str(cursor.fetchone()[0])

    def ensure_agent_session_link(
        self,
        cursor: Any,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO agents_to_sessions (agent_id, session_id, user_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (agent_id, session_id) DO NOTHING
            """,
            (agent_id, session_id, user_id),
        )

    def ensure_topic(
        self,
        cursor: Any,
        user_id: str,
        agent_id: str,
        session_id: str,
        exchange: LobeChatExchange,
    ) -> str:
        topic_id = f"tpc_{self.slug_id('murmur-topic', exchange.thread_id, length=24)}"
        client_id = f"murmur:messenger:{exchange.thread_id}"
        metadata = {
            "source": "murmur",
            "messengerThreadId": exchange.thread_id,
            "messengerThreadName": exchange.thread_name,
            "gateway": exchange.gateway,
        }
        cursor.execute(
            """
            INSERT INTO topics (
              id, title, favorite, session_id, content, agent_id, user_id,
              client_id, description, metadata, trigger, mode, status, model,
              provider, created_at, updated_at, accessed_at
            )
            VALUES (
              %s, %s, false, %s, %s, %s, %s,
              %s, %s, %s::jsonb, 'api', 'default', 'active', %s,
              %s, NOW(), NOW(), NOW()
            )
            ON CONFLICT (id) DO UPDATE
            SET title = EXCLUDED.title,
                session_id = EXCLUDED.session_id,
                content = EXCLUDED.content,
                agent_id = EXCLUDED.agent_id,
                description = EXCLUDED.description,
                metadata = COALESCE(topics.metadata, '{}'::jsonb) || EXCLUDED.metadata,
                status = 'active',
                model = EXCLUDED.model,
                provider = EXCLUDED.provider,
                updated_at = NOW(),
                accessed_at = NOW()
            RETURNING id
            """,
            (
                topic_id,
                exchange.topic_title,
                session_id,
                exchange.user_prompt[:2000],
                agent_id,
                user_id,
                client_id,
                f"Messenger thread: {exchange.thread_name}",
                self.json_param(metadata),
                exchange.model,
                exchange.provider,
            ),
        )
        return str(cursor.fetchone()[0])

    def insert_messages(
        self,
        cursor: Any,
        user_id: str,
        agent_id: str,
        session_id: str,
        topic_id: str,
        exchange: LobeChatExchange,
    ) -> None:
        user_message_id, assistant_message_id = self.message_ids(exchange)
        user_created_at = datetime.now(timezone.utc)
        assistant_created_at = user_created_at + timedelta(milliseconds=1)
        previous_assistant_id = self.previous_assistant_message_id(cursor, user_id, topic_id)

        base_metadata = {
            "source": "murmur",
            "messengerThreadId": exchange.thread_id,
            "messengerThreadName": exchange.thread_name,
            "sourceMessageId": exchange.source_message_id,
            "senderId": exchange.sender_id,
            "senderName": exchange.sender_name,
            "gateway": exchange.gateway,
        }
        self.upsert_message(
            cursor,
            message_id=user_message_id,
            role="user",
            content=exchange.user_prompt,
            user_id=user_id,
            session_id=session_id,
            topic_id=topic_id,
            agent_id=agent_id,
            parent_id=previous_assistant_id,
            target_id=agent_id,
            model=None,
            provider=None,
            metadata={**base_metadata, "mirroredRole": "user"},
            created_at=user_created_at,
        )
        self.upsert_message(
            cursor,
            message_id=assistant_message_id,
            role="assistant",
            content=exchange.assistant_answer,
            user_id=user_id,
            session_id=session_id,
            topic_id=topic_id,
            agent_id=agent_id,
            parent_id=user_message_id,
            target_id="user",
            model=exchange.model,
            provider=exchange.provider,
            metadata={**base_metadata, "mirroredRole": "assistant"},
            created_at=assistant_created_at,
        )
        if exchange.assistant_files:
            self.upsert_message_files(
                cursor,
                user_id=user_id,
                message_id=assistant_message_id,
                exchange=exchange,
                base_metadata=base_metadata,
            )

    def upsert_message_files(
        self,
        cursor: Any,
        *,
        user_id: str,
        message_id: str,
        exchange: LobeChatExchange,
        base_metadata: dict[str, Any],
    ) -> None:
        for index, attachment in enumerate(exchange.assistant_files, start=1):
            content = attachment.content
            file_hash = hashlib.sha256(content).hexdigest()
            content_type = attachment.content_type or "application/octet-stream"
            url = self.data_url(content, content_type)
            file_id = f"file_murmur_{self.slug_id(message_id, file_hash, str(index), length=24)}"
            metadata = {
                **base_metadata,
                "sourceMessageId": exchange.source_message_id,
                "mirroredRole": "assistant",
                "mirroredAttachment": "image",
                "originalName": attachment.name,
            }

            cursor.execute(
                """
                INSERT INTO global_files (
                  hash_id, file_type, size, url, metadata, creator,
                  created_at, accessed_at
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, NOW(), NOW())
                ON CONFLICT (hash_id) DO UPDATE
                SET file_type = EXCLUDED.file_type,
                    size = EXCLUDED.size,
                    url = EXCLUDED.url,
                    metadata = COALESCE(global_files.metadata, '{}'::jsonb) || EXCLUDED.metadata,
                    accessed_at = NOW()
                """,
                (
                    file_hash,
                    content_type,
                    len(content),
                    url,
                    self.json_param(metadata),
                    user_id,
                ),
            )
            cursor.execute(
                """
                INSERT INTO files (
                  id, user_id, file_type, name, size, url, metadata,
                  file_hash, client_id, source,
                  created_at, updated_at, accessed_at
                )
                VALUES (
                  %s, %s, %s, %s, %s, %s, %s::jsonb,
                  %s, %s, %s,
                  NOW(), NOW(), NOW()
                )
                ON CONFLICT (id) DO UPDATE
                SET file_type = EXCLUDED.file_type,
                    name = EXCLUDED.name,
                    size = EXCLUDED.size,
                    url = EXCLUDED.url,
                    metadata = COALESCE(files.metadata, '{}'::jsonb) || EXCLUDED.metadata,
                    file_hash = EXCLUDED.file_hash,
                    source = EXCLUDED.source,
                    updated_at = NOW(),
                    accessed_at = NOW()
                """,
                (
                    file_id,
                    user_id,
                    content_type,
                    attachment.name,
                    len(content),
                    url,
                    self.json_param(metadata),
                    file_hash,
                    file_id,
                    "murmur",
                ),
            )
            cursor.execute(
                """
                INSERT INTO messages_files (file_id, message_id, user_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (file_id, message_id) DO NOTHING
                """,
                (file_id, message_id, user_id),
            )

    def upsert_message(
        self,
        cursor: Any,
        *,
        message_id: str,
        role: str,
        content: str,
        user_id: str,
        session_id: str,
        topic_id: str,
        agent_id: str,
        parent_id: str | None,
        target_id: str | None,
        model: str | None,
        provider: str | None,
        metadata: dict[str, Any],
        created_at: datetime,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO messages (
              id, role, content, metadata, model, provider, client_id, user_id,
              session_id, topic_id, parent_id, agent_id, target_id,
              created_at, updated_at, accessed_at
            )
            VALUES (
              %s, %s, %s, %s::jsonb, %s, %s, %s, %s,
              %s, %s, %s, %s, %s,
              %s, %s, %s
            )
            ON CONFLICT (id) DO UPDATE
            SET content = EXCLUDED.content,
                metadata = COALESCE(messages.metadata, '{}'::jsonb) || EXCLUDED.metadata,
                model = EXCLUDED.model,
                provider = EXCLUDED.provider,
                parent_id = EXCLUDED.parent_id,
                agent_id = EXCLUDED.agent_id,
                target_id = EXCLUDED.target_id,
                updated_at = NOW(),
                accessed_at = NOW()
            """,
            (
                message_id,
                role,
                content,
                self.json_param(metadata),
                model,
                provider,
                message_id,
                user_id,
                session_id,
                topic_id,
                parent_id,
                agent_id,
                target_id,
                created_at,
                created_at,
                created_at,
            ),
        )

    def previous_assistant_message_id(
        self,
        cursor: Any,
        user_id: str,
        topic_id: str,
    ) -> str | None:
        cursor.execute(
            """
            SELECT id
            FROM messages
            WHERE user_id = %s
              AND topic_id = %s
              AND role = 'assistant'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (user_id, topic_id),
        )
        row = cursor.fetchone()
        return str(row[0]) if row else None

    def message_ids(self, exchange: LobeChatExchange) -> tuple[str, str]:
        source = exchange.source_message_id or (
            f"{exchange.thread_id}:{exchange.user_prompt}:{exchange.assistant_answer}"
        )
        digest = self.slug_id("murmur-message", exchange.thread_id, source, length=24)
        return f"msg_murmur_{digest}_u", f"msg_murmur_{digest}_a"

    def json_param(self, value: dict[str, Any]) -> str:
        import json

        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def data_url(self, content: bytes, content_type: str) -> str:
        import base64

        encoded = base64.b64encode(content).decode("ascii")
        return f"data:{content_type};base64,{encoded}"

    def slug_id(self, *parts: str, length: int = 24) -> str:
        raw = "\0".join(str(part) for part in parts)
        return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:length]

    def redact_secret_text(self, text: str) -> str:
        redacted = text
        if self.config.database_url:
            redacted = redacted.replace(self.config.database_url, "[redacted-database-url]")
        redacted = re.sub(
            r"(postgres(?:ql)?://[^:\s/@]+:)[^@\s]+(@)",
            r"\1[redacted]\2",
            redacted,
            flags=re.IGNORECASE,
        )
        return redacted
