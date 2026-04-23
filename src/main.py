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
from pathlib import Path
from dotenv import load_dotenv

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
    PIL_AVAILABLE = True
    RESAMPLE_LANCZOS = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
except ImportError:
    PIL_AVAILABLE = False
    RESAMPLE_LANCZOS = None

load_dotenv(override=False)

TEST_GUILD_ID = 1041046184552308776
TEST_GUILD = discord.Object(id=TEST_GUILD_ID)
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"
RANK_CARD_DIR = DATA_DIR / "rank_cards"
DEFAULT_QUIET_EVENT_MULTIPLIER = 2.0
DEFAULT_QUIET_EVENT_MIN_SILENCE = 45 * 60
DEFAULT_QUIET_EVENT_DURATION = 20 * 60
DEFAULT_QUIET_EVENT_COOLDOWN = 3 * 60 * 60
VOICE_XP_PER_MINUTE = 10


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
        
    async def setup_hook(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        RANK_CARD_DIR.mkdir(parents=True, exist_ok=True)
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
                quiet_event_until REAL DEFAULT 0,
                quiet_event_multiplier REAL DEFAULT 1.0,
                last_message_at REAL DEFAULT 0,
                last_quiet_event_at REAL DEFAULT 0
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

        self.tree.copy_global_to(guild=TEST_GUILD)
        await self.tree.sync(guild=TEST_GUILD)
        print(f"✅ Bot Online & Synced ({self.db_path})")
        self.previous_clean_shutdown = previous_clean_shutdown
        self.previous_heartbeat = previous_heartbeat

    async def close(self):
        await self.announce_lifecycle("shutdown")
        await self.set_meta("clean_shutdown", "1")
        await self.set_meta("last_shutdown_at", str(time.time()))
        await self.db.commit()
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
                level,
                rebirth,
                next_xp_time,
                bio,
                custom_msg,
                birthday,
                last_gift_used
            ) VALUES (?, ?, 0, 0, 0, 0, 1, 0, 0, 'No bio set.', NULL, NULL, 0)
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
                   quiet_event_until,
                   quiet_event_multiplier,
                   last_message_at,
                   last_quiet_event_at
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
            "quiet_event_until": row[6],
            "quiet_event_multiplier": row[7],
            "last_message_at": row[8],
            "last_quiet_event_at": row[9],
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

    async def announce_lifecycle(self, event_name: str, target_guild: discord.Guild = None):
        guilds = [target_guild] if target_guild else list(self.guilds)
        for guild in guilds:
            if not guild:
                continue
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
            elif event_name == "quiet_event_start":
                embed.title = "🌙 Quiet Hours XP Event"
                embed.description = f"Chat has gone quiet, so XP is temporarily boosted to **x{DEFAULT_QUIET_EVENT_MULTIPLIER}** for a little while."
            elif event_name == "quiet_event_end":
                embed.title = "🌤️ Quiet Hours XP Event Ended"
                embed.description = "The temporary quiet-hours XP boost has ended."
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
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(type=discord.ActivityType.watching, name="levels, boosts, and rank cards"),
    )
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
            f"Status: {status_channel.mention if status_channel else 'Not set'}"
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

# =========================================
# 👑 SPONSORS & PROFILES
# =========================================

def get_rank_background_path(guild_id: int, user_id: int):
    guild_dir = RANK_CARD_DIR / str(guild_id)
    guild_dir.mkdir(parents=True, exist_ok=True)
    return guild_dir / f"{user_id}.png"


def load_rank_font(size: int, bold: bool = False):
    preferred = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(preferred, size)
    except Exception:
        return ImageFont.load_default()


async def create_rank_card(target: discord.Member, level: int, rebirth: int, xp: int, xp_needed: int, bio: str, boosts_text: str, sponsor_tier: str = None):
    if not PIL_AVAILABLE:
        return None

    width, height = 1100, 380
    background_path = get_rank_background_path(target.guild.id, target.id)
    if background_path.exists():
        background = Image.open(background_path).convert("RGB")
        background = ImageOps.fit(background, (width, height), method=RESAMPLE_LANCZOS)
    else:
        background = Image.new("RGB", (width, height), color=(28, 34, 44))
        gradient = Image.new("RGB", (width, height), color=(17, 21, 28))
        mask = Image.linear_gradient("L").resize((width, height))
        background = Image.composite(background, gradient, mask)

    background = background.filter(ImageFilter.GaussianBlur(radius=1.4))
    overlay = Image.new("RGBA", (width, height), (8, 11, 17, 155))
    card = Image.alpha_composite(background.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(card)

    draw.rounded_rectangle((28, 28, width - 28, height - 28), radius=26, outline=(255, 255, 255, 55), width=2, fill=(18, 24, 33, 165))
    draw.rounded_rectangle((44, 44, 308, height - 44), radius=24, fill=(11, 16, 24, 175))

    avatar_bytes = await target.display_avatar.with_size(256).read()
    avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((190, 190), RESAMPLE_LANCZOS)
    avatar_mask = Image.new("L", (190, 190), 0)
    avatar_draw = ImageDraw.Draw(avatar_mask)
    avatar_draw.ellipse((0, 0, 190, 190), fill=255)
    avatar.putalpha(avatar_mask)
    card.paste(avatar, (82, 88), avatar)

    title_font = load_rank_font(40, bold=True)
    body_font = load_rank_font(22)
    stat_font = load_rank_font(26, bold=True)
    tiny_font = load_rank_font(18)

    display_name = target.display_name[:24]
    boost_lines = boosts_text.strip().splitlines()[:4] if boosts_text else []
    percent = min(100, max(0, int((xp / xp_needed) * 100))) if xp_needed else 0
    rebirth_text = to_roman(rebirth)

    draw.text((338, 62), display_name, font=title_font, fill=(243, 248, 255))
    draw.text((338, 114), bio[:95], font=body_font, fill=(201, 214, 228))

    sponsor_label = sponsor_tier or "Community Member"
    draw.text((82, 294), sponsor_label, font=body_font, fill=(255, 221, 137))
    draw.text((82, 320), f"@{target.name}"[:24], font=tiny_font, fill=(190, 202, 219))

    draw.text((338, 168), f"Level {level}", font=stat_font, fill=(255, 255, 255))
    draw.text((496, 168), f"Rebirth {rebirth_text}", font=stat_font, fill=(225, 230, 238))
    draw.text((700, 168), f"{xp:,} / {xp_needed:,} XP", font=stat_font, fill=(225, 230, 238))

    bar_left, bar_top, bar_right, bar_bottom = 338, 224, 1038, 270
    draw.rounded_rectangle((bar_left, bar_top, bar_right, bar_bottom), radius=18, fill=(33, 41, 54, 235))
    progress_width = int((bar_right - bar_left) * (percent / 100))
    if progress_width > 0:
        draw.rounded_rectangle((bar_left, bar_top, bar_left + progress_width, bar_bottom), radius=18, fill=(95, 186, 255, 255))
    draw.text((338, 282), f"Progress: {percent}%", font=body_font, fill=(238, 244, 255))

    if boost_lines:
        draw.text((338, 320), "Active boosts: " + " | ".join(line.replace("**", "") for line in boost_lines), font=tiny_font, fill=(200, 213, 229))

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
    embed.set_footer(text="Want to support Labworks? Visit github.com/sponsors/Zaxoosh")
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
        preview = ImageOps.fit(preview, (1100, 380), method=RESAMPLE_LANCZOS)
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
    
    async with bot.db.execute("SELECT xp, level, rebirth, bio FROM users WHERE user_id=? AND guild_id=?", (target.id, interaction.guild.id)) as c:
        data = await c.fetchone()
    async with bot.db.execute("SELECT tier_name FROM sponsors WHERE user_id=? AND guild_id=?", (target.id, interaction.guild.id)) as c:
        s_data = await c.fetchone()
    
    xp, level, rebirth, bio = data if data else (0, 1, 0, "No bio set.")
    
    xp_needed = 5 * (level ** 2) + (50 * level) + 100
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
    rank_card = await create_rank_card(
        target=target,
        level=level,
        rebirth=rebirth,
        xp=xp,
        xp_needed=xp_needed,
        bio=bio,
        boosts_text=boosts_text,
        sponsor_tier=sponsor_tier,
    )

    if rank_card:
        embed = discord.Embed(
            title=f"🛡️ {target.display_name}",
            description=f"Level **{level}** • Rebirth **{to_roman(rebirth)}** • Total Multiplier **x{round(grand_total, 2)}**",
            color=target.color,
        )
        embed.add_field(name="Progress", value=f"`{bar}` **{int(percent)}%**\n`{xp} / {xp_needed} XP`", inline=False)
        if boosts_text:
            embed.add_field(name="🚀 Active Boosts", value=boosts_text, inline=False)
        embed.set_image(url=f"attachment://{rank_card.filename}")
        if sponsor_tier == "Studio Partner":
            embed.set_footer(text="💎 Global Studio Partner | Legend")
        await interaction.response.send_message(embed=embed, file=rank_card)
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

bot.run(os.getenv('DISCORD_TOKEN'))
