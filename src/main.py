import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
import os
import aiosqlite
import random
import time
import datetime
import asyncio
import io
import math
import secrets
import string
from pathlib import Path
from dotenv import load_dotenv
from aiohttp import web

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
    PIL_AVAILABLE = True
    RESAMPLE_LANCZOS = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
except ImportError:
    PIL_AVAILABLE = False
    RESAMPLE_LANCZOS = None

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env", override=False)

TEST_GUILD_ID = 1041046184552308776
TEST_GUILD = discord.Object(id=TEST_GUILD_ID)
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"
RANK_CARD_DIR = DATA_DIR / "rank_cards"
ASSETS_DIR = PROJECT_ROOT / "assets"
FONT_DIR = ASSETS_DIR / "fonts"
DEFAULT_QUIET_EVENT_MULTIPLIER = 2.0
DEFAULT_QUIET_EVENT_MIN_SILENCE = 45 * 60
DEFAULT_QUIET_EVENT_DURATION = 20 * 60
DEFAULT_QUIET_EVENT_COOLDOWN = 3 * 60 * 60
VOICE_XP_PER_MINUTE = 10
GITHUB_SPONSORS_URL = "https://github.com/sponsors/Zaxoosh"
SPONSOR_PROMO_LINES = [
    "Sponsor from $2/mo for an XP boost, sponsor role, and access to the Sponsor Lounge.",
    "Sponsor from $5/mo to add a passive XP salary and voting power on future updates.",
    "Studio Partner sponsorship adds rank-card flair, rebirth perks, and stronger gifting perks.",
]
MINECRAFT_API_HOST = os.getenv("MINECRAFT_API_HOST", "0.0.0.0")
MINECRAFT_API_PORT = int(os.getenv("MINECRAFT_API_PORT", "8095"))
MINECRAFT_API_TOKEN = os.getenv("MINECRAFT_API_TOKEN", "")
MINECRAFT_DAILY_XP_CAP = int(os.getenv("MINECRAFT_DAILY_XP_CAP", "1500"))
MINECRAFT_LINK_CODE_TTL_SECONDS = int(os.getenv("MINECRAFT_LINK_CODE_TTL_SECONDS", "900"))
MINECRAFT_TARGET_GUILD_ID = int(os.getenv("MINECRAFT_TARGET_GUILD_ID", "0") or 0)
MINECRAFT_ANNOUNCE_ENABLED = os.getenv("MINECRAFT_ANNOUNCE_ENABLED", "false").lower() in {"1", "true", "yes", "on"}


def resolve_database_path():
    configured_path = os.getenv("LEVELBOT_DB_PATH")
    if configured_path:
        return Path(configured_path)
    docker_data_dir = Path("/data")
    if docker_data_dir.exists():
        return docker_data_dir / "levels.db"
    project_db = PROJECT_ROOT / "levels.db"
    if project_db.exists():
        return project_db
    return BASE_DIR / "levels.db"


DATABASE_PATH = resolve_database_path()

# --- ROMAN NUMERAL HELPER ---
def to_roman(num):
    val = [1000, 900, 500, 400, 100, 90, 50, 40, 10, 9, 5, 4, 1]
    syb = ["M", "CM", "D", "CD", "C", "XC", "L", "XL", "X", "IX", "V", "IV", "I"]
    roman_num = ''
    i = 0
    while num > 0:
        for _ in range(num // val[i]):
            roman_num += syb[i]
            num -= val[i]
        i += 1
    return roman_num if roman_num else "0"


def xp_needed_for_level(level: int) -> int:
    return 5 * (level ** 2) + (50 * level) + 100


def total_xp_for_state(level: int, xp: int) -> int:
    previous_levels = max(0, level - 1)
    cumulative = (
        5 * previous_levels * (previous_levels + 1) * ((2 * previous_levels) + 1) // 6
        + 25 * previous_levels * (previous_levels + 1)
        + 100 * previous_levels
    )
    return cumulative + max(0, xp)


def format_voice_time(total_minutes: int) -> str:
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours <= 0:
        return f"{minutes}M"
    if minutes == 0:
        return f"{hours}H"
    return f"{hours}H {minutes}M"


async def get_sponsor_tier_for_user(user_id: int, guild_id: int):
    async with bot.db.execute(
        "SELECT tier_name FROM sponsors WHERE user_id = ? AND guild_id = ?",
        (user_id, guild_id),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def maybe_apply_sponsor_promo(embed: discord.Embed, user_id: int, guild_id: int, chance: float = 0.18, force_show: bool = False):
    if await get_sponsor_tier_for_user(user_id, guild_id):
        return embed
    if not force_show and random.random() > chance:
        return embed

    promo_line = random.choice(SPONSOR_PROMO_LINES)
    embed.set_footer(text=f"{promo_line} {GITHUB_SPONSORS_URL}")
    return embed

# --- DB SETUP ---
class LevelBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.members = True 
        super().__init__(command_prefix='!', intents=intents)
        self.db_path = DATABASE_PATH
        self.start_time = datetime.datetime.now(datetime.timezone.utc)
        self.ready_announced = False
        self.current_lifecycle_state = "starting"
        self.minecraft_api_runner = None
        self.minecraft_api_site = None
        
    async def setup_hook(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        RANK_CARD_DIR.mkdir(parents=True, exist_ok=True)
        FONT_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.db_path.as_posix())

        # TABLES
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER, 
                guild_id INTEGER, 
                xp INTEGER DEFAULT 0, 
                weekly_xp INTEGER DEFAULT 0,
                monthly_xp INTEGER DEFAULT 0,
                message_count INTEGER DEFAULT 0,
                voice_minutes INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1, 
                rebirth INTEGER DEFAULT 0, 
                next_xp_time REAL DEFAULT 0, 
                bio TEXT DEFAULT 'No bio set.', 
                custom_msg TEXT DEFAULT NULL, 
                birthday TEXT DEFAULT NULL, 
                last_gift_used REAL DEFAULT 0, 
                PRIMARY KEY (user_id, guild_id)
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY, 
                level_channel_id INTEGER DEFAULT 0, 
                birthday_channel_id INTEGER DEFAULT 0, 
                level100_salary INTEGER DEFAULT 0,
                global_xp_mult REAL DEFAULT 1.0,
                audit_channel_id INTEGER DEFAULT 0,
                status_channel_id INTEGER DEFAULT 0,
                minecraft_announce_channel_id INTEGER DEFAULT 0,
                minecraft_announce_enabled INTEGER DEFAULT 0,
                minecraft_daily_xp_cap INTEGER DEFAULT 1500,
                quiet_event_until REAL DEFAULT 0,
                quiet_event_multiplier REAL DEFAULT 1.0,
                last_message_at REAL DEFAULT 0,
                last_quiet_event_at REAL DEFAULT 0,
                quiet_event_message_channel_id INTEGER DEFAULT 0,
                quiet_event_message_id INTEGER DEFAULT 0
            )
        """)
        await self.db.execute("CREATE TABLE IF NOT EXISTS role_multipliers (role_id INTEGER PRIMARY KEY, guild_id INTEGER, multiplier REAL)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS voice_roles (role_id INTEGER PRIMARY KEY, guild_id INTEGER)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS presence_roles (role_id INTEGER PRIMARY KEY, guild_id INTEGER, amount INTEGER)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS channel_multipliers (channel_id INTEGER PRIMARY KEY, guild_id INTEGER, multiplier REAL)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS active_boosts (user_id INTEGER, guild_id INTEGER, end_time REAL, multiplier REAL)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS level_roles (level INTEGER, role_id INTEGER, guild_id INTEGER, PRIMARY KEY (level, guild_id))")
        await self.db.execute("CREATE TABLE IF NOT EXISTS sponsors (user_id INTEGER, guild_id INTEGER, tier_name TEXT, PRIMARY KEY (user_id, guild_id))")
        await self.db.execute("CREATE TABLE IF NOT EXISTS bot_meta (key TEXT PRIMARY KEY, value TEXT)")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS minecraft_links (
                minecraft_uuid TEXT PRIMARY KEY,
                minecraft_name TEXT,
                discord_id INTEGER UNIQUE,
                linked_at TIMESTAMP
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS minecraft_link_codes (
                code TEXT PRIMARY KEY,
                discord_id INTEGER UNIQUE,
                guild_id INTEGER,
                expires_at REAL,
                created_at REAL
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS minecraft_xp_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                minecraft_uuid TEXT,
                discord_id INTEGER,
                event_type TEXT,
                event_key TEXT,
                xp_awarded INTEGER,
                created_at TIMESTAMP
            )
        """)
        await self.db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_minecraft_xp_events_idempotency
            ON minecraft_xp_events (minecraft_uuid, event_type, event_key)
        """)

        await self.ensure_column(
            "guild_settings",
            "status_channel_id",
            "INTEGER DEFAULT 0",
        )
        await self.ensure_column(
            "guild_settings",
            "quiet_event_until",
            "REAL DEFAULT 0",
        )
        await self.ensure_column(
            "guild_settings",
            "quiet_event_multiplier",
            "REAL DEFAULT 1.0",
        )
        await self.ensure_column(
            "guild_settings",
            "last_message_at",
            "REAL DEFAULT 0",
        )
        await self.ensure_column(
            "guild_settings",
            "last_quiet_event_at",
            "REAL DEFAULT 0",
        )
        await self.ensure_column(
            "guild_settings",
            "quiet_event_channel_id",
            "INTEGER DEFAULT 0",
        )
        await self.ensure_column(
            "guild_settings",
            "quiet_event_message_channel_id",
            "INTEGER DEFAULT 0",
        )
        await self.ensure_column(
            "guild_settings",
            "quiet_event_message_id",
            "INTEGER DEFAULT 0",
        )
        await self.ensure_column(
            "guild_settings",
            "minecraft_announce_channel_id",
            "INTEGER DEFAULT 0",
        )
        await self.ensure_column(
            "guild_settings",
            "minecraft_announce_enabled",
            "INTEGER DEFAULT 0",
        )
        await self.ensure_column(
            "guild_settings",
            "minecraft_daily_xp_cap",
            f"INTEGER DEFAULT {MINECRAFT_DAILY_XP_CAP}",
        )
        await self.ensure_column(
            "users",
            "voice_minutes",
            "INTEGER DEFAULT 0",
        )

        previous_clean_shutdown = (await self.get_meta("clean_shutdown", "1")) == "1"
        previous_heartbeat = float(await self.get_meta("last_heartbeat", "0") or 0)
        self.current_lifecycle_state = "restarting" if previous_heartbeat else "starting"

        await self.set_meta("clean_shutdown", "0")
        await self.set_meta("last_startup_at", str(time.time()))
        await self.db.commit()
        
        self.voice_xp_loop.start()
        self.presence_xp_loop.start()
        self.birthday_loop.start()
        self.reset_stats_loop.start()
        self.quiet_event_loop.start()
        self.heartbeat_loop.start()
        self.presence_refresh_loop.start()

        if MINECRAFT_API_TOKEN:
            await self.start_minecraft_api()
        else:
            print("Minecraft API disabled: set MINECRAFT_API_TOKEN to enable it.")

        self.tree.copy_global_to(guild=TEST_GUILD)
        await self.tree.sync(guild=TEST_GUILD)
        print(f"✅ Bot Online & Synced ({self.db_path})")
        self.previous_clean_shutdown = previous_clean_shutdown
        self.previous_heartbeat = previous_heartbeat

    async def close(self):
        if hasattr(self, "db"):
            await self.announce_lifecycle("shutdown")
            await self.set_meta("clean_shutdown", "1")
            await self.set_meta("last_shutdown_at", str(time.time()))
            await self.db.commit()
        if self.minecraft_api_runner:
            await self.minecraft_api_runner.cleanup()
        if hasattr(self, "db"):
            await self.db.close()
        await super().close()

    async def ensure_column(self, table_name, column_name, column_sql):
        async with self.db.execute(f"PRAGMA table_info({table_name})") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if column_name not in columns:
            await self.db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")

    async def get_meta(self, key, default=None):
        async with self.db.execute("SELECT value FROM bot_meta WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else default

    async def set_meta(self, key, value):
        await self.db.execute(
            "INSERT OR REPLACE INTO bot_meta (key, value) VALUES (?, ?)",
            (key, str(value)),
        )

    async def ensure_user_record(self, member: discord.Member):
        await self.db.execute(
            """
            INSERT OR IGNORE INTO users (
                user_id,
                guild_id,
                xp,
                weekly_xp,
                monthly_xp,
                message_count,
                voice_minutes,
                level,
                rebirth,
                next_xp_time,
                bio,
                custom_msg,
                birthday,
                last_gift_used
            ) VALUES (?, ?, 0, 0, 0, 0, 0, 1, 0, 0, 'No bio set.', NULL, NULL, 0)
            """,
            (member.id, member.guild.id),
        )
        await self.db.execute(
            "INSERT OR IGNORE INTO guild_settings (guild_id, last_message_at) VALUES (?, ?)",
            (member.guild.id, 0),
        )

    async def fetch_guild_settings(self, guild_id: int):
        await self.db.execute("INSERT OR IGNORE INTO guild_settings (guild_id, last_message_at) VALUES (?, ?)", (guild_id, 0))
        async with self.db.execute(
            """
            SELECT level_channel_id,
                   birthday_channel_id,
                   level100_salary,
                   global_xp_mult,
                   audit_channel_id,
                   status_channel_id,
                   quiet_event_channel_id,
                   minecraft_announce_channel_id,
                   minecraft_announce_enabled,
                   minecraft_daily_xp_cap,
                   quiet_event_until,
                   quiet_event_multiplier,
                   last_message_at,
                   last_quiet_event_at,
                   quiet_event_message_channel_id,
                   quiet_event_message_id
            FROM guild_settings
            WHERE guild_id = ?
            """,
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return {
            "level_channel_id": row[0],
            "birthday_channel_id": row[1],
            "level100_salary": row[2],
            "global_xp_mult": row[3],
            "audit_channel_id": row[4],
            "status_channel_id": row[5],
            "quiet_event_channel_id": row[6],
            "minecraft_announce_channel_id": row[7],
            "minecraft_announce_enabled": row[8],
            "minecraft_daily_xp_cap": row[9],
            "quiet_event_until": row[10],
            "quiet_event_multiplier": row[11],
            "last_message_at": row[12],
            "last_quiet_event_at": row[13],
            "quiet_event_message_channel_id": row[14],
            "quiet_event_message_id": row[15],
        }

    def get_configured_channel(self, guild: discord.Guild, channel_id: int):
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    async def get_announcement_channel(self, guild: discord.Guild):
        settings = await self.fetch_guild_settings(guild.id)
        for key in ("status_channel_id", "level_channel_id", "audit_channel_id", "birthday_channel_id"):
            channel = self.get_configured_channel(guild, settings.get(key, 0) if settings else 0)
            if channel:
                return channel
        if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
            return guild.system_channel
        for channel in guild.text_channels:
            perms = channel.permissions_for(guild.me)
            if perms.send_messages and perms.embed_links:
                return channel
        return None

    async def get_quiet_event_channel(self, guild: discord.Guild):
        settings = await self.fetch_guild_settings(guild.id)
        quiet_channel = self.get_configured_channel(guild, settings.get("quiet_event_channel_id", 0) if settings else 0)
        if quiet_channel:
            return quiet_channel
        return await self.get_announcement_channel(guild)

    def build_quiet_event_embed(self, event_name: str):
        embed = discord.Embed(color=discord.Color.blurple())
        if event_name == "quiet_event_start":
            embed.title = "🌙 Quiet Hours XP Event"
            embed.description = (
                f"Chat has gone quiet, so XP is temporarily boosted to "
                f"**x{DEFAULT_QUIET_EVENT_MULTIPLIER}** for a little while."
            )
            embed.add_field(
                name="How it works",
                value="Send messages as normal while the event is active. This notice will update when the boost ends.",
                inline=False,
            )
        elif event_name == "quiet_event_end":
            embed.title = "🌤️ Quiet Hours XP Event Ended"
            embed.description = "The temporary quiet-hours XP boost has ended."
            embed.set_footer(text="This notice will clean itself up when chat resumes.")
        return embed

    async def clear_quiet_event_message_record(self, guild_id: int):
        await self.db.execute(
            """
            UPDATE guild_settings
            SET quiet_event_message_channel_id = 0,
                quiet_event_message_id = 0
            WHERE guild_id = ?
            """,
            (guild_id,),
        )

    async def fetch_quiet_event_message(self, guild: discord.Guild, settings: dict):
        channel_id = settings.get("quiet_event_message_channel_id", 0) if settings else 0
        message_id = settings.get("quiet_event_message_id", 0) if settings else 0
        channel = self.get_configured_channel(guild, channel_id)
        if not channel or not message_id:
            return None
        try:
            return await channel.fetch_message(int(message_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def announce_quiet_event(self, event_name: str, guild: discord.Guild):
        settings = await self.fetch_guild_settings(guild.id)
        channel = await self.get_quiet_event_channel(guild)
        if not channel:
            return

        embed = self.build_quiet_event_embed(event_name)
        if event_name == "quiet_event_start":
            old_message = await self.fetch_quiet_event_message(guild, settings)
            if old_message:
                try:
                    await old_message.delete()
                except (discord.Forbidden, discord.HTTPException):
                    pass

            try:
                message = await channel.send(embed=embed)
            except discord.Forbidden:
                return

            await self.db.execute(
                """
                UPDATE guild_settings
                SET quiet_event_message_channel_id = ?,
                    quiet_event_message_id = ?
                WHERE guild_id = ?
                """,
                (channel.id, message.id, guild.id),
            )
            await self.db.commit()
            return

        if event_name == "quiet_event_end":
            message = await self.fetch_quiet_event_message(guild, settings)
            if message:
                try:
                    await message.edit(embed=embed)
                    return
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    await self.clear_quiet_event_message_record(guild.id)
                    await self.db.commit()

            try:
                message = await channel.send(embed=embed)
            except discord.Forbidden:
                return

            await self.db.execute(
                """
                UPDATE guild_settings
                SET quiet_event_message_channel_id = ?,
                    quiet_event_message_id = ?
                WHERE guild_id = ?
                """,
                (channel.id, message.id, guild.id),
            )
            await self.db.commit()

    async def delete_quiet_event_end_notice(self, guild: discord.Guild):
        settings = await self.fetch_guild_settings(guild.id)
        if not settings or settings.get("quiet_event_until", 0):
            return
        if not settings.get("quiet_event_message_id", 0):
            return

        message = await self.fetch_quiet_event_message(guild, settings)
        if message:
            try:
                await message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        await self.clear_quiet_event_message_record(guild.id)
        await self.db.commit()

    async def announce_lifecycle(self, event_name: str, target_guild: discord.Guild = None):
        guilds = [target_guild] if target_guild else list(self.guilds)
        for guild in guilds:
            if not guild:
                continue
            if event_name in {"quiet_event_start", "quiet_event_end"}:
                await self.announce_quiet_event(event_name, guild)
                continue
            else:
                channel = await self.get_announcement_channel(guild)
            if not channel:
                continue

            embed = discord.Embed(color=discord.Color.blurple())
            if event_name == "startup":
                state_label = "Restarted" if self.current_lifecycle_state == "restarting" else "Started"
                embed.title = f"🤖 Bot {state_label}"
                embed.description = "The leveling bot is online and ready."
                if not self.previous_clean_shutdown and self.previous_heartbeat:
                    downtime = max(0, int(time.time() - self.previous_heartbeat))
                    embed.add_field(
                        name="Recovery",
                        value=f"Recovered after approximately **{downtime // 60}m {downtime % 60}s** of downtime.",
                        inline=False,
                    )
                embed.add_field(name="Database", value=f"`{self.db_path.name}` is connected.", inline=False)
            elif event_name == "shutdown":
                embed.title = "🛑 Bot Shutting Down"
                embed.description = "The leveling bot is going offline for a restart or maintenance."
            else:
                continue

            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                continue

    async def run_health_check_for_guild(self, guild: discord.Guild):
        findings = []
        member = guild.me or guild.get_member(self.user.id)
        settings = await self.fetch_guild_settings(guild.id)

        if not member:
            return ["Bot member is not available in this guild yet."]

        required_channel_perms = []
        if settings:
            for label, key in (
                ("Level-up channel", "level_channel_id"),
                ("Birthday channel", "birthday_channel_id"),
                ("Audit channel", "audit_channel_id"),
                ("Status channel", "status_channel_id"),
                ("Quiet event channel", "quiet_event_channel_id"),
            ):
                channel = self.get_configured_channel(guild, settings.get(key, 0))
                if settings.get(key, 0) and not channel:
                    findings.append(f"{label} is configured but missing or not a text channel.")
                elif channel:
                    perms = channel.permissions_for(member)
                    if not perms.send_messages:
                        findings.append(f"{label} `{channel.name}` is missing `Send Messages`.")
                    if not perms.embed_links:
                        findings.append(f"{label} `{channel.name}` is missing `Embed Links`.")
                    if not perms.attach_files:
                        required_channel_perms.append(f"{label} `{channel.name}` is missing `Attach Files` for rank cards.")

        async with self.db.execute("SELECT level, role_id FROM level_roles WHERE guild_id = ?", (guild.id,)) as cursor:
            level_roles = await cursor.fetchall()
        if not level_roles:
            findings.append("No level reward roles are configured yet.")
        else:
            for level, role_id in level_roles:
                role = guild.get_role(role_id)
                if not role:
                    findings.append(f"Level role for level {level} points to a deleted role (`{role_id}`).")
                    continue
                if role >= member.top_role:
                    findings.append(f"Level role `{role.name}` is above the bot's highest role and cannot be assigned.")

        async with self.db.execute("SELECT role_id FROM role_multipliers WHERE guild_id = ?", (guild.id,)) as cursor:
            multiplier_roles = [row[0] for row in await cursor.fetchall()]
        async with self.db.execute("SELECT role_id FROM presence_roles WHERE guild_id = ?", (guild.id,)) as cursor:
            salary_roles = [row[0] for row in await cursor.fetchall()]
        async with self.db.execute("SELECT role_id FROM voice_roles WHERE guild_id = ?", (guild.id,)) as cursor:
            voice_roles = [row[0] for row in await cursor.fetchall()]

        for role_id in set(multiplier_roles + salary_roles + voice_roles):
            if not guild.get_role(role_id):
                findings.append(f"Role config references deleted role `{role_id}`.")

        findings.extend(required_channel_perms)
        return findings

    async def sync_level_roles_for_member(self, member: discord.Member, known_level: int = None):
        async with self.db.execute(
            "SELECT level, role_id FROM level_roles WHERE guild_id = ? ORDER BY level DESC",
            (member.guild.id,),
        ) as cursor:
            role_data = await cursor.fetchall()

        if not role_data:
            return False

        user_level = known_level
        if user_level is None:
            async with self.db.execute(
                "SELECT level FROM users WHERE user_id = ? AND guild_id = ?",
                (member.id, member.guild.id),
            ) as cursor:
                row = await cursor.fetchone()
            user_level = row[0] if row else 1

        correct_role_id = None
        for level_required, role_id in role_data:
            if user_level >= level_required:
                correct_role_id = role_id
                break

        all_level_role_ids = {row[1] for row in role_data}
        roles_to_remove = []
        roles_to_add = []
        current_role_ids = {role.id for role in member.roles}

        if correct_role_id and correct_role_id not in current_role_ids:
            role = member.guild.get_role(correct_role_id)
            if role:
                roles_to_add.append(role)

        for role in member.roles:
            if role.id in all_level_role_ids and role.id != correct_role_id:
                roles_to_remove.append(role)

        if not roles_to_add and not roles_to_remove:
            return False

        try:
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove)
            if roles_to_add:
                await member.add_roles(*roles_to_add)
            return True
        except discord.Forbidden:
            return False

    async def start_minecraft_api(self):
        app = web.Application()
        app.router.add_post("/minecraft/activity", self.handle_minecraft_activity)
        app.router.add_get("/minecraft/health", self.handle_minecraft_health)
        self.minecraft_api_runner = web.AppRunner(app)
        await self.minecraft_api_runner.setup()
        self.minecraft_api_site = web.TCPSite(self.minecraft_api_runner, MINECRAFT_API_HOST, MINECRAFT_API_PORT)
        await self.minecraft_api_site.start()
        print(f"Minecraft API listening on {MINECRAFT_API_HOST}:{MINECRAFT_API_PORT}")

    async def handle_minecraft_health(self, request):
        return web.json_response({"ok": True, "service": "labworks-minecraft-api"})

    def minecraft_api_authorized(self, request):
        auth_header = request.headers.get("Authorization", "")
        bearer_token = auth_header.removeprefix("Bearer ").strip()
        header_token = request.headers.get("X-API-Token", "").strip()
        return secrets.compare_digest(bearer_token or header_token, MINECRAFT_API_TOKEN)

    async def handle_minecraft_activity(self, request):
        if not MINECRAFT_API_TOKEN or not self.minecraft_api_authorized(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

        event_type = str(payload.get("event_type", "")).strip().lower()
        if event_type == "link":
            return await self.handle_minecraft_link_payload(payload)

        minecraft_uuid = str(payload.get("minecraft_uuid", "")).strip()
        minecraft_name = str(payload.get("minecraft_name", "")).strip()[:32]
        event_key = str(payload.get("event_key", "")).strip()
        xp_requested = int(payload.get("xp", 0) or 0)

        if not minecraft_uuid or not event_type or not event_key or xp_requested <= 0:
            return web.json_response({"ok": False, "error": "missing_required_fields"}, status=400)

        async with self.db.execute(
            "SELECT discord_id FROM minecraft_links WHERE minecraft_uuid = ?",
            (minecraft_uuid,),
        ) as cursor:
            link = await cursor.fetchone()
        if not link:
            return web.json_response({"ok": False, "error": "minecraft_account_not_linked"}, status=404)

        discord_id = int(link[0])
        member = self.find_minecraft_reward_member(discord_id)
        if not member:
            return web.json_response({"ok": False, "error": "discord_member_not_found"}, status=404)

        duplicate = await self.minecraft_event_exists(minecraft_uuid, event_type, event_key)
        if duplicate:
            return web.json_response({"ok": True, "duplicate": True, "xp_awarded": 0})

        settings = await self.fetch_guild_settings(member.guild.id)
        daily_cap = int(settings.get("minecraft_daily_xp_cap") or MINECRAFT_DAILY_XP_CAP) if settings else MINECRAFT_DAILY_XP_CAP
        today_awarded = await self.get_minecraft_daily_xp(discord_id)
        remaining_cap = max(0, daily_cap - today_awarded)
        xp_awarded = min(xp_requested, remaining_cap)

        await self.log_minecraft_xp_event(
            minecraft_uuid=minecraft_uuid,
            discord_id=discord_id,
            event_type=event_type,
            event_key=event_key,
            xp_awarded=xp_awarded,
        )

        if minecraft_name:
            await self.db.execute(
                "UPDATE minecraft_links SET minecraft_name = ? WHERE minecraft_uuid = ?",
                (minecraft_name, minecraft_uuid),
            )

        if xp_awarded > 0:
            await self.add_xp(member, xp_awarded, can_announce_level_up=False)
            await self.announce_minecraft_xp(member, minecraft_name or minecraft_uuid, event_type, xp_awarded)
        else:
            await self.db.commit()

        return web.json_response({
            "ok": True,
            "discord_id": discord_id,
            "xp_awarded": xp_awarded,
            "daily_cap": daily_cap,
            "daily_awarded": today_awarded + xp_awarded,
        })

    async def handle_minecraft_link_payload(self, payload):
        code = str(payload.get("code", "")).strip().upper()
        minecraft_uuid = str(payload.get("minecraft_uuid", "")).strip()
        minecraft_name = str(payload.get("minecraft_name", "")).strip()[:32]
        now = time.time()

        if not code or not minecraft_uuid:
            return web.json_response({"ok": False, "error": "missing_link_fields"}, status=400)

        async with self.db.execute(
            "SELECT discord_id, guild_id, expires_at FROM minecraft_link_codes WHERE code = ?",
            (code,),
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            return web.json_response({"ok": False, "error": "invalid_code"}, status=404)

        discord_id, guild_id, expires_at = row
        if expires_at < now:
            await self.db.execute("DELETE FROM minecraft_link_codes WHERE code = ?", (code,))
            await self.db.commit()
            return web.json_response({"ok": False, "error": "expired_code"}, status=410)

        await self.db.execute("DELETE FROM minecraft_links WHERE discord_id = ? OR minecraft_uuid = ?", (discord_id, minecraft_uuid))
        await self.db.execute(
            """
            INSERT INTO minecraft_links (minecraft_uuid, minecraft_name, discord_id, linked_at)
            VALUES (?, ?, ?, ?)
            """,
            (minecraft_uuid, minecraft_name, discord_id, datetime.datetime.utcnow().isoformat()),
        )
        await self.db.execute("DELETE FROM minecraft_link_codes WHERE code = ?", (code,))
        await self.db.commit()

        guild = self.get_guild(int(guild_id)) if guild_id else None
        member = guild.get_member(int(discord_id)) if guild else self.find_minecraft_reward_member(int(discord_id))
        if member:
            try:
                await member.send(f"Your Minecraft account **{minecraft_name or minecraft_uuid}** is now linked.")
            except discord.Forbidden:
                pass

        return web.json_response({"ok": True, "discord_id": int(discord_id), "minecraft_uuid": minecraft_uuid})

    def find_minecraft_reward_member(self, discord_id: int):
        if MINECRAFT_TARGET_GUILD_ID:
            guild = self.get_guild(MINECRAFT_TARGET_GUILD_ID)
            return guild.get_member(discord_id) if guild else None

        for guild in self.guilds:
            member = guild.get_member(discord_id)
            if member:
                return member
        return None

    async def minecraft_event_exists(self, minecraft_uuid: str, event_type: str, event_key: str):
        async with self.db.execute(
            """
            SELECT 1 FROM minecraft_xp_events
            WHERE minecraft_uuid = ? AND event_type = ? AND event_key = ?
            """,
            (minecraft_uuid, event_type, event_key),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def get_minecraft_daily_xp(self, discord_id: int):
        start_of_day = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        async with self.db.execute(
            """
            SELECT COALESCE(SUM(xp_awarded), 0)
            FROM minecraft_xp_events
            WHERE discord_id = ? AND created_at >= ?
            """,
            (discord_id, start_of_day),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0] or 0)

    async def log_minecraft_xp_event(self, minecraft_uuid: str, discord_id: int, event_type: str, event_key: str, xp_awarded: int):
        await self.db.execute(
            """
            INSERT INTO minecraft_xp_events (
                minecraft_uuid,
                discord_id,
                event_type,
                event_key,
                xp_awarded,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                minecraft_uuid,
                discord_id,
                event_type,
                event_key,
                xp_awarded,
                datetime.datetime.utcnow().isoformat(),
            ),
        )

    async def announce_minecraft_xp(self, member: discord.Member, minecraft_name: str, event_type: str, xp_awarded: int):
        settings = await self.fetch_guild_settings(member.guild.id)
        enabled = bool(settings.get("minecraft_announce_enabled")) if settings else MINECRAFT_ANNOUNCE_ENABLED
        if not enabled:
            return

        channel = self.get_configured_channel(member.guild, settings.get("minecraft_announce_channel_id", 0) if settings else 0)
        if not channel:
            channel = await self.get_announcement_channel(member.guild)
        if not channel:
            return

        try:
            await channel.send(f"Minecraft XP: **{minecraft_name}** earned **{xp_awarded} XP** for `{event_type}`.")
        except discord.Forbidden:
            pass
    
    # --- LOGIC ---
    async def add_xp(self, member, amount, is_salary=False, can_announce_level_up=False):
        if amount <= 0:
            await self.ensure_user_record(member)
            await self.db.commit()
            return False, 1, None

        await self.ensure_user_record(member)

        # 1. Fetch User Data
        async with self.db.execute("SELECT xp, level, rebirth, custom_msg FROM users WHERE user_id = ? AND guild_id = ?", (member.id, member.guild.id)) as cursor:
            data = await cursor.fetchone()

        current_xp, current_level, current_rebirth, custom_msg = data
        
        # 2. Fetch Multipliers
        rebirth_mult = 1.0 + (current_rebirth * 0.2)
        role_mult = await calculate_multiplier(member)
        
        temp_mult = 1.0
        now = time.time()
        await self.db.execute("DELETE FROM active_boosts WHERE end_time < ?", (now,))
        async with self.db.execute("SELECT multiplier FROM active_boosts WHERE user_id=? AND guild_id=?", (member.id, member.guild.id)) as c:
            boost_data = await c.fetchone()
            if boost_data: temp_mult = boost_data[0]
            
        settings = await self.fetch_guild_settings(member.guild.id)
        global_mult = settings["global_xp_mult"] if settings else 1.0
        audit_id = settings["audit_channel_id"] if settings else 0
        quiet_event_mult = 1.0
        if settings and settings["quiet_event_until"] and settings["quiet_event_until"] > now:
            quiet_event_mult = max(1.0, settings["quiet_event_multiplier"])

        # 3. Calculate Final
        final_xp = int(amount * rebirth_mult * role_mult * temp_mult * global_mult * quiet_event_mult)
        
        # 4. AUDIT: Suspicious Activity Check
        if final_xp > 150 and audit_id != 0 and not is_salary:
            audit_chan = member.guild.get_channel(audit_id)
            if audit_chan:
                try:
                    await audit_chan.send(f"⚠️ **SUSPICIOUS ACTIVITY**\nUser: {member.mention}\nGained: **{final_xp} XP** in one action.\nMultipliers: Rb `x{rebirth_mult}` | Role `x{role_mult}` | Global `x{global_mult}` | Quiet `x{quiet_event_mult}`")
                except discord.Forbidden:
                    pass
                except Exception as e:
                    print(f"Audit Error: {e}")

        new_xp = current_xp + final_xp
        
        # 5. Level Up Logic
        xp_needed = 5 * (current_level ** 2) + (50 * current_level) + 100
        did_level_up = False
        while new_xp >= xp_needed:
            if current_level >= 200:
                new_xp = xp_needed
                break
            current_level += 1
            new_xp = new_xp - xp_needed
            xp_needed = 5 * (current_level ** 2) + (50 * current_level) + 100
            did_level_up = True

        # 6. Role Swapping 
        if did_level_up:
            await self.sync_level_roles_for_member(member, current_level)

        # 7. Save
        await self.db.execute("""
            UPDATE users 
            SET xp = ?, weekly_xp = weekly_xp + ?, monthly_xp = monthly_xp + ?, level = ? 
            WHERE user_id = ? AND guild_id = ?
        """, (new_xp, final_xp, final_xp, current_level, member.id, member.guild.id))
        
        await self.db.commit()
        if not can_announce_level_up:
            return False, current_level, None
        return did_level_up, current_level, custom_msg

# --- SALARY DEPLOYMENT ---
    async def deploy_salaries_to_guild(self, guild: discord.Guild):
        try:
            async with self.db.execute("SELECT role_id, amount FROM presence_roles WHERE guild_id=?", (guild.id,)) as cursor:
                salaries = {row[0]: row[1] for row in await cursor.fetchall()}
                
            settings = await self.fetch_guild_settings(guild.id)
            lvl100_amount = settings["level100_salary"] if settings else 0
            audit_id = settings["audit_channel_id"] if settings else 0

            users_paid = 0
            total_xp_given = 0
            
            # Dictionary to track {Role Name : Total XP Generated} for the audit log
            breakdown = {}
            
            for member in guild.members:
                if member.bot: continue
                
                try:
                    await self.ensure_user_record(member)

                    # 1. Find the HIGHEST salary role the user currently holds
                    best_role = None
                    highest_amount = 0
                    
                    for r in member.roles:
                        if r.id in salaries and salaries[r.id] > highest_amount:
                            highest_amount = salaries[r.id]
                            best_role = r
                    
                    total = highest_amount
                    
                    # Track the role payout for the audit breakdown
                    if best_role:
                        breakdown[best_role.name] = breakdown.get(best_role.name, 0) + highest_amount
                    
                    # 2. Add Level 100 Bonus (if applicable)
                    if lvl100_amount > 0:
                        async with self.db.execute("SELECT level FROM users WHERE user_id=? AND guild_id=?", (member.id, guild.id)) as c:
                            ud = await c.fetchone()
                            if ud and ud[0] >= 100: 
                                total += lvl100_amount
                                breakdown["Level 100 Bonus"] = breakdown.get("Level 100 Bonus", 0) + lvl100_amount
                            
                    # 3. Pay the user
                    if total > 0: 
                        await self.add_xp(member, total, is_salary=True)
                        users_paid += 1
                        total_xp_given += total
                        await asyncio.sleep(0.05) 
                except Exception as e:
                    print(f"Salary Error ({member.name}): {e}")
            
            # 4. Send detailed Audit Log Embed
            if audit_id != 0 and users_paid > 0:
                audit_chan = guild.get_channel(audit_id)
                if audit_chan:
                    try:
                        desc = f"Distributed a total of **{total_xp_given:,} XP** to **{users_paid}** users.\n\n**📊 XP Distribution Breakdown:**\n"
                        
                        # Sort the breakdown highest to lowest for readability
                        sorted_breakdown = sorted(breakdown.items(), key=lambda x: x[1], reverse=True)
                        for role_name, amount in sorted_breakdown:
                            desc += f"• **{role_name}:** {amount:,} XP\n"
                            
                        embed = discord.Embed(title="💸 Hourly Salaries Deployed", description=desc, color=discord.Color.green())
                        await audit_chan.send(embed=embed)
                    except discord.Forbidden:
                        pass
                        
            return users_paid, total_xp_given

        except Exception as e:
            print(f"Fatal Salary Error in {guild.name}: {e}")
            return 0, 0

    # --- OPTIMIZED LOOPS ---
    @tasks.loop(minutes=1)
    async def voice_xp_loop(self):
        try:
            for guild in self.guilds:
                async with self.db.execute("SELECT role_id FROM voice_roles WHERE guild_id = ?", (guild.id,)) as cursor:
                    voice_ids = {row[0] for row in await cursor.fetchall()}
                if not voice_ids:
                    continue
                for member in guild.members:
                    if member.bot or not member.voice or member.voice.afk:
                        continue
                    if member.voice.self_deaf or member.voice.self_mute:
                        continue
                    if not any(r.id in voice_ids for r in member.roles):
                        continue

                    voice_channel = member.voice.channel
                    if not voice_channel:
                        continue

                    active_humans = [
                        voice_member for voice_member in voice_channel.members
                        if not voice_member.bot and not voice_member.voice.self_deaf
                    ]
                    if len(active_humans) < 2:
                        continue

                    try:
                        await self.add_xp(member, VOICE_XP_PER_MINUTE)
                        await self.db.execute(
                            "UPDATE users SET voice_minutes = voice_minutes + 1 WHERE user_id = ? AND guild_id = ?",
                            (member.id, guild.id),
                        )
                        await self.db.commit()
                        await asyncio.sleep(0.1)
                    except Exception as e:
                        print(f"Voice XP Error ({member.name}): {e}")
        except Exception as e:
            print(f"Fatal Voice Loop Error: {e}")
                        
    @voice_xp_loop.before_loop
    async def before_voice_xp(self): await self.wait_until_ready()

    @tasks.loop(hours=1)
    async def presence_xp_loop(self):
        for guild in self.guilds:
            await self.deploy_salaries_to_guild(guild)

    @presence_xp_loop.before_loop
    async def before_presence_xp(self): await self.wait_until_ready()

    @tasks.loop(time=datetime.time(hour=0, minute=0, tzinfo=datetime.timezone.utc))
    async def birthday_loop(self):
        now = datetime.datetime.utcnow()
        today_str = now.strftime("%d-%m")
        async with self.db.execute("SELECT user_id, guild_id FROM users WHERE birthday = ?", (today_str,)) as cursor:
            birthdays = await cursor.fetchall()
        for user_id, guild_id in birthdays:
            guild = self.get_guild(guild_id)
            if not guild: continue
            async with self.db.execute("SELECT birthday_channel_id FROM guild_settings WHERE guild_id=?", (guild_id,)) as c:
                s = await c.fetchone()
            if s and s[0] != 0:
                channel = guild.get_channel(s[0])
                if channel: await channel.send(f"🎂 Happy Birthday <@{user_id}>! Hope you have a fantastic day! 🎉")

    @birthday_loop.before_loop
    async def before_birthday(self): await self.wait_until_ready()

    @tasks.loop(time=datetime.time(hour=0, minute=0, tzinfo=datetime.timezone.utc))
    async def reset_stats_loop(self):
        now = datetime.datetime.utcnow()
        if now.day == 1:
            await self.db.execute("UPDATE users SET monthly_xp = 0")
        if now.weekday() == 0:
            await self.db.execute("UPDATE users SET weekly_xp = 0")
        await self.db.commit()

    @reset_stats_loop.before_loop
    async def before_reset_stats(self): await self.wait_until_ready()

    @tasks.loop(minutes=1)
    async def heartbeat_loop(self):
        await self.set_meta("last_heartbeat", str(time.time()))
        await self.db.commit()

    @heartbeat_loop.before_loop
    async def before_heartbeat(self): await self.wait_until_ready()

    @tasks.loop(minutes=15)
    async def presence_refresh_loop(self):
        await self.refresh_presence_status()

    @presence_refresh_loop.before_loop
    async def before_presence_refresh(self): await self.wait_until_ready()

    @tasks.loop(minutes=5)
    async def quiet_event_loop(self):
        now = time.time()
        for guild in self.guilds:
            settings = await self.fetch_guild_settings(guild.id)
            if not settings:
                continue

            quiet_until = settings["quiet_event_until"] or 0
            last_message_at = settings["last_message_at"] or 0
            last_quiet_event_at = settings["last_quiet_event_at"] or 0

            if quiet_until and quiet_until <= now:
                await self.db.execute(
                    """
                    UPDATE guild_settings
                    SET quiet_event_until = 0,
                        quiet_event_multiplier = 1.0
                    WHERE guild_id = ?
                    """,
                    (guild.id,),
                )
                await self.db.commit()
                await self.announce_lifecycle("quiet_event_end", guild)
                continue

            if quiet_until and quiet_until > now:
                continue

            if last_message_at <= 0:
                continue

            if (now - last_message_at) < DEFAULT_QUIET_EVENT_MIN_SILENCE:
                continue

            if (now - last_quiet_event_at) < DEFAULT_QUIET_EVENT_COOLDOWN:
                continue

            if random.random() > 0.35:
                continue

            quiet_event_until = now + DEFAULT_QUIET_EVENT_DURATION
            await self.db.execute(
                """
                UPDATE guild_settings
                SET quiet_event_until = ?,
                    quiet_event_multiplier = ?,
                    last_quiet_event_at = ?
                WHERE guild_id = ?
                """,
                (quiet_event_until, DEFAULT_QUIET_EVENT_MULTIPLIER, now, guild.id),
            )
            await self.db.commit()
            await self.announce_lifecycle("quiet_event_start", guild)

    @quiet_event_loop.before_loop
    async def before_quiet_event(self): await self.wait_until_ready()

    async def refresh_presence_status(self):
        async with self.db.execute("SELECT COUNT(DISTINCT user_id) FROM users") as cursor:
            row = await cursor.fetchone()
        tracked_users = row[0] if row and row[0] else 0
        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{tracked_users} tracked users",
            ),
        )

bot = LevelBot()

async def calculate_multiplier(member):
    async with bot.db.execute("SELECT role_id, multiplier FROM role_multipliers WHERE guild_id = ?", (member.guild.id,)) as cursor:
        rows = await cursor.fetchall()
    total = 1.0
    db_roles = {row[0]: row[1] for row in rows}
    for role in member.roles:
        if role.id in db_roles:
            bonus = db_roles[role.id] - 1.0
            if bonus > 0: total += bonus
    return total


@bot.event
async def on_ready():
    if bot.ready_announced:
        return

    bot.ready_announced = True
    await bot.refresh_presence_status()
    await bot.announce_lifecycle("startup")

    for guild in bot.guilds:
        findings = await bot.run_health_check_for_guild(guild)
        if not findings:
            continue
        channel = await bot.get_announcement_channel(guild)
        if not channel:
            continue
        embed = discord.Embed(
            title="🩺 Startup Health Check",
            description="A few things need attention before everything runs perfectly:",
            color=discord.Color.orange(),
        )
        embed.add_field(name="Findings", value="\n".join(f"• {item}" for item in findings[:15]), inline=False)
        if len(findings) > 15:
            embed.set_footer(text=f"{len(findings) - 15} additional findings omitted.")
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            continue

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    await bot.ensure_user_record(message.author)
    current_time = time.time()
    content = (message.content or "").strip()
    is_prefix_command = bool(content) and content.startswith(str(bot.command_prefix))

    await bot.delete_quiet_event_end_notice(message.guild)

    await bot.db.execute(
        "UPDATE guild_settings SET last_message_at = ? WHERE guild_id = ?",
        (current_time, message.guild.id),
    )

    if not is_prefix_command:
        await bot.db.execute(
            "UPDATE users SET message_count = message_count + 1 WHERE user_id = ? AND guild_id = ?",
            (message.author.id, message.guild.id),
        )

        channel_mult = 1.0
        async with bot.db.execute("SELECT multiplier FROM channel_multipliers WHERE channel_id=?", (message.channel.id,)) as c:
            cm_data = await c.fetchone()
            if cm_data:
                channel_mult = cm_data[0]

        async with bot.db.execute("SELECT next_xp_time FROM users WHERE user_id=? AND guild_id=?", (message.author.id, message.guild.id)) as cursor:
            data = await cursor.fetchone()

        can_gain_xp = current_time >= (data[0] if data else 0)
        if can_gain_xp and len(content) >= 3:
            leveled_up, new_level, custom_msg = await bot.add_xp(
                message.author,
                int(random.randint(15, 25) * channel_mult),
                can_announce_level_up=True,
            )

            if leveled_up:
                settings = await bot.fetch_guild_settings(message.guild.id)
                target = bot.get_configured_channel(message.guild, settings["level_channel_id"] if settings else 0) or message.channel

                if new_level == 75:
                    await target.send(f"💀 {message.author.mention} hit **Level 75**. Welcome back from inactivity...")
                elif custom_msg:
                    await target.send(custom_msg.replace("{user}", message.author.mention).replace("{level}", str(new_level)))
                else:
                    await target.send(f"🎉 {message.author.mention} reached **Level {new_level}**!")

            await bot.db.execute(
                "UPDATE users SET next_xp_time=? WHERE user_id=? AND guild_id=?",
                (current_time + random.randint(15, 30), message.author.id, message.guild.id),
            )

    await bot.db.commit()
    await bot.process_commands(message)

# =========================================
# 🎛️ DEV MENU & DASHBOARDS
# =========================================

class DevValueModal(ui.Modal, title="Update Player Stats"):
    amount = ui.TextInput(label="Enter Amount", placeholder="10")
    def __init__(self, target_user, mode):
        super().__init__()
        self.target_user = target_user
        self.mode = mode 

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = int(self.amount.value)
            col = "level" if self.mode == "level" else "rebirth"
            await bot.ensure_user_record(self.target_user)
            extra_sql = ", xp=0" if self.mode == "level" else ""
            await bot.db.execute(f"UPDATE users SET {col} = ?{extra_sql} WHERE user_id = ? AND guild_id = ?", (val, self.target_user.id, interaction.guild.id))
            await bot.db.commit()
            
            async with bot.db.execute("SELECT audit_channel_id FROM guild_settings WHERE guild_id=?", (interaction.guild.id,)) as c:
                d = await c.fetchone()
            if d and d[0] != 0:
                audit = interaction.guild.get_channel(d[0])
                if audit: await audit.send(f"🛠️ **ADMIN ACTION**\nAdmin: {interaction.user.mention}\nAction: Set {self.mode} to {val}\nTarget: {self.target_user.mention}")

            await interaction.response.send_message(f"✅ Set {self.target_user.name}'s {self.mode} to **{val}**.", ephemeral=True)
        except: await interaction.response.send_message("❌ Invalid integer.", ephemeral=True)

class GlobalEventModal(ui.Modal, title="Global XP Event"):
    mult = ui.TextInput(label="Global Multiplier (1.0 = Normal)", placeholder="2.0")
    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = float(self.mult.value)
            if val < 1.0: val = 1.0
            await bot.db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (interaction.guild.id,))
            await bot.db.execute("UPDATE guild_settings SET global_xp_mult = ? WHERE guild_id = ?", (val, interaction.guild.id))
            await bot.db.commit()
            
            msg = f"🌍 **GLOBAL EVENT ACTIVATED!** XP is now **x{val}**!" if val > 1.0 else "🌍 Global Event Ended. XP is normal."
            await interaction.response.send_message(msg, ephemeral=True)
            if val > 1.0: await interaction.channel.send(msg)
        except: await interaction.response.send_message("❌ Invalid number.", ephemeral=True)

class AuditChannelSelect(ui.ChannelSelect):
    def __init__(self):
        super().__init__(channel_types=[discord.ChannelType.text], placeholder="Select Audit Channel...")
    async def callback(self, interaction: discord.Interaction):
        await bot.db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (interaction.guild.id,))
        await bot.db.execute("UPDATE guild_settings SET audit_channel_id = ? WHERE guild_id = ?", (self.values[0].id, interaction.guild.id))
        await bot.db.commit()
        await interaction.response.send_message(f"🔒 Security Log set to {self.values[0].mention}.", ephemeral=True)


class StatusChannelSelect(ui.ChannelSelect):
    def __init__(self):
        super().__init__(channel_types=[discord.ChannelType.text], placeholder="Select Status Channel...")

    async def callback(self, interaction: discord.Interaction):
        await bot.db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (interaction.guild.id,))
        await bot.db.execute("UPDATE guild_settings SET status_channel_id = ? WHERE guild_id = ?", (self.values[0].id, interaction.guild.id))
        await bot.db.commit()
        await interaction.response.send_message(f"📡 Bot status updates will be sent to {self.values[0].mention}.", ephemeral=True)


class Level100SalaryModal(ui.Modal, title="Level 100+ Salary"):
    amount = ui.TextInput(label="Hourly XP salary", placeholder="50")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = int(self.amount.value)
            if val < 0:
                raise ValueError
            await bot.db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (interaction.guild.id,))
            await bot.db.execute("UPDATE guild_settings SET level100_salary = ? WHERE guild_id = ?", (val, interaction.guild.id))
            await bot.db.commit()
            await interaction.response.send_message(f"✅ Level 100+ salary set to **{val} XP/hr**.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ Enter a whole number that is 0 or higher.", ephemeral=True)

class PlayerDevView(ui.View):
    def __init__(self, target):
        super().__init__()
        self.target = target
    @ui.button(label="Set Level", style=discord.ButtonStyle.danger)
    async def set_lvl(self, i, b): await i.response.send_modal(DevValueModal(self.target, "level"))
    @ui.button(label="Set Rebirth", style=discord.ButtonStyle.danger)
    async def set_rb(self, i, b): await i.response.send_modal(DevValueModal(self.target, "rebirth"))

class DevUserSelect(ui.UserSelect):
    def __init__(self): super().__init__(placeholder="Select Player to Manage...")
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"🛠️ Managing **{self.values[0].name}**:", view=PlayerDevView(self.values[0]), ephemeral=True)

class DevDashboardSelect(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Player Management", description="Force Set Levels/Rebirths", emoji="👤", value="player"),
            discord.SelectOption(label="Global Events", description="Set Server-wide XP Multipliers", emoji="🌍", value="global"),
            discord.SelectOption(label="Security & Audit", description="Set Log Channel for Suspicious Activity", emoji="🔒", value="audit")
        ]
        super().__init__(placeholder="Select Developer Tool...", min_values=1, max_values=1, options=options)
    
    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "player":
            view = ui.View()
            view.add_item(DevUserSelect())
            await interaction.response.send_message("Select a player to edit:", view=view, ephemeral=True)
        elif self.values[0] == "global":
            await interaction.response.send_modal(GlobalEventModal())
        elif self.values[0] == "audit":
            view = ui.View()
            view.add_item(AuditChannelSelect())
            await interaction.response.send_message("Select a channel for Audit Logs:", view=view, ephemeral=True)

class DevDashboard(ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(DevDashboardSelect())

@bot.tree.command(name="dev", description="Open the Developer Dashboard")
@app_commands.checks.has_permissions(administrator=True)
async def dev(interaction: discord.Interaction):
    embed = discord.Embed(title="🛠️ Developer Control Center", color=discord.Color.dark_red())
    embed.add_field(name="👤 Player Man", value="Force Levels/Rebirths", inline=True)
    embed.add_field(name="🌍 Events", value="Global Multipliers", inline=True)
    embed.add_field(name="🔒 Audit", value="Log Suspicious XP", inline=True)
    await interaction.response.send_message(embed=embed, view=DevDashboard(), ephemeral=True)

@bot.tree.command(name="force_salaries", description="Manually deploy hourly salaries to all eligible users (Admin)")
@app_commands.checks.has_permissions(administrator=True)
async def force_salaries(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    users_paid, total_xp = await bot.deploy_salaries_to_guild(interaction.guild)
    await interaction.followup.send(f"✅ **Salaries Forced!**\nDistributed **{total_xp:,} XP** to **{users_paid}** users.")

# =========================================
# 🏆 LEADERBOARD SYSTEM
# =========================================

class LeaderboardSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="All-Time XP", value="xp", emoji="🏆", description="Total Experience gained forever."),
            discord.SelectOption(label="Monthly XP", value="monthly_xp", emoji="📅", description="Experience gained this month."),
            discord.SelectOption(label="Weekly XP", value="weekly_xp", emoji="⏳", description="Experience gained this week."),
            discord.SelectOption(label="Messages", value="message_count", emoji="💬", description="Total chat messages sent.")
        ]
        super().__init__(placeholder="Filter Leaderboard...", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.view.sort_col = self.values[0]
        self.view.page = 0
        await self.view.update_view(interaction)


class LeaderboardView(discord.ui.View):
    def __init__(self, interaction: discord.Interaction):
        super().__init__(timeout=180) 
        self.interaction = interaction
        self.page = 0
        self.sort_col = "xp" 
        self.show_sponsor_promo = random.random() <= 0.16
        self.add_item(LeaderboardSelect()) 

    async def generate_embed(self):
        offset = self.page * 10
        
        titles = {
            "xp": "🏆 All-Time XP Leaderboard",
            "monthly_xp": "📅 Monthly XP Leaderboard",
            "weekly_xp": "⏳ Weekly XP Leaderboard",
            "message_count": "💬 Top Chatters (Message Count)"
        }
        
        if self.sort_col == "xp":
            order_clause = "CAST(level AS INTEGER) DESC, CAST(xp AS INTEGER) DESC"
        else:
            order_clause = f"CAST({self.sort_col} AS INTEGER) DESC"

        query = f"""
            SELECT user_id, {self.sort_col}, level, rebirth 
            FROM users 
            WHERE guild_id = ? 
            ORDER BY {order_clause} 
            LIMIT 10 OFFSET ?
        """
        
        async with bot.db.execute(query, (self.interaction.guild.id, offset)) as c:
            rows = await c.fetchall()

        embed = discord.Embed(title=titles[self.sort_col], color=discord.Color.gold())
        
        if not rows and self.page == 0:
            embed.description = "No data found yet! Start chatting."
            self.next_button.disabled = True
        elif not rows:
            embed.description = "No more users found on this page."
            self.next_button.disabled = True
        else:
            desc = ""
            for index, row in enumerate(rows):
                rank = (self.page * 10) + index + 1
                uid, val, lvl, rebirth = row
                
                val = int(val) if val else 0
                rebirth_str = f" [Rb {to_roman(rebirth)}]" if rebirth and int(rebirth) > 0 else ""
                stat_str = f"**{val:,}** msgs" if self.sort_col == "message_count" else f"**{val:,}** XP"
                
                desc += f"`#{rank}` <@{uid}>{rebirth_str} • Lvl {lvl} • {stat_str}\n"
            
            embed.description = desc
            self.next_button.disabled = len(rows) < 10

        embed.set_footer(text=f"Page {self.page + 1} • Implemented on 08/02/2026. All tracking for messages started after that.")
        embed = await maybe_apply_sponsor_promo(
            embed,
            self.interaction.user.id,
            self.interaction.guild.id,
            force_show=self.show_sponsor_promo,
        )
        return embed

    async def update_view(self, interaction: discord.Interaction):
        self.prev_button.disabled = self.page == 0
        embed = await self.generate_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.blurple, disabled=True, row=1)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await self.update_view(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.blurple, row=1)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self.update_view(interaction)


@bot.tree.command(name="leaderboard", description="View the server leaderboards")
async def leaderboard(interaction: discord.Interaction):
    view = LeaderboardView(interaction)
    embed = await view.generate_embed()
    await interaction.response.send_message(embed=embed, view=view)

# =========================================
# 🎛️ CONFIG DASHBOARD
# =========================================

async def build_config_overview_embed(guild: discord.Guild):
    settings = await bot.fetch_guild_settings(guild.id)
    embed = discord.Embed(
        title="🧭 Server Config Overview",
        description="Current leveling and bot-system settings for this server.",
        color=discord.Color.teal(),
    )

    level_channel = bot.get_configured_channel(guild, settings["level_channel_id"] if settings else 0)
    birthday_channel = bot.get_configured_channel(guild, settings["birthday_channel_id"] if settings else 0)
    audit_channel = bot.get_configured_channel(guild, settings["audit_channel_id"] if settings else 0)
    status_channel = bot.get_configured_channel(guild, settings["status_channel_id"] if settings else 0)
    quiet_event_channel = bot.get_configured_channel(guild, settings["quiet_event_channel_id"] if settings else 0)

    async with bot.db.execute("SELECT COUNT(*) FROM level_roles WHERE guild_id = ?", (guild.id,)) as cursor:
        level_role_count = (await cursor.fetchone())[0]
    async with bot.db.execute("SELECT COUNT(*) FROM role_multipliers WHERE guild_id = ?", (guild.id,)) as cursor:
        role_multiplier_count = (await cursor.fetchone())[0]
    async with bot.db.execute("SELECT COUNT(*) FROM presence_roles WHERE guild_id = ?", (guild.id,)) as cursor:
        salary_role_count = (await cursor.fetchone())[0]
    async with bot.db.execute("SELECT COUNT(*) FROM voice_roles WHERE guild_id = ?", (guild.id,)) as cursor:
        voice_role_count = (await cursor.fetchone())[0]
    async with bot.db.execute("SELECT COUNT(*) FROM channel_multipliers WHERE guild_id = ?", (guild.id,)) as cursor:
        channel_multiplier_count = (await cursor.fetchone())[0]

    embed.add_field(
        name="Channels",
        value=(
            f"Level-ups: {level_channel.mention if level_channel else 'Not set'}\n"
            f"Birthdays: {birthday_channel.mention if birthday_channel else 'Not set'}\n"
            f"Audit: {audit_channel.mention if audit_channel else 'Not set'}\n"
            f"Status: {status_channel.mention if status_channel else 'Not set'}\n"
            f"Quiet hours: {quiet_event_channel.mention if quiet_event_channel else 'Not set'}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Rewards",
        value=(
            f"Level roles: **{level_role_count}**\n"
            f"Role multipliers: **{role_multiplier_count}**\n"
            f"Salary roles: **{salary_role_count}**\n"
            f"Voice XP roles: **{voice_role_count}**\n"
            f"Channel boosts: **{channel_multiplier_count}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="System",
        value=(
            f"Global XP: **x{settings['global_xp_mult'] if settings else 1.0}**\n"
            f"Level 100+ salary: **{settings['level100_salary'] if settings else 0} XP/hr**\n"
            f"Quiet event: **x{settings['quiet_event_multiplier'] if settings and settings['quiet_event_until'] and settings['quiet_event_until'] > time.time() else 1.0}**"
        ),
        inline=True,
    )
    return embed


async def build_health_check_embed(guild: discord.Guild):
    findings = await bot.run_health_check_for_guild(guild)
    if findings:
        embed = discord.Embed(
            title="🩺 Health Check",
            description="A few issues or missing pieces were found.",
            color=discord.Color.orange(),
        )
        embed.add_field(name="Findings", value="\n".join(f"• {item}" for item in findings[:20]), inline=False)
        if len(findings) > 20:
            embed.set_footer(text=f"{len(findings) - 20} additional findings omitted.")
    else:
        embed = discord.Embed(
            title="🩺 Health Check",
            description="Everything important looks healthy right now.",
            color=discord.Color.green(),
        )
    return embed

class MultiplierModal(ui.Modal, title="XP Multiplier"):
    amount = ui.TextInput(label="Multiplier", placeholder="1.5")
    def __init__(self, target_id, is_role=True):
        super().__init__()
        self.target_id, self.is_role = target_id, is_role
    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = float(self.amount.value)
            if val < 1.0: raise ValueError
            table, col = ("role_multipliers", "role_id") if self.is_role else ("channel_multipliers", "channel_id")
            await bot.db.execute(f"INSERT OR REPLACE INTO {table} ({col}, guild_id, multiplier) VALUES (?, ?, ?)", (self.target_id, interaction.guild.id, val))
            await bot.db.commit()
            await interaction.response.send_message(f"✅ Set **x{val}** multiplier.", ephemeral=True)
        except: await interaction.response.send_message("❌ Invalid number.", ephemeral=True)

class SalaryModal(ui.Modal, title="Hourly Salary"):
    amount = ui.TextInput(label="XP Amount", placeholder="50")
    def __init__(self, role_id):
        super().__init__()
        self.role_id = role_id
    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = int(self.amount.value)
            await bot.db.execute("INSERT OR REPLACE INTO presence_roles (role_id, guild_id, amount) VALUES (?, ?, ?)", (self.role_id, interaction.guild.id, val))
            await bot.db.commit()
            await interaction.response.send_message(f"✅ Set salary to **{val} XP/hr**.", ephemeral=True)
        except: await interaction.response.send_message("❌ Invalid integer.", ephemeral=True)

class LevelRoleModal(ui.Modal, title="Level Requirement"):
    level = ui.TextInput(label="Level to unlock role", placeholder="10")
    def __init__(self, role_id):
        super().__init__()
        self.role_id = role_id
    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = int(self.level.value)
            if val < 2: raise ValueError
            await bot.db.execute("INSERT OR REPLACE INTO level_roles (level, role_id, guild_id) VALUES (?, ?, ?)", (val, self.role_id, interaction.guild.id))
            await bot.db.commit()
            await interaction.response.send_message(f"✅ Role will be given at **Level {val}**.", ephemeral=True)
        except: await interaction.response.send_message("❌ Invalid Level (Must be 2+).", ephemeral=True)

class RoleActionView(ui.View):
    def __init__(self, role):
        super().__init__()
        self.role = role
    @ui.button(label="XP Boost", style=discord.ButtonStyle.blurple)
    async def set_boost(self, i, b): await i.response.send_modal(MultiplierModal(self.role.id, True))
    @ui.button(label="Salary", style=discord.ButtonStyle.green)
    async def set_salary(self, i, b): await i.response.send_modal(SalaryModal(self.role.id))
    @ui.button(label="Voice XP Toggle", style=discord.ButtonStyle.secondary)
    async def set_voice(self, i, b):
        async with bot.db.execute("SELECT 1 FROM voice_roles WHERE role_id = ? AND guild_id = ?", (self.role.id, i.guild.id)) as cursor:
            exists = await cursor.fetchone()
        if exists:
            await bot.db.execute("DELETE FROM voice_roles WHERE role_id = ? AND guild_id = ?", (self.role.id, i.guild.id))
            message = "✅ Voice XP disabled for this role."
        else:
            await bot.db.execute("INSERT OR IGNORE INTO voice_roles (role_id, guild_id) VALUES (?, ?)", (self.role.id, i.guild.id))
            message = "✅ Voice XP enabled for this role."
        await bot.db.commit()
        await i.response.send_message(message, ephemeral=True)
    @ui.button(label="Assign to Level", style=discord.ButtonStyle.primary)
    async def set_lvl(self, i, b): await i.response.send_modal(LevelRoleModal(self.role.id))

class ChannelActionView(ui.View):
    def __init__(self, channel):
        super().__init__()
        self.channel = channel
    @ui.button(label="XP Boost", style=discord.ButtonStyle.blurple)
    async def set_boost(self, i, b): await i.response.send_modal(MultiplierModal(self.channel.id, False))
    @ui.button(label="Route Level Ups", style=discord.ButtonStyle.green)
    async def set_route(self, i, b):
        await bot.db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (i.guild.id,))
        await bot.db.execute("UPDATE guild_settings SET level_channel_id = ? WHERE guild_id = ?", (self.channel.id, i.guild.id))
        await bot.db.commit()
        await i.response.send_message(f"✅ Routed to {self.channel.mention}.", ephemeral=True)
    @ui.button(label="Route Birthdays", style=discord.ButtonStyle.primary)
    async def set_bday(self, i, b):
        await bot.db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (i.guild.id,))
        await bot.db.execute("UPDATE guild_settings SET birthday_channel_id = ? WHERE guild_id = ?", (self.channel.id, i.guild.id))
        await bot.db.commit()
        await i.response.send_message(f"✅ Routed to {self.channel.mention}.", ephemeral=True)
    @ui.button(label="Route Quiet Hours", style=discord.ButtonStyle.secondary)
    async def set_quiet(self, i, b):
        await bot.db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (i.guild.id,))
        await bot.db.execute("UPDATE guild_settings SET quiet_event_channel_id = ? WHERE guild_id = ?", (self.channel.id, i.guild.id))
        await bot.db.commit()
        await i.response.send_message(f"✅ Quiet hours announcements routed to {self.channel.mention}.", ephemeral=True)

class SponsorTierSelect(ui.Select):
    def __init__(self, target_user):
        self.target_user = target_user
        options = [
            discord.SelectOption(label="Intern", description="$2-5 Tier", emoji="🟢", value="Intern"),
            discord.SelectOption(label="Alpha Tester", description="$10-15 Tier", emoji="🔵", value="Alpha Tester"),
            discord.SelectOption(label="Studio Partner", description="$30+ Tier", emoji="🟡", value="Studio Partner")
        ]
        super().__init__(placeholder="Select Tier...", min_values=1, max_values=1, options=options)
    async def callback(self, interaction: discord.Interaction):
        await bot.db.execute("INSERT OR REPLACE INTO sponsors (user_id, guild_id, tier_name) VALUES (?, ?, ?)", (self.target_user.id, interaction.guild.id, self.values[0]))
        await bot.db.commit()
        await interaction.response.send_message(f"✅ **{self.target_user.name}** is now a **{self.values[0]}** sponsor!", ephemeral=True)

class SponsorUserSelect(ui.UserSelect):
    def __init__(self, mode="add"):
        self.mode = mode
        super().__init__(placeholder="Select a user...")
    async def callback(self, interaction: discord.Interaction):
        user = self.values[0]
        if self.mode == "add":
            view = ui.View()
            view.add_item(SponsorTierSelect(user))
            await interaction.response.send_message(f"Select tier for **{user.name}**:", view=view, ephemeral=True)
        else:
            await bot.db.execute("DELETE FROM sponsors WHERE user_id=? AND guild_id=?", (user.id, interaction.guild.id))
            await bot.db.commit()
            await interaction.response.send_message(f"🗑️ Removed **{user.name}**.", ephemeral=True)

class SponsorSettingsView(ui.View):
    @ui.button(label="Add Sponsor", style=discord.ButtonStyle.green, emoji="➕")
    async def add_sponsor_btn(self, i, b):
        view = ui.View()
        view.add_item(SponsorUserSelect(mode="add"))
        await i.response.send_message("Select user to **ADD**:", view=view, ephemeral=True)
    @ui.button(label="Remove Sponsor", style=discord.ButtonStyle.red, emoji="➖")
    async def remove_sponsor_btn(self, i, b):
        view = ui.View()
        view.add_item(SponsorUserSelect(mode="remove"))
        await i.response.send_message("Select user to **REMOVE**:", view=view, ephemeral=True)


class SystemSettingsView(ui.View):
    @ui.button(label="Set Status Channel", style=discord.ButtonStyle.primary, emoji="📡")
    async def set_status(self, interaction: discord.Interaction, button: ui.Button):
        view = ui.View()
        view.add_item(StatusChannelSelect())
        await interaction.response.send_message("Select the channel for startup, restart, and health updates:", view=view, ephemeral=True)

    @ui.button(label="Set Audit Channel", style=discord.ButtonStyle.secondary, emoji="🔒")
    async def set_audit(self, interaction: discord.Interaction, button: ui.Button):
        view = ui.View()
        view.add_item(AuditChannelSelect())
        await interaction.response.send_message("Select the audit log channel:", view=view, ephemeral=True)

    @ui.button(label="Level 100 Salary", style=discord.ButtonStyle.success, emoji="💸")
    async def level_salary(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(Level100SalaryModal())

    @ui.button(label="Global Event", style=discord.ButtonStyle.success, emoji="🌍")
    async def global_event(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(GlobalEventModal())

    @ui.button(label="Run Health Check", style=discord.ButtonStyle.danger, emoji="🩺")
    async def run_health(self, interaction: discord.Interaction, button: ui.Button):
        embed = await build_health_check_embed(interaction.guild)
        await interaction.response.send_message(embed=embed, ephemeral=True)

class ConfigDashboard(ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(ConfigSelect())

class ConfigSelect(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Manage Roles", description="Multipliers, Salaries", emoji="🛡️", value="roles"),
            discord.SelectOption(label="Manage Channels", description="Boosts, Routing", emoji="📢", value="channels"),
            discord.SelectOption(label="System Settings", description="Status, audit, salary, health", emoji="⚙️", value="system"),
            discord.SelectOption(label="Overview", description="Quick configuration summary", emoji="🧭", value="overview"),
            discord.SelectOption(label="Sponsors", description="Add/Remove Sponsors", emoji="💎", value="general"),
            discord.SelectOption(label="View Role Stats", description="List levels, multipliers, and salaries", emoji="📊", value="view_role_stats")
        ]
        super().__init__(placeholder="Config Category...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        val = self.values[0]
        
        if val == "roles":
            view = ui.View()
            role_select = ui.RoleSelect(placeholder="Pick a role...")
            async def role_callback(inter):
                await inter.response.send_message(f"⚙️ **{role_select.values[0].name}**:", view=RoleActionView(role_select.values[0]), ephemeral=True)
            role_select.callback = role_callback
            view.add_item(role_select)
            await interaction.response.send_message(embed=discord.Embed(title="🛡️ Roles", color=discord.Color.blue()), view=view, ephemeral=True)
            
        elif val == "channels":
            view = ui.View()
            chan_select = ui.ChannelSelect(channel_types=[discord.ChannelType.text, discord.ChannelType.voice], placeholder="Pick a channel...")
            async def chan_callback(inter):
                await inter.response.send_message(f"⚙️ **{chan_select.values[0].name}**:", view=ChannelActionView(chan_select.values[0]), ephemeral=True)
            chan_select.callback = chan_callback
            view.add_item(chan_select)
            await interaction.response.send_message(embed=discord.Embed(title="📢 Channels", color=discord.Color.green()), view=view, ephemeral=True)

        elif val == "system":
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="⚙️ System Settings",
                    description="Manage lifecycle updates, health checks, admin logs, and server-wide salary/event settings.",
                    color=discord.Color.blurple(),
                ),
                view=SystemSettingsView(),
                ephemeral=True,
            )

        elif val == "overview":
            embed = await build_config_overview_embed(interaction.guild)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        elif val == "general":
            await interaction.response.send_message(embed=discord.Embed(title="💎 Sponsors", color=discord.Color.gold()), view=SponsorSettingsView(), ephemeral=True)
            
        elif val == "view_role_stats":
            async with bot.db.execute("SELECT role_id, level FROM level_roles WHERE guild_id = ?", (interaction.guild.id,)) as c:
                level_data = {row[0]: row[1] for row in await c.fetchall()}
            
            async with bot.db.execute("SELECT role_id, multiplier FROM role_multipliers WHERE guild_id = ?", (interaction.guild.id,)) as c:
                mult_data = {row[0]: row[1] for row in await c.fetchall()}

            async with bot.db.execute("SELECT role_id, amount FROM presence_roles WHERE guild_id = ?", (interaction.guild.id,)) as c:
                sal_data = {row[0]: row[1] for row in await c.fetchall()}

            all_role_ids = set(list(level_data.keys()) + list(mult_data.keys()) + list(sal_data.keys()))

            embed = discord.Embed(title="📊 Server Role Stats", description="Here are the active perks for each role:", color=discord.Color.teal())
            
            if not all_role_ids:
                embed.description = "❌ No roles are currently configured in the database."
            else:
                lines = []
                for r_id in all_role_ids:
                    role = interaction.guild.get_role(r_id)
                    role_str = role.mention if role else f"`Deleted Role ({r_id})`"
                    
                    stats = []
                    if r_id in level_data:
                        stats.append(f"**Lvl:** {level_data[r_id]}")
                    if r_id in mult_data:
                        stats.append(f"**Mult:** {mult_data[r_id]}x")
                    if r_id in sal_data:
                        stats.append(f"**Salary:** {sal_data[r_id]} XP/hr")
                    
                    lines.append(f"{role_str} ➜ " + " | ".join(stats))
                
                embed.description = "\n".join(lines)
            
            await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="config", description="Open the Server Configuration Dashboard")
@app_commands.checks.has_permissions(administrator=True)
async def config(interaction: discord.Interaction):
    embed = discord.Embed(title="🎛️ Labworks Control Panel", description="Use the dropdown menu below to configure your server's XP system.", color=discord.Color.gold())
    embed.add_field(name="🛡️ Roles", value="Set Multipliers, Salaries, Voice XP, & Level Rewards.", inline=True)
    embed.add_field(name="📢 Channels", value="Set Channel Boosts & Message Routing.", inline=True)
    embed.add_field(name="⚙️ System", value="Set status, audit, health, global XP, and level-100 salary.", inline=True)
    embed.add_field(name="🧭 Overview", value="Review current server configuration at a glance.", inline=True)
    embed.add_field(name="💎 Sponsors", value="Add/Remove Sponsors.", inline=True)
    await interaction.response.send_message(embed=embed, view=ConfigDashboard(), ephemeral=True)


@bot.tree.command(name="healthcheck", description="Run a startup-style health check for this server")
@app_commands.checks.has_permissions(administrator=True)
async def healthcheck(interaction: discord.Interaction):
    embed = await build_health_check_embed(interaction.guild)
    await interaction.response.send_message(embed=embed, ephemeral=True)


def generate_minecraft_link_code():
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(6))


@bot.tree.command(name="linkminecraft", description="Generate a code to link your Minecraft account")
async def linkminecraft(interaction: discord.Interaction):
    now = time.time()
    await bot.db.execute("DELETE FROM minecraft_link_codes WHERE expires_at < ?", (now,))

    code = generate_minecraft_link_code()
    while True:
        async with bot.db.execute("SELECT 1 FROM minecraft_link_codes WHERE code = ?", (code,)) as cursor:
            if not await cursor.fetchone():
                break
        code = generate_minecraft_link_code()

    await bot.db.execute(
        """
        INSERT OR REPLACE INTO minecraft_link_codes (
            code,
            discord_id,
            guild_id,
            expires_at,
            created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            code,
            interaction.user.id,
            interaction.guild.id,
            now + MINECRAFT_LINK_CODE_TTL_SECONDS,
            now,
        ),
    )
    await bot.db.commit()

    minutes = max(1, MINECRAFT_LINK_CODE_TTL_SECONDS // 60)
    await interaction.response.send_message(
        f"Use `/linkdiscord {code}` in Minecraft within **{minutes} minutes** to link your account.",
        ephemeral=True,
    )


@bot.tree.command(name="minecraftprofile", description="View your linked Minecraft account and Minecraft XP status")
async def minecraftprofile(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    async with bot.db.execute(
        """
        SELECT minecraft_uuid, minecraft_name, linked_at
        FROM minecraft_links
        WHERE discord_id = ?
        """,
        (target.id,),
    ) as cursor:
        link = await cursor.fetchone()

    if not link:
        await interaction.response.send_message(f"{target.mention} has not linked a Minecraft account yet.", ephemeral=True)
        return

    settings = await bot.fetch_guild_settings(interaction.guild.id)
    daily_cap = int(settings.get("minecraft_daily_xp_cap") or MINECRAFT_DAILY_XP_CAP) if settings else MINECRAFT_DAILY_XP_CAP
    daily_xp = await bot.get_minecraft_daily_xp(target.id)

    embed = discord.Embed(title=f"Minecraft Profile: {target.display_name}", color=discord.Color.green())
    embed.add_field(name="Minecraft Name", value=link[1] or "Unknown", inline=True)
    embed.add_field(name="UUID", value=f"`{link[0]}`", inline=False)
    embed.add_field(name="Today's Minecraft XP", value=f"**{daily_xp:,} / {daily_cap:,} XP**", inline=True)
    embed.add_field(name="Linked At", value=link[2] or "Unknown", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="unlinkminecraft", description="Unlink your Minecraft account")
async def unlinkminecraft(interaction: discord.Interaction):
    async with bot.db.execute("SELECT minecraft_name FROM minecraft_links WHERE discord_id = ?", (interaction.user.id,)) as cursor:
        link = await cursor.fetchone()
    if not link:
        await interaction.response.send_message("You do not have a linked Minecraft account.", ephemeral=True)
        return

    await bot.db.execute("DELETE FROM minecraft_links WHERE discord_id = ?", (interaction.user.id,))
    await bot.db.commit()
    await interaction.response.send_message(f"Unlinked Minecraft account **{link[0] or 'Unknown'}**.", ephemeral=True)


@bot.tree.command(name="minecraftxpcap", description="Set the daily Minecraft XP cap for this server")
@app_commands.checks.has_permissions(administrator=True)
async def minecraftxpcap(interaction: discord.Interaction, amount: app_commands.Range[int, 0, 100000]):
    await bot.db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (interaction.guild.id,))
    await bot.db.execute(
        "UPDATE guild_settings SET minecraft_daily_xp_cap = ? WHERE guild_id = ?",
        (int(amount), interaction.guild.id),
    )
    await bot.db.commit()
    await interaction.response.send_message(f"Minecraft daily XP cap set to **{amount:,} XP**.", ephemeral=True)


@bot.tree.command(name="minecraftannounce", description="Configure Minecraft XP gain announcements")
@app_commands.checks.has_permissions(administrator=True)
async def minecraftannounce(interaction: discord.Interaction, enabled: bool, channel: discord.TextChannel = None):
    await bot.db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (interaction.guild.id,))
    await bot.db.execute(
        """
        UPDATE guild_settings
        SET minecraft_announce_enabled = ?,
            minecraft_announce_channel_id = ?
        WHERE guild_id = ?
        """,
        (1 if enabled else 0, channel.id if channel else 0, interaction.guild.id),
    )
    await bot.db.commit()
    destination = channel.mention if channel else "the default bot announcement channel"
    state = "enabled" if enabled else "disabled"
    await interaction.response.send_message(f"Minecraft XP announcements are now **{state}** in {destination}.", ephemeral=True)

# =========================================
# 👑 SPONSORS & PROFILES
# =========================================

def get_rank_background_path(guild_id: int, user_id: int):
    guild_dir = RANK_CARD_DIR / str(guild_id)
    guild_dir.mkdir(parents=True, exist_ok=True)
    return guild_dir / f"{user_id}.png"


def load_rank_font(size: int, bold: bool = False):
    font_candidates = []
    custom_font_path = os.getenv("LEVELBOT_FONT_BOLD" if bold else "LEVELBOT_FONT_REGULAR")
    if custom_font_path:
        font_candidates.append(Path(custom_font_path))

    if bold:
        font_candidates.extend([
            FONT_DIR / "rank_bold.ttf",
            FONT_DIR / "rank_semibold.ttf",
            FONT_DIR / "rank.ttf",
        ])
    else:
        font_candidates.extend([
            FONT_DIR / "rank_regular.ttf",
            FONT_DIR / "rank.ttf",
        ])

    windows_font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    if bold:
        font_candidates.extend([
            windows_font_dir / "seguisb.ttf",
            windows_font_dir / "segoeuib.ttf",
            windows_font_dir / "arialbd.ttf",
        ])
    else:
        font_candidates.extend([
            windows_font_dir / "segoeui.ttf",
            windows_font_dir / "arial.ttf",
        ])

    for candidate in font_candidates:
        try:
            if isinstance(candidate, Path) and candidate.exists():
                return ImageFont.truetype(str(candidate), size)
        except Exception:
            continue

    for fallback_name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf") if bold else ("DejaVuSans.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(fallback_name, size)
        except Exception:
            continue

    return ImageFont.load_default()


async def fetch_rank_positions(user_id: int, guild_id: int, level: int, xp: int):
    async with bot.db.execute(
        """
        SELECT COUNT(*) + 1
        FROM users
        WHERE guild_id = ?
          AND (level > ? OR (level = ? AND xp > ?))
        """,
        (guild_id, level, level, xp),
    ) as cursor:
        server_rank = (await cursor.fetchone())[0]

    async with bot.db.execute(
        """
        SELECT COUNT(*) + 1
        FROM users
        WHERE (level > ? OR (level = ? AND xp > ?))
        """,
        (level, level, xp),
    ) as cursor:
        global_rank = (await cursor.fetchone())[0]

    return server_rank, global_rank


async def fetch_role_rewards(guild: discord.Guild, current_level: int):
    async with bot.db.execute(
        "SELECT level, role_id FROM level_roles WHERE guild_id = ? ORDER BY level ASC",
        (guild.id,),
    ) as cursor:
        rows = await cursor.fetchall()

    upcoming = []
    for unlock_level, role_id in rows:
        if unlock_level <= current_level:
            continue
        role = guild.get_role(role_id)
        if role:
            upcoming.append((unlock_level, role))

    next_reward = upcoming[0] if upcoming else None
    return next_reward, upcoming[1:3]


def fit_text(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


async def create_rank_card(
    target: discord.Member,
    level: int,
    rebirth: int,
    xp: int,
    xp_needed: int,
    total_xp: int,
    message_count: int,
    voice_minutes: int,
    server_rank: int,
    global_rank: int,
    bio: str,
    boosts_text: str,
    sponsor_tier: str = None,
    next_reward=None,
    upcoming_roles=None,
):
    if not PIL_AVAILABLE:
        return None

    width, height = 1280, 760
    background_path = get_rank_background_path(target.guild.id, target.id)
    if background_path.exists():
        background = Image.open(background_path).convert("RGB")
        background = ImageOps.fit(background, (width, height), method=RESAMPLE_LANCZOS)
    else:
        background = Image.new("RGB", (width, height), color=(28, 34, 44))
        gradient = Image.new("RGB", (width, height), color=(17, 21, 28))
        mask = Image.linear_gradient("L").resize((width, height))
        background = Image.composite(background, gradient, mask)

    background = background.filter(ImageFilter.GaussianBlur(radius=1.8))
    overlay = Image.new("RGBA", (width, height), (7, 10, 16, 170))
    card = Image.alpha_composite(background.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(card)

    for inset, color in (
        (18, (64, 180, 255, 32)),
        (24, (168, 92, 255, 32)),
    ):
        draw.rounded_rectangle((inset, inset, width - inset, height - inset), radius=30, outline=color, width=6)
    draw.rounded_rectangle((28, 28, width - 28, height - 28), radius=28, outline=(150, 196, 255, 130), width=2, fill=(18, 21, 30, 215))
    draw.rounded_rectangle((58, 58, width - 58, height - 58), radius=24, fill=(15, 18, 25, 185))

    hex_outline = (122, 132, 161, 60)
    for x, y in ((760, 120), (820, 90), (880, 124), (940, 96)):
        points = [(x + 22 * math.cos(math.radians(angle)), y + 22 * math.sin(math.radians(angle))) for angle in range(30, 390, 60)]
        draw.polygon(points, outline=hex_outline)

    avatar_bytes = await target.display_avatar.with_size(512).read()
    avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((188, 188), RESAMPLE_LANCZOS)
    avatar_mask = Image.new("L", (188, 188), 0)
    avatar_draw = ImageDraw.Draw(avatar_mask)
    avatar_draw.ellipse((0, 0, 188, 188), fill=255)
    avatar.putalpha(avatar_mask)
    avatar_x, avatar_y = 116, 122
    for pad, color in ((24, (255, 215, 102, 110)), (18, (255, 225, 153, 180)), (11, (17, 22, 31, 255))):
        draw.ellipse((avatar_x - pad, avatar_y - pad, avatar_x + 188 + pad, avatar_y + 188 + pad), fill=color)
    card.paste(avatar, (avatar_x, avatar_y), avatar)

    title_font = load_rank_font(44, bold=True)
    small_font = load_rank_font(18)
    pill_font = load_rank_font(18, bold=True)
    level_label_font = load_rank_font(25, bold=True)
    level_value_font = load_rank_font(60, bold=True)
    card_title_font = load_rank_font(17, bold=True)
    stat_title_font = load_rank_font(22, bold=True)
    stat_value_font = load_rank_font(34, bold=True)
    bottom_title_font = load_rank_font(22, bold=True)
    bottom_body_font = load_rank_font(17)
    progress_font = load_rank_font(24, bold=True)

    percent = min(100, max(0, int((xp / xp_needed) * 100))) if xp_needed else 0
    rebirth_text = to_roman(rebirth)
    next_level_progress = min(1.0, max(0.0, xp / xp_needed)) if xp_needed else 0.0

    def rounded_panel(box, fill=(33, 37, 47, 240), outline=(87, 97, 120, 90), radius=18):
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=2)

    def fit_font_size(text, max_width, base_size, bold=False, min_size=12):
        size = base_size
        while size >= min_size:
            font = load_rank_font(size, bold=bold)
            bbox = draw.textbbox((0, 0), text, font=font)
            if (bbox[2] - bbox[0]) <= max_width:
                return font
            size -= 1
        return load_rank_font(min_size, bold=bold)

    def draw_pill(box, label_prefix, label_value, accent_fill, accent_outline):
        draw.rounded_rectangle(box, radius=14, fill=accent_fill, outline=accent_outline, width=2)
        label_fill = (255, 214, 89) if "PRESTIGE" in label_prefix else (182, 206, 255)
        pill_text = f"{label_prefix} {label_value}"
        pill_max_width = (box[2] - box[0]) - 36
        text_font = fit_font_size(pill_text, pill_max_width, 18, bold=True, min_size=14)
        bbox = draw.textbbox((0, 0), pill_text, font=text_font)
        text_x = box[0] + (((box[2] - box[0]) - (bbox[2] - bbox[0])) / 2)
        text_y = box[1] + (((box[3] - box[1]) - (bbox[3] - bbox[1])) / 2) - 1
        prefix_bbox = draw.textbbox((0, 0), label_prefix, font=text_font)
        draw.text((text_x, text_y), label_prefix, font=text_font, fill=label_fill)
        draw.text((text_x + (prefix_bbox[2] - prefix_bbox[0]) + 10, text_y), label_value, font=text_font, fill=(244, 248, 255))

    def draw_centered_text_in_box(box, text, font, fill, y_offset=0):
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        x = box[0] + ((box[2] - box[0] - text_width) / 2)
        y = box[1] + ((box[3] - box[1] - text_height) / 2) + y_offset
        draw.text((x, y), text, font=font, fill=fill)

    def draw_right_aligned(x, y, text, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        draw.text((x - (bbox[2] - bbox[0]), y), text, font=font, fill=fill)

    draw.rounded_rectangle((430, 28, 850, 64), radius=18, fill=(21, 28, 38, 230), outline=(87, 171, 255, 180), width=2)
    draw.text((450, 37), "DISCORD LEVELLING BOT", font=card_title_font, fill=(198, 216, 255))
    draw.text((690, 37), "| Command: /rank", font=card_title_font, fill=(235, 236, 247))

    draw_right_aligned(1110, 122, "LEVEL", level_label_font, (229, 233, 243))
    draw_right_aligned(1180, 94, str(level), level_value_font, (255, 255, 255))

    prestige_label = rebirth_text if rebirth > 0 else "0"
    draw_pill((330, 210, 640, 264), "PRESTIGE:", prestige_label, (81, 62, 28, 220), (230, 191, 92, 180))
    draw_pill((760, 210, 1120, 264), "SERVER RANK:", f"#{server_rank}", (48, 62, 97, 220), (112, 154, 244, 180))

    rounded_panel((92, 318, 1188, 412), fill=(28, 32, 43, 240))
    draw.text((120, 335), "LVL", font=small_font, fill=(234, 236, 242))
    draw.text((164, 328), str(level), font=progress_font, fill=(255, 255, 255))
    draw.text((608, 334), "LEVEL", font=small_font, fill=(214, 218, 229))
    draw.text((1110, 335), "LVL", font=small_font, fill=(234, 236, 242))
    draw.text((1153, 328), str(level + 1), font=progress_font, fill=(255, 255, 255))

    bar_left, bar_top, bar_right, bar_bottom = 208, 354, 1088, 388
    draw.rounded_rectangle((bar_left, bar_top, bar_right, bar_bottom), radius=17, fill=(18, 22, 33, 255), outline=(76, 84, 106, 120), width=2)
    progress_width = int((bar_right - bar_left) * (percent / 100))
    if progress_width > 0:
        gradient = Image.new("RGBA", (max(1, progress_width), bar_bottom - bar_top), color=0)
        grad_draw = ImageDraw.Draw(gradient)
        for px in range(max(1, progress_width)):
            ratio = px / max(1, progress_width - 1)
            r = int(42 + (72 - 42) * ratio)
            g = int(82 + (131 - 82) * ratio)
            b = int(255 + (255 - 255) * ratio)
            grad_draw.line((px, 0, px, bar_bottom - bar_top), fill=(r, g, b, 255))
        card.alpha_composite(gradient, dest=(bar_left, bar_top))

    xp_progress_text = f"{xp:,} / {xp_needed:,} XP"
    xp_bbox = draw.textbbox((0, 0), xp_progress_text, font=small_font)
    xp_text_x = ((bar_left + bar_right) - (xp_bbox[2] - xp_bbox[0])) / 2
    draw.text((xp_text_x, 392), xp_progress_text, font=small_font, fill=(150, 160, 189))

    stat_boxes = [
        ((92, 446, 409, 566), "TOTAL XP", f"{total_xp:,}", "XP"),
        ((431, 446, 748, 566), "MESSAGES", f"{message_count:,}", "MS"),
        ((770, 446, 1087, 566), "VOICE TIME", format_voice_time(voice_minutes), "VC"),
    ]
    for index, (box, label, value, icon) in enumerate(stat_boxes, start=1):
        rounded_panel(box)
        draw.text((box[0] + 18, box[1] + 20), icon, font=stat_title_font, fill=(255, 208, 88) if index == 1 else (173, 197, 255))
        draw.text((box[0] + 84, box[1] + 20), f"{label}:", font=stat_title_font, fill=(240, 243, 249))
        value_font = fit_font_size(value, (box[2] - box[0]) - 60, 34, bold=True, min_size=24)
        draw_centered_text_in_box((box[0] + 30, box[1] + 54, box[2] - 30, box[3] - 18), value, value_font, (255, 255, 255), y_offset=6)
        draw_right_aligned(box[2] - 16, box[1] + 14, str(index), small_font, (125, 133, 154))

    rounded_panel((92, 588, 610, 704))
    rounded_panel((632, 588, 1188, 704))
    draw.text((116, 607), "NEXT REWARD:", font=bottom_title_font, fill=(245, 247, 255))
    draw.text((656, 607), "UPCOMING ROLES:", font=bottom_title_font, fill=(245, 247, 255))

    if next_reward:
        reward_level, reward_role = next_reward
        reward_text = fit_text(f"Level {reward_level} - {reward_role.name}", 28)
        xp_remaining = max(0, total_xp_for_state(reward_level, 0) - total_xp)
        ring_box = (130, 632, 200, 702)
        draw.ellipse(ring_box, outline=(84, 92, 112, 255), width=8, fill=(21, 25, 35, 180))
        draw.arc(ring_box, start=-90, end=-90 + int(360 * next_level_progress), fill=(96, 143, 255, 255), width=8)
        inner_box = (144, 646, 186, 688)
        draw.ellipse(inner_box, fill=(24, 28, 38, 255))
        pct_text = f"{int(next_level_progress * 100)}%"
        pct_font = fit_font_size(pct_text, 34, 16, bold=True, min_size=11)
        pct_bbox = draw.textbbox((0, 0), pct_text, font=pct_font)
        draw.text((165 - ((pct_bbox[2] - pct_bbox[0]) / 2), 667 - ((pct_bbox[3] - pct_bbox[1]) / 2)), pct_text, font=pct_font, fill=(220, 229, 252))
        draw.text((202, 640), reward_text, font=bottom_body_font, fill=(245, 247, 255))
        draw.text((202, 668), f"Needs {xp_remaining:,} more XP", font=bottom_body_font, fill=(189, 195, 212))
    else:
        ring_box = (130, 632, 200, 702)
        draw.ellipse(ring_box, outline=(82, 164, 123, 255), width=8, fill=(21, 25, 35, 180))
        inner_box = (144, 646, 186, 688)
        draw.ellipse(inner_box, fill=(24, 28, 38, 255))
        done_font = fit_font_size("DONE", 36, 13, bold=True, min_size=10)
        done_bbox = draw.textbbox((0, 0), "DONE", font=done_font)
        draw.text((165 - ((done_bbox[2] - done_bbox[0]) / 2), 667 - ((done_bbox[3] - done_bbox[1]) / 2)), "DONE", font=done_font, fill=(117, 220, 164))
        draw.text((202, 648), "All configured rewards unlocked", font=bottom_body_font, fill=(245, 247, 255))

    upcoming_roles = upcoming_roles or []
    if upcoming_roles:
        for idx, (unlock_level, role) in enumerate(upcoming_roles):
            row_top = 628 + (idx * 48)
            icon_fill = (255, 214, 89) if idx == 0 else (205, 170, 255)
            bullet_x = 672
            bullet_y = row_top + 12
            draw.ellipse((bullet_x - 7, bullet_y - 7, bullet_x + 7, bullet_y + 7), fill=icon_fill)
            role_name = fit_text(role.name, 24)
            role_font = fit_font_size(role_name, 410, 17, bold=False, min_size=14)
            role_bbox = draw.textbbox((0, 0), role_name, font=role_font)
            detail_font = load_rank_font(15, bold=False)
            xp_remaining = max(0, total_xp_for_state(unlock_level, 0) - total_xp)
            detail_text = f"Needs {xp_remaining:,} XP"
            draw.text((694, row_top - 2), role_name, font=role_font, fill=(245, 247, 255))
            draw.text((694, row_top + (role_bbox[3] - role_bbox[1]) + 7), detail_text, font=detail_font, fill=(185, 191, 208))
    else:
        draw.text((658, 650), "No more configured roles", font=bottom_body_font, fill=(245, 247, 255))

    output = io.BytesIO()
    card.convert("RGB").save(output, format="PNG", optimize=True)
    output.seek(0)
    return discord.File(output, filename=f"rank-{target.id}.png")

@bot.tree.command(name="sponsors", description="View the legendary supporters of Labworks")
async def sponsors(interaction: discord.Interaction):
    async with bot.db.execute("SELECT user_id, tier_name FROM sponsors WHERE guild_id = ?", (interaction.guild.id,)) as cursor:
        rows = await cursor.fetchall()
    embed = discord.Embed(title="🛡️ Labworks Studio Sponsors", description="The incredible individuals helping us build the future of gaming.", color=discord.Color.gold())
    if not rows: embed.add_field(name="Current Sponsors", value="No sponsors yet! Be the first to support us.")
    else:
        tiers = {"Studio Partner": [], "Alpha Tester": [], "Intern": []}
        for uid, tier in rows: 
            if tier in tiers: tiers[tier].append(f"<@{uid}>")
        for t, m in tiers.items():
            if m: embed.add_field(name=f"✨ {t}", value="\n".join(m), inline=False)
    embed.add_field(
        name="Sponsor Benefits",
        value=(
            "`$2/mo` Sponsor role, x1.1 XP, credits, and Sponsor Lounge access.\n"
            "`$5/mo` Adds a passive XP salary and voting power on community choices.\n"
            "`$10/mo` Adds rank-card flair, rebirth perks, and stronger boost gifting."
        ),
        inline=False,
    )
    embed.set_footer(text=f"Support Labworks at {GITHUB_SPONSORS_URL}")
    await interaction.response.send_message(embed=embed)

class ProfileGroup(app_commands.Group):
    def __init__(self): super().__init__(name="profile", description="Customize your profile")
    @app_commands.command(name="bio", description="Set your rank card bio (Level 20+)")
    async def bio(self, i, text: str):
        async with bot.db.execute("SELECT level FROM users WHERE user_id=? AND guild_id=?", (i.user.id, i.guild.id)) as c:
            d = await c.fetchone()
        if not d or d[0] < 20: return await i.response.send_message("❌ You must be **Level 20** to set a bio.", ephemeral=True)
        if len(text) > 100: return await i.response.send_message("❌ Bio too long.", ephemeral=True)
        await bot.db.execute("UPDATE users SET bio = ? WHERE user_id=? AND guild_id=?", (text, i.user.id, i.guild.id))
        await bot.db.commit()
        await i.response.send_message("✅ Bio updated!", ephemeral=True)
    @app_commands.command(name="levelup_msg", description="Set custom level up message (Level 20+)")
    async def levelup_msg(self, i, message: str):
        async with bot.db.execute("SELECT level FROM users WHERE user_id=? AND guild_id=?", (i.user.id, i.guild.id)) as c:
            d = await c.fetchone()
        if not d or d[0] < 20: return await i.response.send_message("❌ You must be **Level 20** to set a custom message.", ephemeral=True)
        if "{user}" not in message and "{level}" not in message: return await i.response.send_message("❌ Message must contain `{user}` or `{level}`.", ephemeral=True)
        await bot.db.execute("UPDATE users SET custom_msg = ? WHERE user_id=? AND guild_id=?", (message, i.user.id, i.guild.id))
        await bot.db.commit()
        await i.response.send_message("✅ Message updated!", ephemeral=True)
    @app_commands.command(name="birthday", description="Set your birthday DD-MM (Level 50+)")
    async def birthday(self, i, day: int, month: int):
        async with bot.db.execute("SELECT level FROM users WHERE user_id=? AND guild_id=?", (i.user.id, i.guild.id)) as c:
            d = await c.fetchone()
        if not d or d[0] < 50: return await i.response.send_message("❌ You must be **Level 50** to set your birthday.", ephemeral=True)
        try:
            datetime.date(2024, month, day)
            bdate = f"{day:02d}-{month:02d}"
            await bot.db.execute("UPDATE users SET birthday = ? WHERE user_id=? AND guild_id=?", (bdate, i.user.id, i.guild.id))
            await bot.db.commit()
            await i.response.send_message(f"✅ Birthday set to **{bdate}**!", ephemeral=True)
        except: await i.response.send_message("❌ Invalid Date.", ephemeral=True)
    @app_commands.command(name="card_background", description="Upload a custom rank-card background image")
    async def card_background(self, i: discord.Interaction, image: discord.Attachment):
        if not PIL_AVAILABLE:
            return await i.response.send_message("❌ Rank cards need Pillow installed first.", ephemeral=True)
        if not image.content_type or not image.content_type.startswith("image/"):
            return await i.response.send_message("❌ Please upload an image file.", ephemeral=True)
        if image.size > 8 * 1024 * 1024:
            return await i.response.send_message("❌ Keep the image under 8MB so rank cards stay fast.", ephemeral=True)

        raw_bytes = await image.read()
        try:
            preview = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        except Exception:
            return await i.response.send_message("❌ I couldn't read that image. Try PNG or JPG.", ephemeral=True)

        output_path = get_rank_background_path(i.guild.id, i.user.id)
        preview = ImageOps.fit(preview, (1280, 760), method=RESAMPLE_LANCZOS)
        preview.save(output_path, format="PNG", optimize=True)
        await i.response.send_message("✅ Custom rank-card background saved. Use `/rank` to preview it.", ephemeral=True)
    @app_commands.command(name="clear_card_background", description="Remove your custom rank-card background")
    async def clear_card_background(self, i: discord.Interaction):
        output_path = get_rank_background_path(i.guild.id, i.user.id)
        if output_path.exists():
            output_path.unlink()
            await i.response.send_message("✅ Your custom rank-card background has been removed.", ephemeral=True)
        else:
            await i.response.send_message("ℹ️ You don't have a custom rank-card background saved.", ephemeral=True)

bot.tree.add_command(ProfileGroup())

@bot.tree.command(name="boost_user", description="Level 150+: Give a 2x XP boost to a friend (1hr)")
async def boost_user(interaction: discord.Interaction, target: discord.Member):
    if interaction.user.id == target.id:
        return await interaction.response.send_message("❌ You cannot boost yourself! Spread the love to a friend.", ephemeral=True)

    await bot.ensure_user_record(interaction.user)
    await bot.ensure_user_record(target)
    async with bot.db.execute("SELECT level, last_gift_used FROM users WHERE user_id=? AND guild_id=?", (interaction.user.id, interaction.guild.id)) as c:
        d = await c.fetchone()
    
    if not d or d[0] < 150: 
        return await interaction.response.send_message("❌ You must be **Level 150** to use this.", ephemeral=True)

    last_used = d[1]
    now = time.time()
    cooldown = 86400 # 24 hours

    if now - last_used < cooldown:
        remaining = cooldown - (now - last_used)
        hours = int(remaining // 3600)
        minutes = int((remaining % 3600) // 60)
        return await interaction.response.send_message(f"❌ **Cooldown Active:** You can gift again in **{hours}h {minutes}m**.", ephemeral=True)

    end_time = now + 3600 # 1 hour
    await bot.db.execute("INSERT OR REPLACE INTO active_boosts (user_id, guild_id, end_time, multiplier) VALUES (?, ?, ?, ?)", (target.id, interaction.guild.id, end_time, 2.0))
    await bot.db.execute("UPDATE users SET last_gift_used = ? WHERE user_id=? AND guild_id=?", (now, interaction.user.id, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(f"🎁 **GIFT SENT!** {target.mention} now has a **2x XP Boost** for 1 hour!")
    
@bot.tree.command(name="rank", description="Check your stats or another user's")
async def rank(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    
    async with bot.db.execute("SELECT xp, level, rebirth, bio, message_count, voice_minutes FROM users WHERE user_id=? AND guild_id=?", (target.id, interaction.guild.id)) as c:
        data = await c.fetchone()
    async with bot.db.execute("SELECT tier_name FROM sponsors WHERE user_id=? AND guild_id=?", (target.id, interaction.guild.id)) as c:
        s_data = await c.fetchone()
    
    xp, level, rebirth, bio, message_count, voice_minutes = data if data else (0, 1, 0, "No bio set.", 0, 0)
    
    xp_needed = xp_needed_for_level(level)
    total_xp = total_xp_for_state(level, xp)
    percent = min(100, max(0, (xp / xp_needed) * 100))
    bar = "🟦" * int(percent / 10) + "⬜" * (10 - int(percent / 10))
    
    boosts_text = ""
    
    settings = await bot.fetch_guild_settings(interaction.guild.id)
    global_mult = settings["global_xp_mult"] if settings else 1.0
    if global_mult > 1.0:
        boosts_text += f"🌍 **Global Event**: x{global_mult}\n"

    quiet_mult = 1.0
    if settings and settings["quiet_event_until"] and settings["quiet_event_until"] > time.time():
        quiet_mult = settings["quiet_event_multiplier"]
        boosts_text += f"🌙 **Quiet Hours**: x{quiet_mult}\n"

    channel_mult = 1.0
    async with bot.db.execute("SELECT multiplier FROM channel_multipliers WHERE channel_id=?", (interaction.channel.id,)) as c:
        cm_data = await c.fetchone()
        if cm_data and cm_data[0] > 1.0:
            channel_mult = cm_data[0]
            boosts_text += f"⚡ **Channel Boost**: x{channel_mult}\n"

    role_mult = 1.0
    async with bot.db.execute("SELECT role_id, multiplier FROM role_multipliers WHERE guild_id=?", (interaction.guild.id,)) as c:
        db_roles = {row[0]: row[1] for row in await c.fetchall()}
    for role in target.roles:
        if role.id in db_roles:
            bonus = db_roles[role.id] - 1.0
            role_mult += bonus
            boosts_text += f"🛡️ **{role.name}**: x{db_roles[role.id]}\n"
    
    rebirth_mult = 1.0 + (rebirth * 0.2)
    if rebirth > 0:
        boosts_text += f"🔄 **Rebirth {to_roman(rebirth)}**: x{round(rebirth_mult, 1)}\n"

    temp_mult = 1.0
    async with bot.db.execute("SELECT end_time, multiplier FROM active_boosts WHERE user_id=? AND guild_id=?", (target.id, interaction.guild.id)) as c:
        temp = await c.fetchone()
    if temp and temp[0] > time.time():
        temp_mult = temp[1]
        boosts_text += f"🎁 **Friend Gift**: x{temp_mult}\n"

    grand_total = global_mult * quiet_mult * channel_mult * role_mult * rebirth_mult * temp_mult

    sponsor_tier = s_data[0] if s_data else None
    server_rank, global_rank = await fetch_rank_positions(target.id, interaction.guild.id, level, xp)
    next_reward, upcoming_roles = await fetch_role_rewards(interaction.guild, level)
    rank_card = await create_rank_card(
        target=target,
        level=level,
        rebirth=rebirth,
        xp=xp,
        xp_needed=xp_needed,
        total_xp=total_xp,
        message_count=message_count,
        voice_minutes=voice_minutes,
        server_rank=server_rank,
        global_rank=global_rank,
        bio=bio,
        boosts_text=boosts_text,
        sponsor_tier=sponsor_tier,
        next_reward=next_reward,
        upcoming_roles=upcoming_roles,
    )

    if rank_card:
        if boosts_text:
            embed = discord.Embed(color=target.color)
            embed.add_field(name="🚀 Active Boosts", value=boosts_text, inline=False)
            await interaction.response.send_message(embed=embed, file=rank_card)
        else:
            await interaction.response.send_message(file=rank_card)
        return

    embed = discord.Embed(title=f"🛡️ {target.display_name}", description=f"*{bio}*", color=target.color)
    if target.display_avatar:
        embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Level", value=str(level), inline=True)
    embed.add_field(name="Rebirth", value=f"**{to_roman(rebirth)}**", inline=True)
    embed.add_field(name="Total Multiplier", value=f"**x{round(grand_total, 2)}**", inline=True)
    embed.add_field(name="Progress", value=f"`{bar}` **{int(percent)}%**\n`{xp} / {xp_needed} XP`", inline=False)
    if boosts_text:
        embed.add_field(name="🚀 Active Boosts", value=boosts_text, inline=False)
    embed.set_footer(text="Install Pillow to enable image rank cards.")
    embed = await maybe_apply_sponsor_promo(embed, interaction.user.id, interaction.guild.id, chance=0.16)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="rebirth", description="Reset to Level 1 for a permanent boost (Level 200+)")
async def rebirth(interaction: discord.Interaction):
    async with bot.db.execute("SELECT level, rebirth FROM users WHERE user_id=? AND guild_id=?", (interaction.user.id, interaction.guild.id)) as c:
        data = await c.fetchone()
    if not data or data[0] < 200: return await interaction.response.send_message("❌ Need Level 200.", ephemeral=True)
    await bot.db.execute("UPDATE users SET level=1, xp=0, rebirth=? WHERE user_id=? AND guild_id=?", (data[1] + 1, interaction.user.id, interaction.guild.id))
    await bot.db.commit()
    await bot.sync_level_roles_for_member(interaction.user, 1)
    await interaction.response.send_message(f"🚨 **REBIRTH!** {interaction.user.mention} is now Rebirth **{to_roman(data[1] + 1)}**!")

# =========================================
# 🛠️ UTILS & DEV
# =========================================

@bot.command()
@commands.is_owner()
async def clearglobals(ctx):
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync()
    await ctx.send("✅ Globals wiped.")

@bot.tree.error
async def on_app_command_error(i: discord.Interaction, e: app_commands.AppCommandError):
    if isinstance(e, app_commands.MissingPermissions):
        if i.response.is_done():
            await i.followup.send("🚫 Admin Only.", ephemeral=True)
        else:
            await i.response.send_message("🚫 Admin Only.", ephemeral=True)
    else: print(f"Error: {e}")

@bot.command()
@commands.is_owner()
async def sync(ctx):
    bot.tree.copy_global_to(guild=ctx.guild)
    synced = await bot.tree.sync(guild=ctx.guild)
    await ctx.send(f"✅ **Synced {len(synced)} commands to {ctx.guild.name}!**")

@bot.tree.command(name="sync_roles", description="Fix missing roles for a user or the whole server (Admin Only)")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(target="Leave empty to sync EVERYONE (might take a while)")
async def sync_roles(interaction: discord.Interaction, target: discord.Member = None):
    await interaction.response.defer(ephemeral=True)
    
    async with bot.db.execute("SELECT level, role_id FROM level_roles WHERE guild_id = ? ORDER BY level DESC", (interaction.guild.id,)) as c:
        role_data = await c.fetchall()
    
    if not role_data:
        return await interaction.followup.send("❌ No level roles are configured yet!")

    all_role_ids = {r[1] for r in role_data}

    async def sync_user(member):
        async with bot.db.execute("SELECT level FROM users WHERE user_id=? AND guild_id=?", (member.id, interaction.guild.id)) as c:
            d = await c.fetchone()
        if not d:
            return False
        return await bot.sync_level_roles_for_member(member, d[0])

    if target:
        modified = await sync_user(target)
        if modified:
            await interaction.followup.send(f"✅ **Synced:** {target.mention} roles have been updated.")
        else:
            await interaction.followup.send(f"👍 **Up to Date:** {target.mention} already has the correct roles.")
    else:
        await interaction.followup.send("🔄 **Syncing Server...** This may take a moment.")
        count = 0
        for member in interaction.guild.members:
            if member.bot: continue
            if await sync_user(member):
                count += 1
                await discord.utils.sleep_until(discord.utils.utcnow() + datetime.timedelta(seconds=0.5))
        
        await interaction.followup.send(f"✅ **Complete:** Updated roles for **{count}** users.")

@bot.tree.command(name="debug_rank", description="Analyze exactly why a user isn't getting a role")
@app_commands.checks.has_permissions(administrator=True)
async def debug_rank(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.defer(ephemeral=True)

    async with bot.db.execute("SELECT level, xp FROM users WHERE user_id=? AND guild_id=?", (target.id, interaction.guild.id)) as c:
        u_data = await c.fetchone()
    
    if not u_data:
        return await interaction.followup.send("❌ User not found in database.")
    
    u_level = u_data[0]
    
    async with bot.db.execute("SELECT level, role_id FROM level_roles WHERE guild_id=? ORDER BY level DESC", (interaction.guild.id,)) as c:
        roles = await c.fetchall()

    log = [f"🔍 **Analysis for {target.mention}**"]
    log.append(f"• **DB Level:** `{u_level}` (Type: {type(u_level).__name__})")
    log.append(f"• **User Roles:** {[r.name for r in target.roles]}")
    log.append("--- **Logic Trace** ---")

    best_match = None

    for lvl, r_id in roles:
        role_obj = interaction.guild.get_role(r_id)
        role_name = role_obj.name if role_obj else "⚠️ DELETED ROLE"
        
        is_high_enough = u_level >= lvl
        marker = "✅" if is_high_enough else "❌"
        
        log.append(f"{marker} **Lvl {lvl}** ({role_name}) -> User is {u_level}")
        
        if is_high_enough and best_match is None:
            best_match = r_id
            log.append(f"   🎉 **MATCH FOUND!** Bot selected: {role_name}")
            
            if role_obj:
                bot_member = interaction.guild.get_member(bot.user.id)
                if role_obj.position >= bot_member.top_role.position:
                    log.append(f"   ⛔ **CRITICAL ERROR:** Bot role is BELOW {role_name}. Cannot assign.")
                elif role_obj in target.roles:
                    log.append(f"   ℹ️ User already has this role. No action needed.")
                else:
                    log.append(f"   ✨ User needs this role. 'Up to date' message is WRONG.")
            else:
                log.append(f"   ⚠️ Role ID {r_id} does not exist in Discord.")

    await interaction.followup.send("\n".join(log))

@bot.tree.command(name="debug_user_db", description="View raw database row for a specific user")
@app_commands.checks.has_permissions(administrator=True)
async def debug_user_db(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.defer(ephemeral=True)

    async with bot.db.execute("SELECT * FROM users WHERE user_id = ? AND guild_id = ?", (target.id, interaction.guild.id)) as c:
        row = await c.fetchone()
        columns = [description[0] for description in c.description]

    if not row:
        return await interaction.followup.send(f"❌ **{target.display_name}** was not found in the database at all.")

    data_list = dict(zip(columns, row))
    
    debug_msg = [f"📊 **Raw DB Data for {target.mention}**"]
    for key, value in data_list.items():
        val_type = type(value).__name__
        debug_msg.append(f"• `{key}`: **{value}** (Type: `{val_type}`)")

    current_guild_id = interaction.guild.id
    db_guild_id = data_list.get('guild_id')

    debug_msg.append("\n🔎 **Leaderboard Compatibility Check:**")
    
    if str(db_guild_id) != str(current_guild_id):
        debug_msg.append(f"⚠️ **MISMATCH:** User is registered under Guild ID `{db_guild_id}`, but this server is `{current_guild_id}`. They won't appear on this leaderboard.")
    else:
        debug_msg.append("✅ **Guild ID:** Matches this server.")

    if isinstance(data_list.get('xp'), str):
        debug_msg.append("⚠️ **TYPE ERROR:** XP is stored as a `string`. This causes '9' to be ranked higher than '80'.")
    else:
        debug_msg.append("✅ **Data Type:** XP is a valid number.")

    await interaction.followup.send("\n".join(debug_msg))

discord_token = os.getenv("DISCORD_TOKEN")
if not discord_token:
    raise RuntimeError("DISCORD_TOKEN is missing. Set it in src/.env or pass it as a container environment variable.")

bot.run(discord_token)
