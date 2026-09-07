"""Persistence helpers for the optional Labworks support/operations layer.

The leveling bot uses one SQLite connection.  This module keeps all support
state behind a small repository so that forum events, the reminder loop, and
configuration views do not each invent their own SQL or transaction rules.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any


SUPPORT_STATES = ("unanswered", "open", "waiting", "solved")


@dataclass(frozen=True)
class SupportSettings:
    guild_id: int
    enabled: bool
    forum_channel_id: int
    waiting_delay_seconds: int
    reminder_after_hours: float
    hard_reminder_after_hours: float
    close_after_reminder_hours: float
    solved_archive_after_hours: float


@dataclass(frozen=True)
class SupportPost:
    guild_id: int
    thread_id: int
    creator_id: int
    state: str
    last_message_id: int | None
    last_author_id: int | None
    last_message_at: float
    waiting_since: float | None
    reminder_stage: int
    reminder_message_id: int | None
    reminder_sent_at: float | None
    close_at: float | None
    solved_at: float | None
    closed_at: float | None
    incomplete_prompt_sent: bool
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class CannedResponse:
    guild_id: int
    name: str
    content: str
    creator_id: int
    created_at: float
    uses: int


class SupportStore:
    """Repository for support settings, post state, and operations data."""

    _SETTING_COLUMNS = {
        "enabled",
        "forum_channel_id",
        "waiting_delay_seconds",
        "reminder_after_hours",
        "hard_reminder_after_hours",
        "close_after_reminder_hours",
        "solved_archive_after_hours",
    }

    _POST_COLUMNS = {
        "creator_id",
        "state",
        "last_message_id",
        "last_author_id",
        "last_message_at",
        "waiting_since",
        "reminder_stage",
        "reminder_message_id",
        "reminder_sent_at",
        "close_at",
        "solved_at",
        "closed_at",
        "incomplete_prompt_sent",
        "updated_at",
    }

    def __init__(self, bot: Any):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    @property
    def db_lock(self):
        return self.bot.db_lock

    async def ensure_schema(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS support_settings (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                forum_channel_id INTEGER NOT NULL DEFAULT 0,
                waiting_delay_seconds INTEGER NOT NULL DEFAULT 600,
                reminder_after_hours REAL NOT NULL DEFAULT 24,
                hard_reminder_after_hours REAL NOT NULL DEFAULT 72,
                close_after_reminder_hours REAL NOT NULL DEFAULT 24,
                solved_archive_after_hours REAL NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS support_staff_roles (
                guild_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, role_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS support_lifecycle_tags (
                guild_id INTEGER NOT NULL,
                state TEXT NOT NULL,
                tag_id INTEGER NOT NULL,
                managed_by_bot INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, state),
                UNIQUE (guild_id, tag_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS support_posts (
                guild_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'unanswered',
                last_message_id INTEGER,
                last_author_id INTEGER,
                last_message_at REAL NOT NULL DEFAULT 0,
                waiting_since REAL,
                reminder_stage INTEGER NOT NULL DEFAULT 0,
                reminder_message_id INTEGER,
                reminder_sent_at REAL,
                close_at REAL,
                solved_at REAL,
                closed_at REAL,
                incomplete_prompt_sent INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (guild_id, thread_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS canned_responses (
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                creator_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                uses INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, name)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS channel_lock_backups (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                overwrites_json TEXT NOT NULL,
                locked_at REAL NOT NULL,
                locked_by INTEGER NOT NULL,
                PRIMARY KEY (guild_id, channel_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS bot_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                actor_id INTEGER,
                thread_id INTEGER,
                action TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                correlation_id TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """,
        )
        async with self.db_lock:
            for statement in statements:
                await self.db.execute(statement)
            await self.db.commit()

    async def ensure_guild(self, guild_id: int) -> None:
        async with self.db_lock:
            await self.db.execute(
                "INSERT OR IGNORE INTO support_settings (guild_id, updated_at) VALUES (?, ?)",
                (guild_id, time.time()),
            )
            await self.db.commit()

    @staticmethod
    def _settings_from_row(row: tuple[Any, ...]) -> SupportSettings:
        return SupportSettings(
            guild_id=int(row[0]),
            enabled=bool(row[1]),
            forum_channel_id=int(row[2] or 0),
            waiting_delay_seconds=int(row[3]),
            reminder_after_hours=float(row[4]),
            hard_reminder_after_hours=float(row[5]),
            close_after_reminder_hours=float(row[6]),
            solved_archive_after_hours=float(row[7]),
        )

    async def get_settings(self, guild_id: int) -> SupportSettings:
        await self.ensure_guild(guild_id)
        async with self.db.execute(
            """
            SELECT guild_id, enabled, forum_channel_id, waiting_delay_seconds,
                   reminder_after_hours, hard_reminder_after_hours,
                   close_after_reminder_hours, solved_archive_after_hours
            FROM support_settings WHERE guild_id=?
            """,
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return self._settings_from_row(row)

    async def update_settings(self, guild_id: int, **values: Any) -> SupportSettings:
        unknown = set(values) - self._SETTING_COLUMNS
        if unknown:
            raise ValueError(f"Unknown support setting(s): {', '.join(sorted(unknown))}")
        if not values:
            return await self.get_settings(guild_id)

        await self.ensure_guild(guild_id)
        assignments = ", ".join(f"{column}=?" for column in values)
        parameters = [values[column] for column in values]
        parameters.extend((time.time(), guild_id))
        async with self.db_lock:
            await self.db.execute(
                f"UPDATE support_settings SET {assignments}, updated_at=? WHERE guild_id=?",
                parameters,
            )
            await self.db.commit()
        return await self.get_settings(guild_id)

    async def get_staff_roles(self, guild_id: int) -> set[int]:
        async with self.db.execute(
            "SELECT role_id FROM support_staff_roles WHERE guild_id=? ORDER BY role_id",
            (guild_id,),
        ) as cursor:
            return {int(row[0]) for row in await cursor.fetchall()}

    async def replace_staff_roles(self, guild_id: int, role_ids: set[int]) -> None:
        async with self.db_lock:
            await self.db.execute("DELETE FROM support_staff_roles WHERE guild_id=?", (guild_id,))
            await self.db.executemany(
                "INSERT INTO support_staff_roles (guild_id, role_id) VALUES (?, ?)",
                [(guild_id, int(role_id)) for role_id in sorted(role_ids)],
            )
            await self.db.commit()

    async def get_tag_bindings(self, guild_id: int) -> dict[str, tuple[int, bool]]:
        async with self.db.execute(
            "SELECT state, tag_id, managed_by_bot FROM support_lifecycle_tags WHERE guild_id=?",
            (guild_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return {str(state): (int(tag_id), bool(managed)) for state, tag_id, managed in rows}

    async def set_tag_binding(self, guild_id: int, state: str, tag_id: int, managed_by_bot: bool) -> None:
        if state not in SUPPORT_STATES:
            raise ValueError(f"Unsupported support state: {state}")
        async with self.db_lock:
            await self.db.execute(
                """
                INSERT INTO support_lifecycle_tags (guild_id, state, tag_id, managed_by_bot)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, state) DO UPDATE SET
                    tag_id=excluded.tag_id, managed_by_bot=excluded.managed_by_bot
                """,
                (guild_id, state, tag_id, int(managed_by_bot)),
            )
            await self.db.commit()

    @staticmethod
    def _post_from_row(row: tuple[Any, ...]) -> SupportPost:
        return SupportPost(
            guild_id=int(row[0]),
            thread_id=int(row[1]),
            creator_id=int(row[2]),
            state=str(row[3]),
            last_message_id=int(row[4]) if row[4] is not None else None,
            last_author_id=int(row[5]) if row[5] is not None else None,
            last_message_at=float(row[6] or 0),
            waiting_since=float(row[7]) if row[7] is not None else None,
            reminder_stage=int(row[8] or 0),
            reminder_message_id=int(row[9]) if row[9] is not None else None,
            reminder_sent_at=float(row[10]) if row[10] is not None else None,
            close_at=float(row[11]) if row[11] is not None else None,
            solved_at=float(row[12]) if row[12] is not None else None,
            closed_at=float(row[13]) if row[13] is not None else None,
            incomplete_prompt_sent=bool(row[14]),
            created_at=float(row[15]),
            updated_at=float(row[16]),
        )

    _POST_SELECT = (
        "SELECT guild_id, thread_id, creator_id, state, last_message_id, "
        "last_author_id, last_message_at, waiting_since, reminder_stage, "
        "reminder_message_id, reminder_sent_at, close_at, solved_at, closed_at, "
        "incomplete_prompt_sent, created_at, updated_at FROM support_posts"
    )

    async def get_post(self, guild_id: int, thread_id: int) -> SupportPost | None:
        async with self.db.execute(
            f"{self._POST_SELECT} WHERE guild_id=? AND thread_id=?",
            (guild_id, thread_id),
        ) as cursor:
            row = await cursor.fetchone()
        return self._post_from_row(row) if row else None

    async def ensure_post(
        self,
        guild_id: int,
        thread_id: int,
        creator_id: int,
        last_message_id: int | None,
        last_author_id: int | None,
        last_message_at: float,
        *,
        state: str = "unanswered",
        created_at: float | None = None,
    ) -> SupportPost:
        if state not in SUPPORT_STATES:
            raise ValueError(f"Unsupported support state: {state}")
        now = time.time()
        created_at = now if created_at is None else float(created_at)
        async with self.db_lock:
            await self.db.execute(
                """
                INSERT OR IGNORE INTO support_posts
                    (guild_id, thread_id, creator_id, state, last_message_id,
                     last_author_id, last_message_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (guild_id, thread_id, creator_id, state, last_message_id,
                 last_author_id, last_message_at, created_at, now),
            )
            await self.db.commit()
        return await self.get_post(guild_id, thread_id)  # type: ignore[return-value]

    async def update_post(self, guild_id: int, thread_id: int, **values: Any) -> SupportPost | None:
        unknown = set(values) - self._POST_COLUMNS
        if unknown:
            raise ValueError(f"Unknown support post field(s): {', '.join(sorted(unknown))}")
        if "state" in values and values["state"] not in SUPPORT_STATES:
            raise ValueError(f"Unsupported support state: {values['state']}")
        if not values:
            return await self.get_post(guild_id, thread_id)

        values = dict(values)
        values["updated_at"] = time.time()
        assignments = ", ".join(f"{column}=?" for column in values)
        parameters = [values[column] for column in values]
        parameters.extend((guild_id, thread_id))
        async with self.db_lock:
            await self.db.execute(
                f"UPDATE support_posts SET {assignments} WHERE guild_id=? AND thread_id=?",
                parameters,
            )
            await self.db.commit()
        return await self.get_post(guild_id, thread_id)

    async def claim_incomplete_prompt(self, guild_id: int, thread_id: int) -> bool:
        """Atomically claim the one-shot incomplete-post prompt."""
        async with self.db_lock:
            cursor = await self.db.execute(
                "UPDATE support_posts SET incomplete_prompt_sent=1, updated_at=? WHERE guild_id=? AND thread_id=? AND incomplete_prompt_sent=0",
                (time.time(), guild_id, thread_id),
            )
            await self.db.commit()
            return cursor.rowcount == 1

    async def list_posts(self, guild_id: int, *, include_closed: bool = False) -> list[SupportPost]:
        query = f"{self._POST_SELECT} WHERE guild_id=?"
        parameters: list[Any] = [guild_id]
        if not include_closed:
            query += " AND closed_at IS NULL"
        query += " ORDER BY updated_at ASC"
        async with self.db.execute(query, parameters) as cursor:
            rows = await cursor.fetchall()
        return [self._post_from_row(row) for row in rows]

    async def find_response(self, guild_id: int, name: str) -> CannedResponse | None:
        async with self.db.execute(
            "SELECT guild_id, name, content, creator_id, created_at, uses FROM canned_responses WHERE guild_id=? AND lower(name)=lower(?)",
            (guild_id, name),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return CannedResponse(
            guild_id=int(row[0]), name=str(row[1]), content=str(row[2]),
            creator_id=int(row[3]), created_at=float(row[4]), uses=int(row[5]),
        )

    async def list_responses(self, guild_id: int) -> list[CannedResponse]:
        async with self.db.execute(
            "SELECT guild_id, name, content, creator_id, created_at, uses FROM canned_responses WHERE guild_id=? ORDER BY uses DESC, lower(name) ASC LIMIT 100",
            (guild_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            CannedResponse(
                guild_id=int(row[0]), name=str(row[1]), content=str(row[2]),
                creator_id=int(row[3]), created_at=float(row[4]), uses=int(row[5]),
            )
            for row in rows
        ]

    async def create_response(self, guild_id: int, name: str, content: str, creator_id: int) -> None:
        now = time.time()
        async with self.db_lock:
            async with self.db.execute(
                "SELECT 1 FROM canned_responses WHERE guild_id=? AND lower(name)=lower(?)",
                (guild_id, name),
            ) as cursor:
                if await cursor.fetchone():
                    raise ValueError("A canned response with that name already exists.")
            await self.db.execute(
                "INSERT INTO canned_responses (guild_id, name, content, creator_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (guild_id, name, content, creator_id, now),
            )
            await self.db.commit()

    async def update_response(self, guild_id: int, original_name: str, new_name: str, content: str) -> None:
        async with self.db_lock:
            async with self.db.execute(
                "SELECT 1 FROM canned_responses WHERE guild_id=? AND lower(name)=lower(?) AND lower(name)<>lower(?)",
                (guild_id, new_name, original_name),
            ) as cursor:
                if await cursor.fetchone():
                    raise ValueError("A canned response with that name already exists.")
            await self.db.execute(
                "UPDATE canned_responses SET name=?, content=? WHERE guild_id=? AND lower(name)=lower(?)",
                (new_name, content, guild_id, original_name),
            )
            await self.db.commit()

    async def delete_response(self, guild_id: int, name: str) -> None:
        async with self.db_lock:
            await self.db.execute(
                "DELETE FROM canned_responses WHERE guild_id=? AND lower(name)=lower(?)",
                (guild_id, name),
            )
            await self.db.commit()

    async def increment_response_uses(self, guild_id: int, name: str) -> None:
        async with self.db_lock:
            await self.db.execute(
                "UPDATE canned_responses SET uses=uses+1 WHERE guild_id=? AND lower(name)=lower(?)",
                (guild_id, name),
            )
            await self.db.commit()

    async def save_lock_backup(
        self,
        guild_id: int,
        channel_id: int,
        overwrites: list[dict[str, int]],
        locked_by: int,
    ) -> bool:
        async with self.db_lock:
            cursor = await self.db.execute(
                """
                INSERT OR IGNORE INTO channel_lock_backups (guild_id, channel_id, overwrites_json, locked_at, locked_by)
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, channel_id, json.dumps(overwrites), time.time(), locked_by),
            )
            await self.db.commit()
            return cursor.rowcount == 1

    async def get_lock_backup(self, guild_id: int, channel_id: int) -> list[dict[str, int]] | None:
        async with self.db.execute(
            "SELECT overwrites_json FROM channel_lock_backups WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id),
        ) as cursor:
            row = await cursor.fetchone()
        return json.loads(row[0]) if row else None

    async def delete_lock_backup(self, guild_id: int, channel_id: int) -> None:
        async with self.db_lock:
            await self.db.execute(
                "DELETE FROM channel_lock_backups WHERE guild_id=? AND channel_id=?",
                (guild_id, channel_id),
            )
            await self.db.commit()

    async def audit(
        self,
        guild_id: int,
        action: str,
        *,
        actor_id: int | None = None,
        thread_id: int | None = None,
        details: str = "",
        correlation_id: str,
    ) -> None:
        async with self.db_lock:
            await self.db.execute(
                """
                INSERT INTO bot_audit_log
                    (guild_id, actor_id, thread_id, action, details, correlation_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (guild_id, actor_id, thread_id, action, details[:500], correlation_id, time.time()),
            )
            await self.db.commit()

    async def activity_summary(self, guild_id: int, since: float) -> list[tuple[str, int]]:
        async with self.db.execute(
            "SELECT action, COUNT(*) FROM bot_audit_log WHERE guild_id=? AND created_at>=? GROUP BY action ORDER BY COUNT(*) DESC",
            (guild_id, since),
        ) as cursor:
            return [(str(row[0]), int(row[1])) for row in await cursor.fetchall()]

    async def recent_activity(self, guild_id: int, limit: int = 15) -> list[tuple[Any, ...]]:
        async with self.db.execute(
            "SELECT actor_id, thread_id, action, details, created_at, correlation_id FROM bot_audit_log WHERE guild_id=? ORDER BY id DESC LIMIT ?",
            (guild_id, limit),
        ) as cursor:
            return await cursor.fetchall()
