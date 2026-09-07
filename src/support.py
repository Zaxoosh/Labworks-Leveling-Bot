"""Support-forum lifecycle automation for Labworks.

The support feature is deliberately opt-in per guild.  All durable state is
kept in :mod:`support_store`, while this module owns Discord-facing behavior:
forum tags, lifecycle transitions, reminders, and the small set of member
commands used to manage a post.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
import time
import uuid
from typing import Any

import discord
from discord import app_commands, ui
from discord.ext import commands, tasks

try:  # Works both as ``python src/main.py`` and ``from src import main``.
    from .support_store import SUPPORT_STATES, SupportPost, SupportSettings, SupportStore
except ImportError:  # pragma: no cover - exercised by the production entrypoint.
    from support_store import SUPPORT_STATES, SupportPost, SupportSettings, SupportStore


logger = logging.getLogger(__name__)

CANONICAL_TAG_NAMES = {
    "unanswered": "Unanswered",
    "open": "Open",
    "waiting": "Waiting for Reply",
    "solved": "Solved",
}

TAG_ALIASES = {
    "unanswered": {"unanswered", "unanswered question", "needs answer"},
    "open": {"open", "in progress", "not solved", "not closed"},
    "waiting": {"waiting", "waiting for reply", "waiting reply", "awaiting reply", "awaiting response"},
    "solved": {"solved", "closed", "complete", "completed"},
}

INCOMPLETE_POST_LIMIT = 40
REMINDER_LOOP_MINUTES = 15


def normalize_tag_name(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", " ", str(value).lower())
    return re.sub(r"\s+", " ", value).strip()


def timestamp(value: Any, fallback: float | None = None) -> float:
    if value is None:
        return time.time() if fallback is None else fallback
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value.timestamp()
    return time.time() if fallback is None else fallback


def snowflake_timestamp(identifier: int) -> float:
    try:
        return discord.utils.snowflake_time(int(identifier)).timestamp()
    except (TypeError, ValueError, OverflowError):
        return time.time()


def is_forum_channel(channel: Any) -> bool:
    return isinstance(channel, discord.ForumChannel) or getattr(channel, "type", None) == discord.ChannelType.forum or (
        channel is not None
        and hasattr(channel, "available_tags")
        and hasattr(channel, "create_tag")
    )


def is_thread_channel(channel: Any) -> bool:
    return isinstance(channel, discord.Thread) or (
        channel is not None
        and hasattr(channel, "parent_id")
        and hasattr(channel, "owner_id")
    )


def _mention(user_id: int) -> str:
    return f"<@{int(user_id)}>"


async def send_interaction_message(
    interaction: discord.Interaction,
    content: str,
    *,
    ephemeral: bool = True,
    embed: discord.Embed | None = None,
    view: ui.View | None = None,
    allowed_mentions: discord.AllowedMentions | None = None,
) -> None:
    """Reply once, also making the helper convenient for small test doubles."""

    response = interaction.response
    mention_policy = allowed_mentions if allowed_mentions is not None else discord.AllowedMentions.none()
    is_done = getattr(response, "is_done", None)
    if callable(is_done) and is_done():
        await interaction.followup.send(
            content,
            embed=embed,
            view=view,
            ephemeral=ephemeral,
            allowed_mentions=mention_policy,
        )
    else:
        await response.send_message(
            content,
            embed=embed,
            view=view,
            ephemeral=ephemeral,
            allowed_mentions=mention_policy,
        )


class ReminderView(ui.View):
    """Persistent controls attached to waiting-for-reply reminders."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _get_cog(self, interaction: discord.Interaction) -> "SupportCog | None":
        cog = interaction.client.get_cog("SupportCog")
        return cog if isinstance(cog, SupportCog) else None

    async def _disable(self, interaction: discord.Interaction) -> None:
        for child in self.children:
            if isinstance(child, ui.Button):
                child.disabled = True
        message = getattr(interaction, "message", None)
        if message is not None and hasattr(message, "edit"):
            try:
                await message.edit(view=self)
            except discord.HTTPException:
                logger.debug("Could not disable support reminder buttons", exc_info=True)

    @ui.button(
        label="Issue Resolved",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="labworks:support:issue-resolved",
    )
    async def issue_resolved(self, interaction: discord.Interaction, button: ui.Button) -> None:
        cog = await self._get_cog(interaction)
        if cog is None:
            await send_interaction_message(interaction, "Support automation is unavailable right now.")
            return
        if not await cog.can_manage_thread(interaction, interaction.channel):
            await send_interaction_message(interaction, "You do not have permission to update this support post.")
            return
        await interaction.response.defer(ephemeral=True)
        success, message = await cog.mark_solved(interaction.channel, interaction.user.id, "reminder_resolved")
        if success:
            await self._disable(interaction)
        await interaction.followup.send(message, ephemeral=True)

    @ui.button(
        label="Still Need Help",
        style=discord.ButtonStyle.secondary,
        emoji="🛠️",
        custom_id="labworks:support:still-need-help",
    )
    async def still_need_help(self, interaction: discord.Interaction, button: ui.Button) -> None:
        cog = await self._get_cog(interaction)
        if cog is None:
            await send_interaction_message(interaction, "Support automation is unavailable right now.")
            return
        if not await cog.can_manage_thread(interaction, interaction.channel):
            await send_interaction_message(interaction, "You do not have permission to update this support post.")
            return
        await interaction.response.defer(ephemeral=True)
        post = await cog.store.get_post(interaction.guild.id, interaction.channel.id)
        if post is None:
            await interaction.followup.send("This support post is no longer being tracked.", ephemeral=True)
            return
        await cog.store.update_post(
            interaction.guild.id,
            interaction.channel.id,
            last_author_id=interaction.user.id,
            last_message_at=time.time(),
            waiting_since=None,
            reminder_stage=0,
            reminder_message_id=None,
            reminder_sent_at=None,
            close_at=None,
        )
        await cog.audit_action(
            interaction.guild,
            "support_reminder_acknowledged",
            actor_id=interaction.user.id,
            thread_id=interaction.channel.id,
            details="Member selected Still Need Help",
        )
        await self._disable(interaction)
        await interaction.followup.send("Got it — the post will remain open. Please add any missing details or reply when ready.", ephemeral=True)


class SupportCog(commands.Cog):
    """Forum lifecycle and reminder implementation."""

    def __init__(self, bot: commands.Bot, store: SupportStore):
        self.bot = bot
        self.store = store
        self._view_registered = False
        self._tag_cache: dict[tuple[int, int], discord.ForumTag] = {}
        self._reminder_send_lock = asyncio.Lock()
        self._tag_provision_lock = asyncio.Lock()

    def start_reminder_loop(self) -> None:
        if not self.reminder_loop.is_running():
            self.reminder_loop.start()

    def stop_reminder_loop(self) -> None:
        if self.reminder_loop.is_running():
            self.reminder_loop.cancel()

    @tasks.loop(minutes=REMINDER_LOOP_MINUTES)
    async def reminder_loop(self) -> None:
        for guild in list(getattr(self.bot, "guilds", ())):
            try:
                await self.process_guild(guild)
            except Exception:
                logger.exception("Support reminder pass failed for guild %s", getattr(guild, "id", "unknown"))

    @reminder_loop.before_loop
    async def before_reminder_loop(self) -> None:
        await self.bot.wait_until_ready()

    @reminder_loop.error
    async def reminder_loop_error(self, error: BaseException) -> None:
        logger.exception("Support reminder loop failed", exc_info=error)
        handler = getattr(self.bot, "send_unhandled_error", None)
        if handler is not None:
            try:
                await handler(error, task=self.reminder_loop)
            except Exception:
                logger.exception("Could not report support reminder failure")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self._view_registered:
            try:
                self.bot.add_view(ReminderView())
                self._view_registered = True
            except (discord.ClientException, discord.HTTPException):
                logger.debug("Could not register persistent support reminder view", exc_info=True)

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        await self.handle_thread_created(thread)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if getattr(getattr(message, "author", None), "bot", False):
            return
        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        if guild is None or not is_thread_channel(channel):
            return
        settings = await self.store.get_settings(guild.id)
        if not settings.enabled or self.thread_parent_id(channel) != settings.forum_channel_id:
            return
        if getattr(message, "id", None) == getattr(channel, "id", None):
            await self.handle_thread_created(channel, starter_message=message)
            return
        await self.handle_thread_message(message, settings)

    @staticmethod
    def thread_parent_id(thread: Any) -> int:
        parent_id = getattr(thread, "parent_id", None)
        if parent_id is not None:
            return int(parent_id)
        parent = getattr(thread, "parent", None)
        return int(getattr(parent, "id", 0) or 0)

    @staticmethod
    def thread_owner_id(thread: Any) -> int:
        return int(getattr(thread, "owner_id", 0) or 0)

    def forum_for_settings(self, guild: discord.Guild, settings: SupportSettings) -> Any | None:
        channel = guild.get_channel(settings.forum_channel_id) if settings.forum_channel_id else None
        return channel if is_forum_channel(channel) else None

    async def is_support_thread(self, thread: Any, guild: discord.Guild | None = None) -> bool:
        guild = guild or getattr(thread, "guild", None)
        if guild is None:
            return False
        settings = await self.store.get_settings(guild.id)
        return bool(settings.enabled and self.thread_parent_id(thread) == settings.forum_channel_id)

    @staticmethod
    def _interaction_is_admin(interaction: discord.Interaction) -> bool:
        permissions = getattr(interaction, "permissions", None)
        user = getattr(interaction, "user", None)
        user_permissions = getattr(user, "guild_permissions", None)
        return bool(
            getattr(permissions, "administrator", False)
            or getattr(user_permissions, "administrator", False)
        )

    async def member_is_staff(self, guild: discord.Guild, member: Any) -> bool:
        if getattr(getattr(member, "guild_permissions", None), "administrator", False):
            return True
        role_ids = await self.store.get_staff_roles(guild.id)
        return bool(role_ids.intersection({int(getattr(role, "id", 0)) for role in getattr(member, "roles", ())}))

    async def can_manage_thread(self, interaction: discord.Interaction, thread: Any) -> bool:
        if interaction.guild is None or thread is None:
            return False
        if not await self.is_support_thread(thread, interaction.guild):
            return False
        if self._interaction_is_admin(interaction):
            return True
        if await self.member_is_staff(interaction.guild, interaction.user):
            return True
        post = await self.store.get_post(interaction.guild.id, getattr(thread, "id", 0))
        owner_id = self.thread_owner_id(thread)
        return bool(
            (post and post.creator_id == interaction.user.id)
            or (not post and owner_id and owner_id == interaction.user.id)
        )

    async def audit_action(
        self,
        guild: discord.Guild,
        action: str,
        *,
        actor_id: int | None = None,
        thread_id: int | None = None,
        details: str = "",
    ) -> None:
        correlation_id = uuid.uuid4().hex[:12]
        await self.store.audit(
            guild.id,
            action,
            actor_id=actor_id,
            thread_id=thread_id,
            details=details,
            correlation_id=correlation_id,
        )
        try:
            settings = await self.bot.fetch_guild_settings(guild.id)
            channel = self.bot.get_configured_channel(guild, settings.get("audit_channel_id", 0) if settings else 0)
            if channel is not None:
                await channel.send(
                    f"`{correlation_id}` **{action}** actor={actor_id or 'system'} thread={thread_id or '-'} {details[:300]}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except (discord.HTTPException, AttributeError, KeyError):
            logger.debug("Could not publish support audit action %s", action, exc_info=True)

    async def provision_tags(self, guild: discord.Guild) -> tuple[dict[str, int], list[str]]:
        async with self._tag_provision_lock:
            return await self._provision_tags(guild)

    async def _provision_tags(self, guild: discord.Guild) -> tuple[dict[str, int], list[str]]:
        settings = await self.store.get_settings(guild.id)
        forum = self.forum_for_settings(guild, settings)
        if forum is None:
            return {}, ["Choose a forum channel before provisioning lifecycle tags."]

        bindings = await self.store.get_tag_bindings(guild.id)
        resolved: dict[str, int] = {}
        errors: list[str] = []
        available = list(getattr(forum, "available_tags", ()) or ())

        for state in SUPPORT_STATES:
            existing_binding = bindings.get(state)
            if existing_binding:
                bound = next((tag for tag in available if int(getattr(tag, "id", 0)) == existing_binding[0]), None)
                bound = bound or self._tag_cache.get((guild.id, existing_binding[0]))
                verified_from_discord = False
                if bound is None:
                    fetch_channel = getattr(guild, "fetch_channel", None)
                    if callable(fetch_channel):
                        try:
                            refreshed = await fetch_channel(forum.id)
                            if is_forum_channel(refreshed):
                                verified_from_discord = True
                                forum = refreshed
                                available = list(getattr(forum, "available_tags", ()) or ())
                                bound = next(
                                    (tag for tag in available if int(getattr(tag, "id", 0)) == existing_binding[0]),
                                    None,
                                )
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            pass
                if bound is not None:
                    resolved[state] = existing_binding[0]
                    self._tag_cache[(guild.id, existing_binding[0])] = bound
                    continue
                if not verified_from_discord:
                    errors.append(
                        f"Could not verify the stored {CANONICAL_TAG_NAMES[state]} tag binding; refusing to create a duplicate."
                    )
                    continue

            aliases = TAG_ALIASES[state] | {normalize_tag_name(CANONICAL_TAG_NAMES[state])}
            matches = [tag for tag in available if normalize_tag_name(getattr(tag, "name", "")) in aliases]
            if len(matches) > 1:
                errors.append(
                    f"{CANONICAL_TAG_NAMES[state]} has multiple matching forum tags; rename or remove the duplicate manually."
                )
                continue
            if matches:
                tag = matches[0]
                tag_id = int(getattr(tag, "id", 0) or 0)
                if tag_id <= 0:
                    errors.append(f"The existing {CANONICAL_TAG_NAMES[state]} tag has no usable ID.")
                    continue
                resolved[state] = tag_id
                self._tag_cache[(guild.id, tag_id)] = tag
                await self.store.set_tag_binding(guild.id, state, tag_id, False)
                continue

            if len(available) >= 20:
                errors.append(f"No tag is available for {CANONICAL_TAG_NAMES[state]} and Discord's 20-tag limit is full.")
                continue
            try:
                tag = await forum.create_tag(
                    name=CANONICAL_TAG_NAMES[state],
                    reason="Provision Labworks support lifecycle tags",
                )
                tag_id = int(getattr(tag, "id", 0) or 0)
                if tag_id <= 0:
                    errors.append(f"Discord returned no usable ID for {CANONICAL_TAG_NAMES[state]}.")
                    continue
                available.append(tag)
                resolved[state] = tag_id
                self._tag_cache[(guild.id, tag_id)] = tag
                await self.store.set_tag_binding(guild.id, state, tag_id, True)
            except (discord.Forbidden, discord.HTTPException) as error:
                errors.append(f"Could not create {CANONICAL_TAG_NAMES[state]}: {error.__class__.__name__}.")

        return resolved, errors

    async def _tag_object(self, thread: Any, tag_id: int) -> Any | None:
        guild = getattr(thread, "guild", None)
        if guild is None:
            return None
        parent = getattr(thread, "parent", None)
        if parent is None and getattr(thread, "guild", None) is not None:
            parent = thread.guild.get_channel(self.thread_parent_id(thread))
        if parent is None:
            return None
        getter = getattr(parent, "get_tag", None)
        if callable(getter):
            tag = getter(tag_id)
            if tag is not None:
                self._tag_cache[(guild.id, int(tag_id))] = tag
                return tag
        tag = next(
            (tag for tag in getattr(parent, "available_tags", ()) if int(getattr(tag, "id", 0)) == int(tag_id)),
            None,
        )
        if tag is not None:
            self._tag_cache[(guild.id, int(tag_id))] = tag
            return tag
        cached = self._tag_cache.get((guild.id, int(tag_id)))
        if cached is not None:
            return cached
        # A ForumTag returned by create_tag is not guaranteed to be added to
        # the channel cache immediately. The ID is still the authoritative
        # value Discord needs for Thread.edit; use a lightweight object until
        # the next channel refresh.
        bindings = await self.store.get_tag_bindings(guild.id)
        state = next((state for state, (bound_id, _managed) in bindings.items() if bound_id == int(tag_id)), None)
        if state is None:
            return None
        fallback = discord.ForumTag(name=CANONICAL_TAG_NAMES[state])
        fallback.id = int(tag_id)
        self._tag_cache[(guild.id, int(tag_id))] = fallback
        return fallback

    async def apply_state_tag(self, thread: Any, state: str) -> bool:
        guild = getattr(thread, "guild", None)
        if guild is None or state not in SUPPORT_STATES:
            return False
        bindings = await self.store.get_tag_bindings(guild.id)
        binding = bindings.get(state)
        if not binding:
            return False
        target = await self._tag_object(thread, binding[0])
        if target is None:
            return False
        managed_ids = {tag_id for tag_id, _managed in bindings.values()}
        current = list(getattr(thread, "applied_tags", ()) or ())
        applied = [tag for tag in current if int(getattr(tag, "id", 0)) not in managed_ids]
        if not any(int(getattr(tag, "id", 0)) == int(binding[0]) for tag in applied):
            applied.append(target)
        editor = getattr(thread, "edit", None)
        if not callable(editor):
            return False
        try:
            await editor(applied_tags=applied, reason=f"Labworks support state: {state}")
            return True
        except (discord.Forbidden, discord.HTTPException):
            logger.debug("Could not apply support tag %s to thread %s", state, getattr(thread, "id", "unknown"), exc_info=True)
            return False

    async def change_state(
        self,
        thread: Any,
        state: str,
        *,
        actor_id: int | None,
        action: str,
        details: str = "",
        **fields: Any,
    ) -> SupportPost | None:
        if state not in SUPPORT_STATES:
            raise ValueError(f"Unknown support state: {state}")
        await self.apply_state_tag(thread, state)
        guild = getattr(thread, "guild", None)
        if guild is None:
            return None
        post = await self.store.update_post(guild.id, thread.id, state=state, **fields)
        if post is not None:
            await self.audit_action(
                guild,
                action,
                actor_id=actor_id,
                thread_id=thread.id,
                details=details or f"state={state}",
            )
        return post

    async def _fetch_starter(self, thread: Any) -> Any | None:
        fetch_message = getattr(thread, "fetch_message", None)
        if callable(fetch_message):
            try:
                return await fetch_message(thread.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        return getattr(thread, "last_message", None)

    async def _fetch_tracking_message(self, thread: Any) -> Any | None:
        """Use the newest cached/API message when rebuilding state after a restart."""
        latest = getattr(thread, "last_message", None)
        latest_id = getattr(thread, "last_message_id", None)
        fetch_message = getattr(thread, "fetch_message", None)
        if callable(fetch_message) and latest_id and int(latest_id) != int(getattr(thread, "id", 0)):
            try:
                latest = await fetch_message(int(latest_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        return latest or await self._fetch_starter(thread)

    async def ensure_tracked_post(self, thread: Any, starter_message: Any | None = None) -> SupportPost | None:
        guild = getattr(thread, "guild", None)
        owner_id = self.thread_owner_id(thread)
        if guild is None or not owner_id:
            return None
        existing = await self.store.get_post(guild.id, thread.id)
        message = starter_message or await self._fetch_tracking_message(thread)
        message_id = getattr(message, "id", None)
        author_id = getattr(getattr(message, "author", None), "id", None) or owner_id
        created_at = timestamp(
            getattr(thread, "created_at", None),
            snowflake_timestamp(getattr(thread, "id", 0)),
        )
        last_message_at = timestamp(getattr(message, "created_at", None), created_at)
        initial_state = "unanswered" if starter_message is not None or message_id is None or message_id == getattr(thread, "id", None) else "open"
        post = await self.store.ensure_post(
            guild.id,
            thread.id,
            owner_id,
            message_id,
            author_id,
            last_message_at,
            state=initial_state,
            created_at=created_at,
        )
        if post and initial_state == "open" and author_id == owner_id and post.waiting_since is None:
            post = await self.store.update_post(guild.id, thread.id, waiting_since=last_message_at)
        if existing is None and post and initial_state != "unanswered":
            await self.apply_state_tag(thread, initial_state)
        return post

    @staticmethod
    def is_incomplete_starter(thread: Any, message: Any | None) -> bool:
        content = str(getattr(message, "content", "") or "").strip()
        name = str(getattr(thread, "name", "") or "").strip()
        return len(content) < INCOMPLETE_POST_LIMIT or (not content and bool(name))

    async def send_incomplete_prompt(self, thread: Any, *, actor_id: int | None = None) -> bool:
        guild = getattr(thread, "guild", None)
        if guild is None:
            return False
        post = await self.store.get_post(guild.id, thread.id)
        if post is None:
            post = await self.ensure_tracked_post(thread)
        if (
            post is None
            or post.incomplete_prompt_sent
            or post.closed_at is not None
            or post.state == "solved"
            or getattr(thread, "archived", False)
            or getattr(thread, "locked", False)
        ):
            return False
        if not await self.store.claim_incomplete_prompt(guild.id, thread.id):
            return False
        owner = guild.get_member(post.creator_id) if hasattr(guild, "get_member") else None
        description = (
            f"{owner.mention if owner else _mention(post.creator_id)}, please add the expected behavior, what you tried, "
            "and any error messages or screenshots. That will help the team reproduce the issue."
        )
        try:
            await thread.send(
                embed=discord.Embed(
                    title="A little more detail would help 🧪",
                    description=description,
                    color=discord.Color.orange(),
                ),
                allowed_mentions=discord.AllowedMentions(users=[owner] if owner else [discord.Object(id=post.creator_id)]),
            )
        except (discord.Forbidden, discord.HTTPException):
            await self.store.update_post(guild.id, thread.id, incomplete_prompt_sent=0)
            logger.debug("Could not send incomplete-post prompt", exc_info=True)
            return False
        await self.audit_action(
            guild,
            "support_incomplete_prompt",
            actor_id=actor_id,
            thread_id=thread.id,
            details="Prompted for reproducible support details",
        )
        return True

    async def handle_thread_created(self, thread: Any, starter_message: Any | None = None) -> None:
        guild = getattr(thread, "guild", None)
        if guild is None:
            return
        settings = await self.store.get_settings(guild.id)
        if not settings.enabled or self.thread_parent_id(thread) != settings.forum_channel_id:
            return
        post = await self.ensure_tracked_post(thread, starter_message)
        if post is None or post.closed_at is not None:
            return
        if post.state == "solved":
            return
        if post.state == "unanswered":
            await self.apply_state_tag(thread, "unanswered")
        message = starter_message or await self._fetch_starter(thread)
        if self.is_incomplete_starter(thread, message):
            await self.send_incomplete_prompt(thread)

    async def handle_thread_message(self, message: Any, settings: SupportSettings | None = None) -> None:
        thread = getattr(message, "channel", None)
        guild = getattr(message, "guild", None)
        if guild is None or thread is None:
            return
        settings = settings or await self.store.get_settings(guild.id)
        if not settings.enabled or self.thread_parent_id(thread) != settings.forum_channel_id:
            return
        if getattr(thread, "archived", False) or getattr(thread, "locked", False):
            return
        post = await self.store.get_post(guild.id, thread.id) or await self.ensure_tracked_post(thread)
        if post is None or post.closed_at is not None or post.state == "solved":
            return

        message_at = timestamp(getattr(message, "created_at", None))
        author_id = int(getattr(getattr(message, "author", None), "id", 0) or 0)
        fields: dict[str, Any] = {
            "last_message_id": getattr(message, "id", None),
            "last_author_id": author_id,
            "last_message_at": message_at,
            "reminder_stage": 0,
            "reminder_message_id": None,
            "reminder_sent_at": None,
            "close_at": None,
        }
        is_owner = author_id == post.creator_id
        if is_owner and post.state in ("open", "waiting"):
            fields["waiting_since"] = message_at
        elif not is_owner:
            fields["waiting_since"] = None

        if is_owner and post.state == "waiting":
            await self.change_state(
                thread,
                "open",
                actor_id=author_id,
                action="support_post_updated",
                details="Owner replied again; waiting timer restarted",
                **fields,
            )
            return
        if not is_owner and post.state in ("unanswered", "waiting"):
            await self.change_state(
                thread,
                "open",
                actor_id=author_id,
                action="support_post_opened",
                details="A staff/member reply moved the post to Open",
                **fields,
            )
            return
        await self.store.update_post(guild.id, thread.id, **fields)

    async def reconcile_latest_message(
        self,
        thread: Any,
        post: SupportPost,
        settings: SupportSettings,
    ) -> SupportPost:
        """Recover human replies that arrived while the bot was offline."""
        latest = getattr(thread, "last_message", None)
        latest_id = getattr(thread, "last_message_id", None) or getattr(latest, "id", None)
        if not latest_id or int(latest_id) == post.last_message_id or int(latest_id) == post.reminder_message_id:
            return post
        if latest is not None and getattr(getattr(latest, "author", None), "bot", False):
            return post
        fetch_message = getattr(thread, "fetch_message", None)
        if latest is None and callable(fetch_message):
            try:
                latest = await fetch_message(int(latest_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return post
        if latest is not None and not getattr(getattr(latest, "author", None), "bot", False):
            await self.handle_thread_message(latest, settings)
            return await self.store.get_post(thread.guild.id, thread.id) or post
        return post

    async def mark_solved(self, thread: Any, actor_id: int, action: str = "support_solved") -> tuple[bool, str]:
        guild = getattr(thread, "guild", None)
        if guild is None or not await self.is_support_thread(thread, guild):
            return False, "This command only works inside an enabled support forum post."
        if getattr(thread, "locked", False):
            return False, "This support post is locked and cannot be updated."
        post = await self.store.get_post(guild.id, thread.id)
        if post is None:
            post = await self.ensure_tracked_post(thread)
        if post is None:
            return False, "This support post could not be tracked."
        if post.closed_at is not None:
            return False, "This support post is already closed."
        if post.state == "solved":
            return True, "✅ This support post is already marked solved."
        now = time.time()
        if getattr(thread, "archived", False):
            try:
                await thread.edit(archived=False, reason="Reopen support post to mark solved")
            except (discord.Forbidden, discord.HTTPException):
                return False, "I could not reopen the post to update its lifecycle tag."
        await self.change_state(
            thread,
            "solved",
            actor_id=actor_id,
            action=action,
            details="Marked solved; scheduled archive",
            waiting_since=None,
            reminder_stage=0,
            reminder_message_id=None,
            reminder_sent_at=None,
            close_at=now + post_settings_seconds(await self.store.get_settings(guild.id), "solved_archive_after_hours"),
            solved_at=now,
        )
        return True, "✅ Marked solved. I’ll archive this post after the configured grace period."

    async def mark_unsolved(self, thread: Any, actor_id: int) -> tuple[bool, str]:
        guild = getattr(thread, "guild", None)
        if guild is None or not await self.is_support_thread(thread, guild):
            return False, "This command only works inside an enabled support forum post."
        if getattr(thread, "locked", False):
            return False, "This support post is locked and cannot be updated."
        post = await self.store.get_post(guild.id, thread.id)
        if post is None:
            return False, "This support post is not being tracked."
        if getattr(thread, "archived", False):
            try:
                await thread.edit(archived=False, reason="Reopen support post")
            except (discord.Forbidden, discord.HTTPException):
                return False, "I could not reopen this support post."
        await self.change_state(
            thread,
            "open",
            actor_id=actor_id,
            action="support_unsolved",
            details="Returned solved post to Open",
            waiting_since=None,
            reminder_stage=0,
            reminder_message_id=None,
            reminder_sent_at=None,
            close_at=None,
            solved_at=None,
            closed_at=None,
        )
        return True, "↩️ Reopened this support post and returned it to Open."

    async def send_reminder(self, thread: Any, post: SupportPost, settings: SupportSettings, *, hard: bool) -> bool:
        # The scheduler is single-threaded, but button callbacks and a manual
        # reconciliation can overlap it. Serialize the Discord send so one
        # persisted post cannot receive duplicate reminders in one process.
        async with self._reminder_send_lock:
            guild = getattr(thread, "guild", None)
            if guild is None:
                return False
            current = await self.store.get_post(guild.id, thread.id)
            target_stage = 2 if hard else 1
            if (
                current is None
                or current.closed_at is not None
                or current.state == "solved"
                or current.reminder_stage >= target_stage
                or (not hard and current.reminder_stage != 0)
            ):
                return False
            return await self._send_reminder(thread, current, settings, hard=hard)

    async def _send_reminder(self, thread: Any, post: SupportPost, settings: SupportSettings, *, hard: bool) -> bool:
        guild = getattr(thread, "guild", None)
        target_stage = 2 if hard else 1
        if (
            guild is None
            or post.closed_at is not None
            or post.state == "solved"
            or post.reminder_stage >= target_stage
            or (not hard and post.reminder_stage != 0)
        ):
            return False
        age_label = "72 hours" if hard else "24 hours"
        embed = discord.Embed(
            title="Still need help with this?",
            description=(
                f"{_mention(post.creator_id)}, this support post has not had a recent reply for about {age_label}. "
                "Use a button below or reply with an update."
            ),
            color=discord.Color.blurple(),
        )
        try:
            sent = await thread.send(
                embed=embed,
                view=ReminderView(),
                allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=post.creator_id)]),
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.debug("Could not send support reminder", exc_info=True)
            return False
        now = time.time()
        reminder_stage = target_stage
        await self.store.update_post(
            guild.id,
            thread.id,
            reminder_stage=reminder_stage,
            reminder_message_id=getattr(sent, "id", None),
            reminder_sent_at=now,
            close_at=now + settings.close_after_reminder_hours * 3600 if hard else None,
        )
        await self.audit_action(
            guild,
            "support_reminder_sent",
            actor_id=None,
            thread_id=thread.id,
            details=f"kind={'hard' if hard else 'first'}",
        )
        return True

    async def close_post(self, thread: Any, post: SupportPost, *, reason: str) -> bool:
        guild = getattr(thread, "guild", None)
        if guild is None:
            return False
        if getattr(thread, "locked", False):
            return False
        await self.apply_state_tag(thread, "solved")
        try:
            await thread.edit(archived=True, reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            logger.debug("Could not archive support post %s", getattr(thread, "id", "unknown"), exc_info=True)
            return False
        now = time.time()
        await self.store.update_post(
            guild.id,
            thread.id,
            state="solved",
            solved_at=post.solved_at or now,
            closed_at=now,
            close_at=None,
        )
        await self.audit_action(
            guild,
            "support_post_closed",
            actor_id=None,
            thread_id=thread.id,
            details=reason,
        )
        return True

    async def process_guild(self, guild: discord.Guild) -> None:
        settings = await self.store.get_settings(guild.id)
        if not settings.enabled:
            return
        forum = self.forum_for_settings(guild, settings)
        if forum is None:
            return
        try:
            active_threads = await guild.active_threads()
        except (discord.Forbidden, discord.HTTPException):
            logger.debug("Could not enumerate active support threads", exc_info=True)
            return
        active = {
            int(thread.id): thread
            for thread in active_threads
            if self.thread_parent_id(thread) == settings.forum_channel_id
        }
        for thread in active.values():
            if getattr(thread, "archived", False) or getattr(thread, "locked", False):
                continue
            post = await self.store.get_post(guild.id, thread.id) or await self.ensure_tracked_post(thread)
            if post is None or post.closed_at is not None:
                continue
            post = await self.reconcile_latest_message(thread, post, settings)
            await self.process_post(thread, post, settings)

        # Solved posts may not appear in active_threads.  Persisted close_at
        # lets a restart finish the archive operation when the post is cached.
        now = time.time()
        for post in await self.store.list_posts(guild.id):
            if post.thread_id in active or post.closed_at is not None:
                continue
            if post.state == "solved" and post.close_at and post.close_at <= now:
                thread = guild.get_channel(post.thread_id)
                get_thread = getattr(forum, "get_thread", None)
                if thread is None and callable(get_thread):
                    thread = get_thread(post.thread_id)
                if thread is None:
                    try:
                        thread = await self.bot.fetch_channel(post.thread_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        thread = None
                if thread is not None:
                    await self.close_post(thread, post, reason="Archive scheduled solved support post")

    async def process_post(self, thread: Any, post: SupportPost, settings: SupportSettings) -> None:
        now = time.time()
        if getattr(thread, "archived", False) or getattr(thread, "locked", False) or post.closed_at is not None:
            return
        if post.state == "solved":
            if post.close_at and post.close_at <= now:
                await self.close_post(thread, post, reason="Archive scheduled solved support post")
            return

        if post.state == "open" and post.waiting_since and now - post.waiting_since >= settings.waiting_delay_seconds:
            post = await self.change_state(
                thread,
                "waiting",
                actor_id=None,
                action="support_waiting_for_reply",
                details="Owner reply has been waiting for a response",
                waiting_since=post.waiting_since,
            ) or post

        age = max(0.0, now - post.last_message_at)
        hard_due = age >= settings.hard_reminder_after_hours * 3600
        first_due = age >= settings.reminder_after_hours * 3600 and post.last_author_id != post.creator_id
        if post.reminder_stage == 0 and (hard_due or first_due):
            await self.send_reminder(thread, post, settings, hard=hard_due)
        elif post.reminder_stage == 1 and hard_due:
            await self.send_reminder(thread, post, settings, hard=True)
        elif post.reminder_stage == 1 and post.close_at and post.close_at <= now:
            # Also honor a close_at written by an older build or an operator
            # override; normal first reminders leave this field empty so the
            # hard reminder remains the next durable stage.
            await self.close_post(thread, post, reason="Close after unanswered support reminder")
        elif post.reminder_stage >= 2 and post.close_at and post.close_at <= now:
            await self.close_post(thread, post, reason="Close after unanswered support reminder")

    @app_commands.command(name="solved", description="Mark this support post as solved")
    async def solved(self, interaction: discord.Interaction) -> None:
        if not is_thread_channel(interaction.channel) or not await self.can_manage_thread(interaction, interaction.channel):
            await send_interaction_message(interaction, "Only the post creator, configured support staff, or an administrator can do that here.")
            return
        success, message = await self.mark_solved(interaction.channel, interaction.user.id)
        await send_interaction_message(interaction, message)

    @app_commands.command(name="unsolve", description="Reopen a solved support post")
    async def unsolve(self, interaction: discord.Interaction) -> None:
        if not is_thread_channel(interaction.channel) or not await self.can_manage_thread(interaction, interaction.channel):
            await send_interaction_message(interaction, "Only the post creator, configured support staff, or an administrator can do that here.")
            return
        success, message = await self.mark_unsolved(interaction.channel, interaction.user.id)
        await send_interaction_message(interaction, message)

    @app_commands.command(name="incomplete-post", description="Prompt a support post for reproducible details")
    async def incomplete_post(self, interaction: discord.Interaction) -> None:
        if not is_thread_channel(interaction.channel) or not await self.can_manage_thread(interaction, interaction.channel):
            await send_interaction_message(interaction, "Only the post creator, configured support staff, or an administrator can do that here.")
            return
        sent = await self.send_incomplete_prompt(interaction.channel, actor_id=interaction.user.id)
        await send_interaction_message(
            interaction,
            "✅ Asked the creator for more reproducible details." if sent else "No prompt was sent; it may already have been sent or the post is not tracked.",
        )


def post_settings_seconds(settings: SupportSettings, field: str) -> float:
    return float(getattr(settings, field)) * 3600
