"""Staff operations for Labworks support communities.

This module contains the human-facing controls that sit beside the support
forum lifecycle: reusable canned responses, safe channel controls, and the
support portion of the existing administrator configuration dashboard.
"""

from __future__ import annotations

import datetime
import difflib
import logging
import re
import time
from typing import Any, Iterable

import discord
from discord import app_commands, ui
from discord.ext import commands

try:  # Works both as ``python src/main.py`` and package imports in tests.
    from .support import (
        CANONICAL_TAG_NAMES,
        SupportCog,
        is_forum_channel,
        send_interaction_message,
    )
    from .support_store import CannedResponse, SupportSettings, SupportStore
except ImportError:  # pragma: no cover - exercised by the production entrypoint.
    from support import CANONICAL_TAG_NAMES, SupportCog, is_forum_channel, send_interaction_message
    from support_store import CannedResponse, SupportSettings, SupportStore


logger = logging.getLogger(__name__)
MAX_TAG_CONTENT = 1000
MAX_SLOWMODE_SECONDS = 6 * 60 * 60


def parse_duration(value: str, *, maximum: int = MAX_SLOWMODE_SECONDS) -> int:
    """Parse a compact duration such as ``30s``, ``10m``, or ``2h``."""

    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours)\s*", value or "", re.I)
    if not match:
        raise ValueError("Use a duration such as 30s, 10m, or 2h.")
    amount = float(match.group(1))
    unit = match.group(2).lower()
    multiplier = 1 if unit.startswith("s") else 60 if unit.startswith("m") else 3600
    seconds = int(amount * multiplier)
    if seconds < 0 or seconds > maximum:
        raise ValueError(f"Duration must be between 0 and {maximum // 3600 if maximum >= 3600 else maximum} hours.")
    return seconds


def _now_text(value: float) -> str:
    return discord.utils.format_dt(datetime.datetime.fromtimestamp(value, datetime.timezone.utc), style="R")


async def require_admin(interaction: discord.Interaction) -> bool:
    permissions = getattr(interaction, "permissions", None)
    user_permissions = getattr(getattr(interaction, "user", None), "guild_permissions", None)
    if interaction.guild and (
        getattr(permissions, "administrator", False)
        or getattr(user_permissions, "administrator", False)
    ):
        return True
    await send_interaction_message(interaction, "🚫 Administrator permission is required.")
    return False


class ResponseModal(ui.Modal):
    def __init__(self, cog: "OperationsCog", *, response: CannedResponse | None = None):
        super().__init__(title="Edit canned response" if response else "Create canned response")
        self.cog = cog
        self.original_name = response.name if response else None
        self.name_input = ui.TextInput(
            label="Name",
            placeholder="for example: reset-password",
            default=response.name if response else None,
            min_length=1,
            max_length=50,
        )
        self.content_input = ui.TextInput(
            label="Response",
            style=discord.TextStyle.paragraph,
            placeholder="The message to send in the current channel",
            default=response.content if response else None,
            min_length=1,
            max_length=MAX_TAG_CONTENT,
        )
        self.add_item(self.name_input)
        self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.cog.member_is_staff_or_admin(interaction):
            await send_interaction_message(interaction, "Configured support staff or an administrator is required.")
            return
        name = str(self.name_input.value).strip()
        content = str(self.content_input.value).strip()
        if not name or not content or len(content) > MAX_TAG_CONTENT:
            await send_interaction_message(interaction, "A name and a response of at most 1,000 characters are required.")
            return
        guild_id = interaction.guild.id
        existing = await self.cog.store.find_response(guild_id, name)
        if existing and (self.original_name is None or existing.name.lower() != self.original_name.lower()):
            await send_interaction_message(interaction, "That canned response name is already in use.")
            return
        try:
            if self.original_name is None:
                await self.cog.store.create_response(guild_id, name, content, interaction.user.id)
                action = "canned_response_created"
                result = f"✅ Created canned response **{name}**."
            else:
                await self.cog.store.update_response(guild_id, self.original_name, name, content)
                action = "canned_response_updated"
                result = f"✅ Updated canned response **{name}**."
            await self.cog.audit_action(interaction.guild, action, actor_id=interaction.user.id, details=f"name={name}")
            await send_interaction_message(interaction, result)
        except (ValueError, discord.HTTPException):
            await send_interaction_message(interaction, "I could not save that canned response.")


class TagUseView(ui.View):
    def __init__(self, cog: "OperationsCog", requester_id: int, response: CannedResponse):
        super().__init__(timeout=60)
        self.cog = cog
        self.requester_id = requester_id
        self.response = response

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await send_interaction_message(interaction, "Only the member who requested this preview can use it.")
            return False
        return await self.cog.member_is_staff_or_admin(interaction, allow_public=True)

    @ui.button(label="Send response", style=discord.ButtonStyle.primary, emoji="📨")
    async def send_response(self, interaction: discord.Interaction, button: ui.Button) -> None:
        current = await self.cog.store.find_response(interaction.guild.id, self.response.name)
        if current is None:
            await send_interaction_message(interaction, "That canned response was deleted.")
            self.stop()
            return
        channel = interaction.channel
        try:
            await channel.send(current.content, allowed_mentions=discord.AllowedMentions.none())
            await self.cog.store.increment_response_uses(interaction.guild.id, current.name)
            await self.cog.audit_action(
                interaction.guild,
                "canned_response_used",
                actor_id=interaction.user.id,
                details=f"name={current.name}",
            )
            await interaction.response.edit_message(content="✅ Response sent.", embed=None, view=None)
        except (discord.Forbidden, discord.HTTPException):
            await send_interaction_message(interaction, "I could not send that response in this channel.")
        self.stop()


class TagDeleteView(ui.View):
    def __init__(self, cog: "OperationsCog", requester_id: int, response: CannedResponse):
        super().__init__(timeout=60)
        self.cog = cog
        self.requester_id = requester_id
        self.response = response

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await send_interaction_message(interaction, "Only the member who requested this confirmation can use it.")
            return False
        return await self.cog.member_is_staff_or_admin(interaction)

    @ui.button(label="Delete response", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm_delete(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await self.cog.store.delete_response(interaction.guild.id, self.response.name)
        await self.cog.audit_action(
            interaction.guild,
            "canned_response_deleted",
            actor_id=interaction.user.id,
            details=f"name={self.response.name}",
        )
        await interaction.response.edit_message(content=f"🗑️ Deleted **{self.response.name}**.", embed=None, view=None)
        self.stop()


class SupportTimerModal(ui.Modal):
    def __init__(self, cog: "OperationsCog", settings: SupportSettings):
        super().__init__(title="Support reminder timings")
        self.cog = cog
        self.waiting = ui.TextInput(
            label="Waiting delay (minutes)",
            default=str(max(1, settings.waiting_delay_seconds // 60)),
            min_length=1,
            max_length=5,
        )
        self.first = ui.TextInput(
            label="First reminder (hours)",
            default=str(settings.reminder_after_hours),
            min_length=1,
            max_length=6,
        )
        self.hard = ui.TextInput(
            label="Hard reminder (hours)",
            default=str(settings.hard_reminder_after_hours),
            min_length=1,
            max_length=6,
        )
        self.close = ui.TextInput(
            label="Close after reminder (hours)",
            default=str(settings.close_after_reminder_hours),
            min_length=1,
            max_length=6,
        )
        self.solved_archive = ui.TextInput(
            label="Solved archive delay (hours)",
            default=str(settings.solved_archive_after_hours),
            min_length=1,
            max_length=6,
        )
        for field in (self.waiting, self.first, self.hard, self.close, self.solved_archive):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await require_admin(interaction):
            return
        try:
            waiting_minutes = int(str(self.waiting.value).strip())
            first = float(str(self.first.value).strip())
            hard = float(str(self.hard.value).strip())
            close = float(str(self.close.value).strip())
            solved_archive = float(str(self.solved_archive.value).strip())
            if not 1 <= waiting_minutes <= 1440:
                raise ValueError
            if not 1 <= first <= 720 or not first <= hard <= 720:
                raise ValueError
            if not 1 <= close <= 720 or not 0.25 <= solved_archive <= 168:
                raise ValueError
        except ValueError:
            await send_interaction_message(
                interaction,
                "Use valid values: waiting 1–1,440 minutes; reminders/closure 1–720 hours; solved archive 0.25–168 hours.",
            )
            return
        await self.cog.store.update_settings(
            interaction.guild.id,
            waiting_delay_seconds=waiting_minutes * 60,
            reminder_after_hours=first,
            hard_reminder_after_hours=hard,
            close_after_reminder_hours=close,
            solved_archive_after_hours=solved_archive,
        )
        await self.cog.audit_action(interaction.guild, "support_settings_updated", actor_id=interaction.user.id, details="timers")
        await send_interaction_message(interaction, "✅ Support reminder timings updated.")


class SupportConfigView(ui.View):
    def __init__(self, cog: "OperationsCog"):
        super().__init__(timeout=300)
        self.cog = cog
        selector = ui.Select(
            placeholder="Support configuration…",
            options=[
                discord.SelectOption(label="Overview", value="overview", emoji="🧭"),
                discord.SelectOption(label="Forum channel", value="forum", emoji="🧵"),
                discord.SelectOption(label="Staff roles", value="roles", emoji="🛡️"),
                discord.SelectOption(label="Reminder timings", value="timings", emoji="⏱️"),
                discord.SelectOption(label="Provision lifecycle tags", value="provision", emoji="🏷️"),
                discord.SelectOption(label="Enable support workflow", value="enable", emoji="✅"),
                discord.SelectOption(label="Disable support workflow", value="disable", emoji="⏸️"),
            ],
        )
        self.selector = selector
        selector.callback = self.category_selected
        self.add_item(selector)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_admin(interaction)

    @ui.button(label="Clear staff roles", style=discord.ButtonStyle.secondary, emoji="🧹")
    async def clear_staff_roles(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await self.cog.store.replace_staff_roles(interaction.guild.id, set())
        await self.cog.audit_action(interaction.guild, "support_staff_roles_updated", actor_id=interaction.user.id, details="cleared")
        await send_interaction_message(interaction, "✅ Cleared configured support staff roles. Administrators still bypass staff checks.")

    async def category_selected(self, interaction: discord.Interaction) -> None:
        value = self.selector.values[0]
        if value == "overview":
            await self.cog.send_support_config(interaction, include_view=False)
        elif value == "forum":
            selector = ui.ChannelSelect(
                channel_types=[discord.ChannelType.forum],
                placeholder="Select the support forum…",
            )

            async def callback(inner: discord.Interaction) -> None:
                if not await require_admin(inner):
                    return
                channel = selector.values[0]
                if not is_forum_channel(channel):
                    await send_interaction_message(inner, "That is not a forum channel.")
                    return
                await self.cog.store.update_settings(inner.guild.id, forum_channel_id=channel.id)
                await self.cog.audit_action(inner.guild, "support_forum_configured", actor_id=inner.user.id, details=f"channel={channel.id}")
                await send_interaction_message(inner, f"✅ Support forum set to {channel.mention}.")

            selector.callback = callback
            view = ui.View(timeout=120)
            view.add_item(selector)
            await send_interaction_message(interaction, "Select the forum that should receive support lifecycle automation.", view=view)
        elif value == "roles":
            selector = ui.RoleSelect(placeholder="Select one or more staff roles…", min_values=1, max_values=10)

            async def callback(inner: discord.Interaction) -> None:
                if not await require_admin(inner):
                    return
                role_ids = {role.id for role in selector.values}
                await self.cog.store.replace_staff_roles(inner.guild.id, role_ids)
                await self.cog.audit_action(inner.guild, "support_staff_roles_updated", actor_id=inner.user.id, details=f"count={len(role_ids)}")
                await send_interaction_message(inner, "✅ Support staff roles updated.")

            selector.callback = callback
            view = ui.View(timeout=120)
            view.add_item(selector)
            await send_interaction_message(interaction, "Select the roles that may manage support posts and canned responses.", view=view)
        elif value == "timings":
            await interaction.response.send_modal(SupportTimerModal(self.cog, await self.cog.store.get_settings(interaction.guild.id)))
        elif value == "provision":
            bindings, errors = await self.cog.support.provision_tags(interaction.guild)
            await self.cog.audit_action(interaction.guild, "support_tags_provisioned", actor_id=interaction.user.id, details=f"bound={len(bindings)}")
            await send_interaction_message(interaction, self.cog.format_provision_result(bindings, errors))
        elif value == "enable":
            await self.cog.enable_support(interaction)
        elif value == "disable":
            await self.cog.store.update_settings(interaction.guild.id, enabled=False)
            await self.cog.audit_action(interaction.guild, "support_workflow_disabled", actor_id=interaction.user.id)
            await send_interaction_message(interaction, "⏸️ Support workflow disabled for this server. Existing posts are left untouched.")


class ChannelActionView(ui.View):
    def __init__(self, cog: "OperationsCog", action: str, *, seconds: int = 0, reason: str | None = None):
        super().__init__(timeout=180)
        self.cog = cog
        self.action = action
        self.seconds = seconds
        self.reason = reason or f"Labworks {action} command"
        self.selector = ui.ChannelSelect(
            channel_types=[discord.ChannelType.text, discord.ChannelType.forum],
            placeholder="Select up to five text/forum channels…",
            min_values=1,
            max_values=5,
        )
        self.add_item(self.selector)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_admin(interaction)

    @ui.button(label="Apply", style=discord.ButtonStyle.primary, emoji="⚙️")
    async def apply(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        results: list[str] = []
        for channel in self.selector.values:
            try:
                if self.action == "lock":
                    await self.cog.lock_channel(interaction.guild, channel, interaction.user.id, self.reason)
                    results.append(f"✅ Locked {channel.mention}.")
                elif self.action == "unlock":
                    restored = await self.cog.unlock_channel(interaction.guild, channel, interaction.user.id, self.reason)
                    results.append(f"✅ Restored {channel.mention}." if restored else f"ℹ️ {channel.mention} has no Labworks lock backup.")
                else:
                    await channel.edit(slowmode_delay=self.seconds, reason=self.reason)
                    await self.cog.audit_action(
                        interaction.guild,
                        "channel_slowmode_updated",
                        actor_id=interaction.user.id,
                        details=f"channel={channel.id} seconds={self.seconds}",
                    )
                    results.append(f"✅ Set {channel.mention} slowmode to **{self.seconds}s**.")
            except (discord.Forbidden, discord.HTTPException, ValueError):
                results.append(f"❌ Could not update {getattr(channel, 'mention', channel)}.")
        self.stop()
        await interaction.followup.send("\n".join(results), ephemeral=True)


class OperationsCog(commands.Cog):
    """Canned responses, channel controls, and support configuration."""

    tag_group = app_commands.Group(name="tag", description="Use and manage canned support responses")

    def __init__(self, bot: commands.Bot, store: SupportStore, support: SupportCog):
        self.bot = bot
        self.store = store
        self.support = support
        self.tag_cooldowns: dict[tuple[int, int], float] = {}

    async def member_is_staff_or_admin(self, interaction: discord.Interaction, *, allow_public: bool = False) -> bool:
        if interaction.guild is None:
            return False
        if await require_admin_without_response(interaction):
            return True
        if allow_public:
            return True
        return await self.support.member_is_staff(interaction.guild, interaction.user)

    async def audit_action(self, guild: discord.Guild, action: str, **kwargs: Any) -> None:
        await self.support.audit_action(guild, action, **kwargs)

    @staticmethod
    async def _response_or_suggestion(interaction: discord.Interaction, responses: Iterable[CannedResponse], name: str) -> None:
        names = [response.name for response in responses]
        suggestion = difflib.get_close_matches(name, names, n=3, cutoff=0.35)
        suffix = f" Did you mean: {', '.join(f'`{item}`' for item in suggestion)}?" if suggestion else ""
        await send_interaction_message(interaction, f"No canned response named **{name}** exists.{suffix}")

    def format_provision_result(self, bindings: dict[str, int], errors: list[str]) -> str:
        lines = [f"`{CANONICAL_TAG_NAMES[state]}` → `{tag_id}`" for state, tag_id in bindings.items()]
        if errors:
            lines.extend(f"❌ {error}" for error in errors)
        if not lines:
            return "❌ No lifecycle tags were bound."
        prefix = "⚠️ Lifecycle tag bindings (action required):" if errors else "✅ Lifecycle tag bindings:"
        return prefix + "\n" + "\n".join(lines)

    async def send_support_config(self, interaction: discord.Interaction, *, include_view: bool = True) -> None:
        settings = await self.store.get_settings(interaction.guild.id)
        forum = interaction.guild.get_channel(settings.forum_channel_id) if settings.forum_channel_id else None
        roles = await self.store.get_staff_roles(interaction.guild.id)
        bindings = await self.store.get_tag_bindings(interaction.guild.id)
        posts = await self.store.list_posts(interaction.guild.id)
        role_text = ", ".join(
            (interaction.guild.get_role(role_id).mention if interaction.guild.get_role(role_id) else f"`{role_id}`")
            for role_id in sorted(roles)
        ) or "Administrators only"
        tags_text = ", ".join(f"{CANONICAL_TAG_NAMES.get(state, state)}=`{tag_id}`" for state, (tag_id, _managed) in bindings.items()) or "Not provisioned"
        embed = discord.Embed(
            title="🧵 Support workflow",
            description="Opt-in forum lifecycle automation for Labworks. Existing forum tags are reused where unambiguous; missing canonical tags can be created.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Status", value="✅ Enabled" if settings.enabled else "⏸️ Disabled", inline=True)
        embed.add_field(name="Forum", value=forum.mention if forum else "Not configured", inline=True)
        embed.add_field(name="Staff roles", value=role_text, inline=False)
        embed.add_field(name="Lifecycle tags", value=tags_text, inline=False)
        embed.add_field(
            name="Timings",
            value=(
                f"Waiting: **{settings.waiting_delay_seconds // 60}m**\n"
                f"Reminder: **{settings.reminder_after_hours:g}h / {settings.hard_reminder_after_hours:g}h**\n"
                f"Close after reminder: **{settings.close_after_reminder_hours:g}h**\n"
                f"Solved archive: **{settings.solved_archive_after_hours:g}h**"
            ),
            inline=True,
        )
        embed.add_field(name="Tracked posts", value=str(len(posts)), inline=True)
        await send_interaction_message(interaction, "", embed=embed, view=SupportConfigView(self) if include_view else None)

    async def enable_support(self, interaction: discord.Interaction) -> None:
        bindings, errors = await self.support.provision_tags(interaction.guild)
        if errors or len(bindings) != 4:
            await send_interaction_message(
                interaction,
                "❌ Support workflow remains disabled until the forum and all four lifecycle tags are ready.\n" + self.format_provision_result(bindings, errors),
            )
            return
        forum = interaction.guild.get_channel((await self.store.get_settings(interaction.guild.id)).forum_channel_id)
        missing: list[str] = []
        me = getattr(interaction.guild, "me", None)
        if forum is None:
            missing.append("forum channel")
        elif me is not None:
            permissions = forum.permissions_for(me)
            for permission in ("view_channel", "send_messages", "embed_links", "manage_threads"):
                if not getattr(permissions, permission, False):
                    missing.append(permission)
        if missing:
            await send_interaction_message(interaction, "❌ I need these forum permissions before enabling: " + ", ".join(missing))
            return
        await self.store.update_settings(interaction.guild.id, enabled=True)
        await self.audit_action(interaction.guild, "support_workflow_enabled", actor_id=interaction.user.id, details="all tags ready")
        await send_interaction_message(interaction, "✅ Support workflow enabled. New posts will use the lifecycle and reminder rules.")

    # ----- Canned response commands -------------------------------------------------

    @tag_group.command(name="create", description="Create a canned support response")
    async def tag_create(self, interaction: discord.Interaction) -> None:
        if not await self.member_is_staff_or_admin(interaction):
            await send_interaction_message(interaction, "Configured support staff or an administrator is required.")
            return
        await interaction.response.send_modal(ResponseModal(self))

    @tag_group.command(name="use", description="Preview and send a canned support response")
    @app_commands.describe(tag_name="The canned response name")
    async def tag_use(self, interaction: discord.Interaction, tag_name: str) -> None:
        response = await self.store.find_response(interaction.guild.id, tag_name)
        if response is None:
            await self._response_or_suggestion(interaction, await self.store.list_responses(interaction.guild.id), tag_name)
            return
        is_staff = await self.support.member_is_staff(interaction.guild, interaction.user)
        is_admin = await require_admin_without_response(interaction)
        key = (interaction.guild.id, interaction.user.id)
        now = time.time()
        if not (is_staff or is_admin) and self.tag_cooldowns.get(key, 0) > now:
            await send_interaction_message(interaction, "Please wait before using another canned response.")
            return
        if not (is_staff or is_admin):
            self.tag_cooldowns[key] = now + 60
        embed = discord.Embed(title=f"Canned response: {response.name}", description=response.content, color=discord.Color.blurple())
        await send_interaction_message(interaction, "Preview — send this response to the current channel?", embed=embed, view=TagUseView(self, interaction.user.id, response))

    @tag_use.autocomplete("tag_name")
    async def tag_use_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        responses = await self.store.list_responses(interaction.guild.id)
        current = current.lower()
        return [app_commands.Choice(name=response.name, value=response.name) for response in responses if current in response.name.lower()][:25]

    @tag_group.command(name="info", description="Show information about a canned support response")
    @app_commands.describe(tag_name="The canned response name")
    async def tag_info(self, interaction: discord.Interaction, tag_name: str) -> None:
        response = await self.store.find_response(interaction.guild.id, tag_name)
        if response is None:
            await self._response_or_suggestion(interaction, await self.store.list_responses(interaction.guild.id), tag_name)
            return
        embed = discord.Embed(title=f"Canned response: {response.name}", description=response.content, color=discord.Color.teal())
        embed.add_field(name="Uses", value=str(response.uses))
        embed.add_field(name="Creator", value=f"<@{response.creator_id}>")
        embed.add_field(name="Created", value=_now_text(response.created_at))
        await send_interaction_message(interaction, "", embed=embed)

    @tag_group.command(name="edit", description="Edit a canned support response")
    @app_commands.describe(tag_name="The canned response name")
    async def tag_edit(self, interaction: discord.Interaction, tag_name: str) -> None:
        if not await self.member_is_staff_or_admin(interaction):
            await send_interaction_message(interaction, "Configured support staff or an administrator is required.")
            return
        response = await self.store.find_response(interaction.guild.id, tag_name)
        if response is None:
            await self._response_or_suggestion(interaction, await self.store.list_responses(interaction.guild.id), tag_name)
            return
        await interaction.response.send_modal(ResponseModal(self, response=response))

    @tag_group.command(name="delete", description="Delete a canned support response")
    @app_commands.describe(tag_name="The canned response name")
    async def tag_delete(self, interaction: discord.Interaction, tag_name: str) -> None:
        if not await self.member_is_staff_or_admin(interaction):
            await send_interaction_message(interaction, "Configured support staff or an administrator is required.")
            return
        response = await self.store.find_response(interaction.guild.id, tag_name)
        if response is None:
            await self._response_or_suggestion(interaction, await self.store.list_responses(interaction.guild.id), tag_name)
            return
        await send_interaction_message(
            interaction,
            f"Delete canned response **{response.name}**?",
            view=TagDeleteView(self, interaction.user.id, response),
        )

    @tag_delete.autocomplete("tag_name")
    async def tag_delete_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        responses = await self.store.list_responses(interaction.guild.id)
        return [app_commands.Choice(name=response.name, value=response.name) for response in responses if current.lower() in response.name.lower()][:25]

    @tag_edit.autocomplete("tag_name")
    async def tag_edit_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        responses = await self.store.list_responses(interaction.guild.id)
        return [app_commands.Choice(name=response.name, value=response.name) for response in responses if current.lower() in response.name.lower()][:25]

    @tag_info.autocomplete("tag_name")
    async def tag_info_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        responses = await self.store.list_responses(interaction.guild.id)
        return [app_commands.Choice(name=response.name, value=response.name) for response in responses if current.lower() in response.name.lower()][:25]

    # ----- Channel controls ----------------------------------------------------------

    async def lock_channel(self, guild: discord.Guild, channel: Any, actor_id: int, reason: str) -> None:
        if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
            raise ValueError("Only text and forum channels can be locked.")
        if await self.store.get_lock_backup(guild.id, channel.id) is not None:
            return
        backup = serialize_overwrites(channel)
        if not await self.store.save_lock_backup(guild.id, channel.id, backup, actor_id):
            return
        overwrites = dict(channel.overwrites)
        everyone = guild.default_role
        staff_role_ids = await self.store.get_staff_roles(guild.id)
        everyone_overwrite = overwrites.get(everyone, discord.PermissionOverwrite())
        everyone_overwrite.send_messages = False
        everyone_overwrite.create_public_threads = False
        everyone_overwrite.create_private_threads = False
        everyone_overwrite.send_messages_in_threads = False
        overwrites[everyone] = everyone_overwrite
        for target, overwrite in overwrites.items():
            if target == everyone:
                continue
            if isinstance(target, discord.Role):
                may_post = target.id in staff_role_ids
            elif isinstance(target, discord.Member):
                may_post = bool(
                    getattr(getattr(target, "guild_permissions", None), "administrator", False)
                    or staff_role_ids.intersection({role.id for role in target.roles})
                )
            else:
                may_post = False
            overwrite.send_messages = may_post
            overwrite.create_public_threads = may_post
            overwrite.create_private_threads = may_post
            overwrite.send_messages_in_threads = may_post
        for role_id in staff_role_ids:
            role = guild.get_role(role_id)
            if role is not None and role not in overwrites:
                overwrites[role] = discord.PermissionOverwrite(
                    send_messages=True,
                    create_public_threads=True,
                    create_private_threads=True,
                    send_messages_in_threads=True,
                )
        try:
            await channel.edit(overwrites=overwrites, reason=reason)
        except Exception:
            # The backup is retained if an edit fails, so an operator can
            # inspect and restore it rather than losing the original state.
            raise
        await self.audit_action(guild, "channel_locked", actor_id=actor_id, details=f"channel={channel.id}")

    async def unlock_channel(self, guild: discord.Guild, channel: Any, actor_id: int, reason: str) -> bool:
        backup = await self.store.get_lock_backup(guild.id, channel.id)
        if backup is None:
            return False
        overwrites, skipped = deserialize_overwrites(guild, backup)
        if skipped:
            raise ValueError("Some deleted roles or members no longer exist, so the original overwrites cannot be reconstructed exactly.")
        await channel.edit(overwrites=overwrites, reason=reason)
        await self.store.delete_lock_backup(guild.id, channel.id)
        await self.audit_action(guild, "channel_unlocked", actor_id=actor_id, details=f"channel={channel.id}")
        return True

    @app_commands.command(name="lock", description="Lock up to five text/forum channels")
    @app_commands.describe(reason="Optional reason recorded in the audit log")
    async def lock(self, interaction: discord.Interaction, reason: str | None = None) -> None:
        if not await require_admin(interaction):
            return
        await send_interaction_message(interaction, "Select the channels to lock, then apply.", view=ChannelActionView(self, "lock", reason=reason))

    @app_commands.command(name="unlock", description="Restore up to five previously locked channels")
    @app_commands.describe(reason="Optional reason recorded in the audit log")
    async def unlock(self, interaction: discord.Interaction, reason: str | None = None) -> None:
        if not await require_admin(interaction):
            return
        await send_interaction_message(interaction, "Select the channels to restore, then apply.", view=ChannelActionView(self, "unlock", reason=reason))

    @app_commands.command(name="slowmode", description="Set slowmode on up to five text/forum channels")
    @app_commands.describe(delay="Duration such as 30s, 10m, or 2h", reason="Optional reason recorded in the audit log")
    async def slowmode(self, interaction: discord.Interaction, delay: str, reason: str | None = None) -> None:
        if not await require_admin(interaction):
            return
        try:
            seconds = parse_duration(delay)
        except ValueError as error:
            await send_interaction_message(interaction, str(error))
            return
        await send_interaction_message(interaction, "Select the channels to update, then apply.", view=ChannelActionView(self, "slowmode", seconds=seconds, reason=reason))


async def require_admin_without_response(interaction: discord.Interaction) -> bool:
    permissions = getattr(interaction, "permissions", None)
    user_permissions = getattr(getattr(interaction, "user", None), "guild_permissions", None)
    return bool(
        interaction.guild
        and (getattr(permissions, "administrator", False) or getattr(user_permissions, "administrator", False))
    )


def serialize_overwrites(channel: Any) -> list[dict[str, int | str]]:
    serialized: list[dict[str, int | str]] = []
    for target, overwrite in channel.overwrites.items():
        if isinstance(target, discord.Role):
            target_type = "role"
        elif isinstance(target, discord.Member):
            target_type = "member"
        elif getattr(target, "type", None) is discord.Role:
            target_type = "role"
        elif getattr(target, "type", None) is discord.Member:
            target_type = "member"
        else:
            target_type = "role" if getattr(target, "is_default", lambda: False)() else "unknown"
        if target_type == "unknown":
            continue
        allow, deny = overwrite.pair()
        serialized.append({"type": target_type, "id": int(target.id), "allow": int(allow.value), "deny": int(deny.value)})
    return serialized


def deserialize_overwrites(guild: discord.Guild, entries: Iterable[dict[str, Any]]) -> tuple[dict[Any, discord.PermissionOverwrite], list[dict[str, Any]]]:
    overwrites: dict[Any, discord.PermissionOverwrite] = {}
    skipped: list[dict[str, Any]] = []
    for entry in entries:
        target_type = entry.get("type")
        if target_type == "role":
            target = guild.get_role(int(entry["id"])) or discord.Object(int(entry["id"]), type=discord.Role)
        elif target_type == "member":
            target = guild.get_member(int(entry["id"])) or discord.Object(int(entry["id"]), type=discord.Member)
        else:
            skipped.append(entry)
            continue
        overwrites[target] = discord.PermissionOverwrite.from_pair(
            discord.Permissions(int(entry.get("allow", 0))),
            discord.Permissions(int(entry.get("deny", 0))),
        )
    return overwrites, skipped
