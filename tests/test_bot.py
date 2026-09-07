"""Local regression tests. No Discord login, production token, or production DB."""
import asyncio
import datetime
import time
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from src import main as app


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = app.LevelBot()
        self.bot.db_path = Path(self.temp.name) / "test.db"
        root = Path(self.temp.name)
        with patch.object(app.tasks.Loop, "start"), patch.object(self.bot.tree, "sync", new=AsyncMock()), \
             patch.multiple(app, DATA_DIR=root, RANK_CARD_DIR=root / "cards", FONT_DIR=root / "fonts"):
            await self.bot.setup_hook()
        self.global_bot = patch.object(app, "bot", self.bot)
        self.global_bot.start()
        self.guild = SimpleNamespace(id=10, get_channel=lambda _: None, get_member=lambda _: self.member)
        self.member = SimpleNamespace(id=20, guild=self.guild, roles=[], mention="<@20>", bot=False)
        self.bot.sync_level_roles_for_member = AsyncMock()
        self.bot.announce_lifecycle = AsyncMock()
        await self.bot.ensure_user_record(self.member)
        await self.bot.db.commit()

    async def asyncTearDown(self):
        await self.bot.close()
        self.global_bot.stop()
        self.temp.cleanup()

    async def row(self, sql, params=()):
        async with self.bot.db.execute(sql, params) as cursor:
            return await cursor.fetchone()


    async def test_concurrent_xp_preserves_every_award(self):
        await asyncio.gather(*(self.bot.add_xp(self.member, 10) for _ in range(30)))
        xp, level, weekly = await self.row("SELECT xp, level, weekly_xp FROM users")
        self.assertEqual(app.total_xp_for_state(level, xp), 300)
        self.assertEqual(weekly, 300)

    async def test_failed_role_update_does_not_lose_level(self):
        self.bot.sync_level_roles_for_member.side_effect = discord.HTTPException(SimpleNamespace(status=500, reason="test"), "test")
        await self.bot.add_xp(self.member, 200)
        self.assertEqual(await self.row("SELECT level, xp, weekly_xp FROM users"), (2, 45, 200))

    async def test_chat_burst_awards_once(self):
        self.bot.delete_quiet_event_end_notice = AsyncMock()
        self.bot.process_commands = AsyncMock()
        message = SimpleNamespace(author=self.member, guild=self.guild, content="hello", channel=SimpleNamespace(id=30, send=AsyncMock()))
        with patch.object(app.random, "randint", return_value=20):
            await asyncio.gather(*(app.on_message(message) for _ in range(12)))
        self.assertEqual(await self.row("SELECT weekly_xp, message_count FROM users"), (45, 12))

    async def test_failed_notice_still_consumes_cooldown(self):
        self.bot.delete_quiet_event_end_notice = AsyncMock()
        self.bot.process_commands = AsyncMock()
        await self.bot.db.execute("UPDATE users SET xp=150")
        await self.bot.db.commit()
        send = AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=403, reason="test"), "test"))
        message = SimpleNamespace(author=self.member, guild=self.guild, content="hello", channel=SimpleNamespace(id=30, send=send))
        with patch.object(app.random, "randint", return_value=20):
            await app.on_message(message)
            await app.on_message(message)
        self.assertEqual(await self.row("SELECT level, weekly_xp FROM users"), (2, 45))
        self.assertEqual(self.bot.process_commands.await_count, 2)
        self.assertEqual(send.call_args.kwargs["allowed_mentions"].users, [self.member])


    async def test_dm_commands_are_rejected(self):
        interaction = SimpleNamespace(guild=None, response=SimpleNamespace(send_message=AsyncMock()))
        self.assertFalse(await self.bot.tree.interaction_check(interaction))
        interaction.response.send_message.assert_awaited_once()

    async def test_interaction_helper_omits_none_optional_payloads(self):
        from src.support import send_interaction_message

        response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock())
        interaction = SimpleNamespace(response=response, followup=SimpleNamespace(send=AsyncMock()))
        await send_interaction_message(interaction, "ok")
        kwargs = response.send_message.await_args.kwargs
        self.assertNotIn("embed", kwargs)
        self.assertNotIn("view", kwargs)

        response = SimpleNamespace(is_done=lambda: True, send_message=AsyncMock())
        interaction = SimpleNamespace(response=response, followup=SimpleNamespace(send=AsyncMock()))
        await send_interaction_message(interaction, "ok")
        kwargs = interaction.followup.send.await_args.kwargs
        self.assertNotIn("embed", kwargs)
        self.assertNotIn("view", kwargs)

    async def test_shutdown_survives_discord_failure(self):
        self.bot.announce_lifecycle.side_effect = discord.HTTPException(SimpleNamespace(status=500, reason="test"), "test")
        await self.bot.close()
        self.assertFalse(hasattr(self.bot, "db"))
        self.assertTrue(self.bot.is_closed())


    async def test_unsafe_multipliers_rejected(self):
        for value in ("nan", "inf", "-inf", "-1", "0", "101", "garbage"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.finite_multiplier(value)
        self.assertEqual(app.finite_multiplier("1.5"), 1.5)
        self.assertFalse(self.bot.allowed_mentions.everyone)
        self.assertFalse(self.bot.allowed_mentions.roles)

    async def test_slow_discord_request_does_not_block_other_xp(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_sync(*args):
            started.set()
            await release.wait()
        self.bot.sync_level_roles_for_member.side_effect = slow_sync
        first = asyncio.create_task(self.bot.add_xp(self.member, 200))
        await asyncio.wait_for(started.wait(), 2)
        try:
            await asyncio.wait_for(self.bot.add_xp(self.member, 10), 2)
            self.assertEqual(await self.row("SELECT weekly_xp FROM users"), (210,))
        finally:
            release.set()
            await first

    async def test_rank_defers_before_image_rendering(self):
        interaction = SimpleNamespace(
            user=self.member, guild=self.guild, channel=SimpleNamespace(id=30),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
        )
        self.member.display_name = "Player"
        async def render(**kwargs):
            interaction.response.defer.assert_awaited_once()
            return "fake-card"
        with patch.object(app, "fetch_role_rewards", new=AsyncMock(return_value=(None, []))), \
             patch.object(app, "create_rank_card", new=render):
            await app.rank.callback(interaction)
        interaction.followup.send.assert_awaited_once_with(file="fake-card")

    async def test_shutdown_cancels_background_writers(self):
        await self.bot._async_setup_hook()
        self.bot._ready.set()
        self.bot.heartbeat_loop.start()
        task = self.bot.heartbeat_loop.get_task()
        await asyncio.sleep(0)
        await self.bot.close()
        self.assertTrue(task.done())
        self.assertFalse(hasattr(self.bot, "db"))

    async def test_admin_menus_recheck_current_permissions(self):
        interaction = SimpleNamespace(guild=self.guild, permissions=SimpleNamespace(administrator=False),
                                      response=SimpleNamespace(send_message=AsyncMock()))
        for component in (app.DevDashboard(), app.GlobalEventModal(), app.ConfigDashboard(), app.MultiplierModal(30)):
            self.assertFalse(await component.interaction_check(interaction))
            interaction.permissions.administrator = True
            self.assertTrue(await component.interaction_check(interaction))
            interaction.permissions.administrator = False

    async def test_concurrent_gifts_consume_cooldown_once(self):
        await self.bot.db.execute("UPDATE users SET level=150")
        await self.bot.db.commit()
        target = SimpleNamespace(id=21, guild=self.guild, mention="<@21>")
        interactions = [SimpleNamespace(user=self.member, guild=self.guild,
                        response=SimpleNamespace(send_message=AsyncMock())) for _ in range(2)]
        await asyncio.gather(*(app.boost_user.callback(i, target) for i in interactions))
        replies = [i.response.send_message.call_args.args[0] for i in interactions]
        self.assertEqual(sum("GIFT SENT" in r for r in replies), 1)
        self.assertEqual(sum("Cooldown Active" in r for r in replies), 1)
        self.assertEqual(await self.row("SELECT COUNT(*) FROM active_boosts"), (1,))

    async def test_gift_replaces_existing_target_boosts(self):
        await self.bot.db.execute("UPDATE users SET level=150")
        await self.bot.db.execute("INSERT INTO active_boosts VALUES (21,10,9999999999,2)")
        await self.bot.db.execute("INSERT INTO active_boosts VALUES (21,10,9999999999,2)")
        await self.bot.db.commit()
        target = SimpleNamespace(id=21, guild=self.guild, mention="<@21>")
        interaction = SimpleNamespace(user=self.member, guild=self.guild, response=SimpleNamespace(send_message=AsyncMock()))
        await app.boost_user.callback(interaction, target)
        self.assertEqual(await self.row("SELECT COUNT(*) FROM active_boosts"), (1,))

    async def test_concurrent_rebirth_only_happens_once(self):
        await self.bot.db.execute("UPDATE users SET level=200")
        await self.bot.db.commit()
        interactions = [SimpleNamespace(user=self.member, guild=self.guild,
                        response=SimpleNamespace(send_message=AsyncMock())) for _ in range(2)]
        await asyncio.gather(*(app.rebirth.callback(i) for i in interactions))
        self.assertEqual(await self.row("SELECT level, rebirth FROM users"), (1, 1))
        replies = [i.response.send_message.call_args.args[0] for i in interactions]
        self.assertEqual(sum("REBIRTH!" in r for r in replies), 1)

    async def test_removed_integration_is_not_registered(self):
        names = {command.name for command in self.bot.tree.walk_commands()}
        self.assertFalse(any("minecraft" in name for name in names))
        self.assertFalse(hasattr(self.bot, "start_minecraft_api"))
        async with self.bot.db.execute("SELECT name FROM sqlite_master WHERE type='table'") as cursor:
            self.assertFalse(any("minecraft" in row[0] for row in await cursor.fetchall()))

    async def test_legacy_database_is_preserved_on_startup(self):
        await self.bot.db.execute("ALTER TABLE guild_settings ADD COLUMN minecraft_daily_xp_cap INTEGER DEFAULT 1500")
        await self.bot.db.execute("CREATE TABLE minecraft_links (minecraft_uuid TEXT PRIMARY KEY, discord_id INTEGER)")
        await self.bot.db.execute("INSERT INTO minecraft_links VALUES ('legacy', 20)")
        await self.bot.db.execute("UPDATE guild_settings SET quiet_event_until=123, quiet_event_message_id=456")
        await self.bot.db.execute("UPDATE users SET xp=75, bio='Keep me'")
        await self.bot.db.commit()
        await self.bot.db.close()
        root = Path(self.temp.name)
        with patch.object(app.tasks.Loop, "start"), patch.object(self.bot.tree, "sync", new=AsyncMock()), \
             patch.multiple(app, DATA_DIR=root, RANK_CARD_DIR=root / "cards", FONT_DIR=root / "fonts"):
            await self.bot.setup_hook()
        self.assertEqual(await self.row("SELECT xp, bio FROM users"), (75, "Keep me"))
        self.assertEqual(await self.row("SELECT * FROM minecraft_links"), ("legacy", 20))
        settings = await self.bot.fetch_guild_settings(10)
        self.assertEqual((settings["quiet_event_until"], settings["quiet_event_message_id"]), (123, 456))

    async def test_lifetime_xp_survives_rebirth(self):
        await self.bot.add_xp(self.member, 250, check_achievements=False)
        lifetime = await self.row("SELECT lifetime_xp FROM users")
        self.assertEqual(lifetime, (250,))
        await self.bot.db.execute("UPDATE users SET level=200")
        await self.bot.db.commit()
        interaction = SimpleNamespace(user=self.member, guild=self.guild, response=SimpleNamespace(send_message=AsyncMock()))
        await app.rebirth.callback(interaction)
        self.assertEqual(await self.row("SELECT level, rebirth, lifetime_xp FROM users"), (1, 1, 250))

    async def test_legacy_lifetime_backfill_uses_cumulative_level_progress(self):
        await self.bot.db.execute(
            "UPDATE users SET xp=?, level=?, lifetime_xp=?, rebirth=0 WHERE user_id=? AND guild_id=?",
            (42360, 114, 42360, self.member.id, self.guild.id),
        )
        await self.bot.db.execute("DELETE FROM bot_meta WHERE key=?", ("lifetime_xp_backfill_v2",))
        await self.bot.db.commit()

        await self.bot.migrate_legacy_lifetime_xp()

        expected = app.total_xp_for_state(114, 42360)
        self.assertEqual(await self.row("SELECT lifetime_xp FROM users"), (expected,))

    async def test_achievement_unlock_records_reward(self):
        await self.bot.db.execute("UPDATE users SET message_count=1")
        await self.bot.db.commit()
        await self.bot.add_xp(self.member, 10)
        self.assertEqual(await self.row("SELECT achievement_key FROM user_achievements WHERE user_id=20"), ("first_steps",))
        self.assertEqual(await self.row("SELECT lifetime_xp FROM users"), (35,))

    async def test_weekly_challenge_completes_once(self):
        week, key, title, description, target, reward = await self.bot.ensure_weekly_challenge(10)
        member_result = await self.bot.record_challenge_progress(self.member, target, key)
        self.assertEqual(member_result[0], title)
        self.assertEqual(await self.row("SELECT completed_at IS NOT NULL FROM challenge_progress"), (1,))
        await self.bot.record_challenge_progress(self.member, target, key)
        self.assertEqual(await self.row("SELECT COUNT(*) FROM challenge_progress"), (1,))

    async def test_backup_and_restore_round_trip(self):
        await self.bot.db.execute("UPDATE users SET bio='before backup'")
        await self.bot.db.commit()
        backup = await self.bot.backup_database("test")
        await self.bot.db.execute("UPDATE users SET bio='changed'")
        await self.bot.db.commit()
        await self.bot.restore_database(backup)
        self.assertEqual(await self.row("SELECT bio FROM users"), ("before backup",))

    async def test_salary_run_is_idempotent_and_retryable(self):
        self.assertTrue(await self.bot.claim_salary_run(10, "2026-09-05T13"))
        self.assertFalse(await self.bot.claim_salary_run(10, "2026-09-05T13"))
        await self.bot.finish_salary_run(10, "2026-09-05T13", 1, 50)
        self.assertFalse(await self.bot.claim_salary_run(10, "2026-09-05T13"))

    async def test_activity_log_captures_xp_source(self):
        await self.bot.add_xp(self.member, 12, check_achievements=False)
        self.assertEqual(await self.row("SELECT activity_type, amount, details FROM activity_log WHERE user_id=20 ORDER BY id DESC LIMIT 1"), ("xp", 12, "progression"))

    async def test_support_schema_and_settings_are_guild_isolated(self):
        settings = await self.bot.support_store.get_settings(10)
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.waiting_delay_seconds, 600)
        await self.bot.support_store.update_settings(10, enabled=True, forum_channel_id=500)
        other = await self.bot.support_store.get_settings(11)
        self.assertTrue((await self.bot.support_store.get_settings(10)).enabled)
        self.assertFalse(other.enabled)
        async with self.bot.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'support_%' ORDER BY name"
        ) as cursor:
            tables = {row[0] for row in await cursor.fetchall()}
        self.assertEqual(
            tables,
            {"support_lifecycle_tags", "support_posts", "support_settings", "support_staff_roles"},
        )

    async def test_support_commands_are_registered(self):
        names = {command.qualified_name for command in self.bot.tree.walk_commands()}
        self.assertTrue({"solved", "unsolve", "incomplete-post", "lock", "unlock", "slowmode"}.issubset(names))
        self.assertTrue({"tag create", "tag use", "tag info", "tag edit", "tag delete"}.issubset(names))

    async def test_channel_select_partial_resolves_to_full_forum(self):
        from src.support import is_forum_channel

        forum = SimpleNamespace(id=500, guild=self.guild, available_tags=[], create_tag=AsyncMock())
        selected = SimpleNamespace(id=forum.id, type=discord.ChannelType.forum, resolve=lambda: forum)
        operations = self.bot.get_cog("OperationsCog")
        resolved = await operations.resolve_selected_channel(self.guild, selected)
        self.assertIs(resolved, forum)
        self.assertTrue(is_forum_channel(resolved))

    async def test_lifecycle_tag_selector_saves_each_required_state(self):
        from src.operations import LifecycleTagConfigView

        tags = [
            SimpleNamespace(id=101, name="Questions"),
            SimpleNamespace(id=102, name="In progress"),
            SimpleNamespace(id=103, name="Needs reply"),
            SimpleNamespace(id=104, name="Complete"),
        ]
        forum = SimpleNamespace(id=500, available_tags=tags)
        guild = SimpleNamespace(id=10, get_channel=lambda channel_id: forum if channel_id == forum.id else None)
        await self.bot.support_store.update_settings(10, forum_channel_id=forum.id)
        operations = self.bot.get_cog("OperationsCog")
        operations.audit_action = AsyncMock()
        view = LifecycleTagConfigView(operations, guild.id, forum, {})

        self.assertEqual(set(view.selectors), {"unanswered", "open", "waiting", "solved"})
        self.assertEqual(len([child for child in view.children if isinstance(child, discord.ui.Select)]), 4)
        for state, tag_id in zip(("unanswered", "open", "waiting", "solved"), (101, 102, 103, 104)):
            view.selected[state] = tag_id

        interaction = SimpleNamespace(
            guild=guild,
            user=self.member,
            response=SimpleNamespace(edit_message=AsyncMock()),
        )
        await view.save.callback(interaction)
        bindings = await self.bot.support_store.get_tag_bindings(10)
        self.assertEqual({state: binding[0] for state, binding in bindings.items()}, {
            "unanswered": 101,
            "open": 102,
            "waiting": 103,
            "solved": 104,
        })
        self.assertTrue(all(not managed for _tag_id, managed in bindings.values()))
        interaction.response.edit_message.assert_awaited_once()

    async def test_canned_responses_are_isolated_and_case_insensitive(self):
        store = self.bot.support_store
        await store.create_response(10, "welcome", "Welcome to Labworks.", 20)
        await store.create_response(11, "welcome", "A different guild response.", 21)
        with self.assertRaises(ValueError):
            await store.create_response(10, "WELCOME", "Duplicate.", 22)
        await store.increment_response_uses(10, "WELCOME")
        response = await store.find_response(10, "WELCOME")
        self.assertEqual((response.content, response.uses), ("Welcome to Labworks.", 1))
        self.assertEqual((await store.find_response(11, "welcome")).content, "A different guild response.")

    async def test_support_tags_and_forum_lifecycle_are_persisted(self):
        from src.operations import parse_duration
        from src.support import SupportCog

        now = datetime.datetime.now(datetime.timezone.utc)
        owner = SimpleNamespace(id=20, mention="<@20>", roles=[], bot=False)
        responder = SimpleNamespace(id=30, mention="<@30>", roles=[], bot=False)

        class Forum:
            id = 500

            def __init__(self):
                self.available_tags = [
                    SimpleNamespace(id=1, name="Unanswered"),
                    SimpleNamespace(id=2, name="Open"),
                    SimpleNamespace(id=3, name="Waiting for Reply"),
                    SimpleNamespace(id=4, name="Solved"),
                ]
                self.create_tag = AsyncMock(side_effect=AssertionError("forum tags must be selected, not created"))

            def get_tag(self, tag_id):
                return next((tag for tag in self.available_tags if tag.id == tag_id), None)

        forum = Forum()
        guild = SimpleNamespace(
            id=10,
            get_channel=lambda channel_id: forum if channel_id == forum.id else None,
            get_member=lambda user_id: owner if user_id == owner.id else responder,
        )
        test_guild = guild

        await self.bot.support_store.update_settings(10, enabled=True, forum_channel_id=forum.id)
        support = self.bot.get_cog("SupportCog")
        bindings, errors = await support.provision_tags(guild)
        self.assertEqual(bindings, {})
        self.assertEqual(len(errors), 4)
        forum.create_tag.assert_not_awaited()

        for state, tag_id in zip(("unanswered", "open", "waiting", "solved"), (1, 2, 3, 4)):
            await self.bot.support_store.set_tag_binding(10, state, tag_id, False)
        bindings, errors = await support.provision_tags(guild)
        self.assertFalse(errors)
        self.assertEqual(set(bindings), {"unanswered", "open", "waiting", "solved"})
        self.assertFalse((await self.bot.support_store.get_tag_bindings(10))["open"][1])
        forum.create_tag.assert_not_awaited()

        starter = SimpleNamespace(id=700, author=owner, content="help", created_at=now)
        sent = AsyncMock(return_value=SimpleNamespace(id=900))
        edits = []

        class Thread:
            id = 701
            parent_id = forum.id
            owner_id = owner.id
            name = "help"
            guild = test_guild
            parent = forum
            applied_tags = []
            archived = False
            locked = False
            send = sent

            async def fetch_message(self, message_id):
                return starter

            async def edit(self, **kwargs):
                edits.append(kwargs)
                if "applied_tags" in kwargs:
                    self.applied_tags = kwargs["applied_tags"]
                if "archived" in kwargs:
                    self.archived = kwargs["archived"]

        thread = Thread()
        await support.handle_thread_created(thread, starter_message=starter)
        post = await self.bot.support_store.get_post(10, thread.id)
        self.assertEqual(post.state, "unanswered")
        self.assertTrue(post.incomplete_prompt_sent)
        self.assertIn(bindings["unanswered"], {tag.id for tag in thread.applied_tags})
        await self.bot.support_store.update_post(10, thread.id, incomplete_prompt_sent=0)
        sent.reset_mock()
        await asyncio.gather(
            support.send_incomplete_prompt(thread),
            support.send_incomplete_prompt(thread),
        )
        self.assertEqual(sent.await_count, 1)

        reply = SimpleNamespace(id=702, author=responder, guild=guild, channel=thread, created_at=now + datetime.timedelta(minutes=1))
        await support.handle_thread_message(reply)
        self.assertEqual((await self.bot.support_store.get_post(10, thread.id)).state, "open")

        owner_reply = SimpleNamespace(id=703, author=owner, guild=guild, channel=thread, created_at=now + datetime.timedelta(minutes=2))
        await support.handle_thread_message(owner_reply)
        post = await self.bot.support_store.get_post(10, thread.id)
        self.assertEqual(post.last_author_id, owner.id)
        self.assertIsNotNone(post.waiting_since)

        settings = await self.bot.support_store.get_settings(10)
        await self.bot.support_store.update_post(10, thread.id, last_author_id=responder.id, last_message_at=time.time() - 25 * 3600, waiting_since=None)
        post = await self.bot.support_store.get_post(10, thread.id)
        await support.process_post(thread, post, settings)
        self.assertEqual((await self.bot.support_store.get_post(10, thread.id)).reminder_stage, 1)
        await self.bot.support_store.update_post(10, thread.id, last_message_at=time.time() - 73 * 3600)
        await support.process_post(thread, await self.bot.support_store.get_post(10, thread.id), settings)
        self.assertEqual((await self.bot.support_store.get_post(10, thread.id)).reminder_stage, 2)
        await self.bot.support_store.update_post(10, thread.id, close_at=time.time() - 1)
        await support.process_post(thread, await self.bot.support_store.get_post(10, thread.id), settings)
        self.assertIsNotNone((await self.bot.support_store.get_post(10, thread.id)).closed_at)
        self.assertTrue(thread.archived)
        self.assertGreaterEqual(len(edits), 3)
        self.assertEqual(parse_duration("10m"), 600)
        with self.assertRaises(ValueError):
            parse_duration("7h")


if __name__ == "__main__":
    unittest.main()
