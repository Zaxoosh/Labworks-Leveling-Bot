import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import aiosqlite
import random
import time
import datetime
from dotenv import load_dotenv

load_dotenv()

TEST_GUILD_ID = 1041046184552308776  # Your Server ID
TEST_GUILD = discord.Object(id=TEST_GUILD_ID)

# --- ROMAN NUMERAL HELPER ---
def to_roman(num):
    val = [
        1000, 900, 500, 400,
        100, 90, 50, 40,
        10, 9, 5, 4,
        1
    ]
    syb = [
        "M", "CM", "D", "CD",
        "C", "XC", "L", "XL",
        "X", "IX", "V", "IV",
        "I"
    ]
    roman_num = ''
    i = 0
    while  num > 0:
        for _ in range(num // val[i]):
            roman_num += syb[i]
            num -= val[i]
        i += 1
    return roman_num if roman_num else "0"

class LevelBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.members = True 
        super().__init__(command_prefix='!', intents=intents)
        
    async def setup_hook(self):
        self.db = await aiosqlite.connect("levels.db")
        
        # 1. Users Table
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER,
                guild_id INTEGER,
                xp INTEGER DEFAULT 0,
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
        
        # 2. Config Tables
        await self.db.execute("CREATE TABLE IF NOT EXISTS role_multipliers (role_id INTEGER PRIMARY KEY, guild_id INTEGER, multiplier REAL)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS voice_roles (role_id INTEGER PRIMARY KEY, guild_id INTEGER)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS presence_roles (role_id INTEGER PRIMARY KEY, guild_id INTEGER, amount INTEGER)")
        
        # 3. Settings & Channel Multipliers
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY, 
                level_channel_id INTEGER DEFAULT 0, 
                birthday_channel_id INTEGER DEFAULT 0, 
                level100_salary INTEGER DEFAULT 0
            )
        """)
        await self.db.execute("CREATE TABLE IF NOT EXISTS channel_multipliers (channel_id INTEGER PRIMARY KEY, guild_id INTEGER, multiplier REAL)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS active_boosts (user_id INTEGER, guild_id INTEGER, end_time REAL, multiplier REAL)")

        # 4. Level Roles (Level -> Role ID)
        await self.db.execute("CREATE TABLE IF NOT EXISTS level_roles (level INTEGER, role_id INTEGER, guild_id INTEGER, PRIMARY KEY (level, guild_id))")

        await self.db.commit()
        
        # Background Tasks
        self.loop.create_task(self.voice_xp_loop())
        self.loop.create_task(self.presence_xp_loop())
        self.loop.create_task(self.birthday_loop())

        # Sync Commands
        self.tree.copy_global_to(guild=TEST_GUILD)
        await self.tree.sync(guild=TEST_GUILD)
        print("✅ Bot Online & Synced")

    async def close(self):
        await self.db.close()
        await super().close()
    
    # --- SHARED XP ADDER (Includes Role Swapping Logic) ---
    async def add_xp(self, member, amount):
        async with self.db.execute("SELECT xp, level, rebirth, custom_msg FROM users WHERE user_id = ? AND guild_id = ?", 
                                   (member.id, member.guild.id)) as cursor:
            data = await cursor.fetchone()
            
        if not data:
            await self.db.execute("INSERT INTO users (user_id, guild_id, xp, level, rebirth) VALUES (?, ?, ?, ?, ?)", 
                                 (member.id, member.guild.id, amount, 1, 0))
            await self.db.commit()
            return False, 1, None

        current_xp, current_level, current_rebirth, custom_msg = data
        
        # 1. Multipliers
        rebirth_mult = 1.0 + (current_rebirth * 0.2)
        role_mult = await calculate_multiplier(member)
        
        # 2. Temp Boosts (Gifts)
        temp_mult = 1.0
        now = time.time()
        await self.db.execute("DELETE FROM active_boosts WHERE end_time < ?", (now,)) # Clean old
        await self.db.commit()
        
        async with self.db.execute("SELECT multiplier FROM active_boosts WHERE user_id=? AND guild_id=?", (member.id, member.guild.id)) as c:
            boost_data = await c.fetchone()
            if boost_data: temp_mult = boost_data[0]

        final_xp = int(amount * rebirth_mult * role_mult * temp_mult)
        
        new_xp = current_xp + final_xp
        xp_needed = 5 * (current_level ** 2) + (50 * current_level) + 100
        
        # Level Up Loop
        did_level_up = False
        while new_xp >= xp_needed:
            if current_level >= 200:
                new_xp = xp_needed
                break
            current_level += 1
            new_xp = new_xp - xp_needed
            xp_needed = 5 * (current_level ** 2) + (50 * current_level) + 100
            did_level_up = True

        # --- LEVEL ROLE LOGIC (REPLACE MODE) ---
        if did_level_up:
            # Get all configured level roles for this guild
            async with self.db.execute("SELECT level, role_id FROM level_roles WHERE guild_id = ?", (member.guild.id,)) as c:
                all_level_roles = await c.fetchall()
            
            level_map = {row[0]: row[1] for row in all_level_roles}
            
            # If the NEW level has a role assigned
            if current_level in level_map:
                new_role_id = level_map[current_level]
                new_role = member.guild.get_role(new_role_id)
                
                # Identify roles to remove (Any configured level role that isn't the new one)
                roles_to_remove = []
                all_configured_ids = set(level_map.values())
                
                for role in member.roles:
                    if role.id in all_configured_ids and role.id != new_role_id:
                        roles_to_remove.append(role)
                
                try:
                    if roles_to_remove:
                        await member.remove_roles(*roles_to_remove, reason=f"Level Up to {current_level} (Replace Mode)")
                    if new_role:
                        await member.add_roles(new_role, reason=f"Level Up to {current_level}")
                except discord.Forbidden:
                    print(f"⚠️ Missing Permissions: Could not swap roles for {member.name}")

        await self.db.execute("UPDATE users SET xp = ?, level = ? WHERE user_id = ? AND guild_id = ?", 
                             (new_xp, current_level, member.id, member.guild.id))
        await self.db.commit()
        
        return did_level_up, current_level, custom_msg

    # --- LOOPS ---
    async def voice_xp_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            await discord.utils.sleep_until(discord.utils.utcnow() + datetime.timedelta(minutes=1))
            async with self.db.execute("SELECT role_id FROM voice_roles") as cursor:
                voice_ids = {row[0] for row in await cursor.fetchall()}
            
            for guild in self.guilds:
                for member in guild.members:
                    if member.voice and not member.voice.self_deaf and not member.bot:
                        if any(r.id in voice_ids for r in member.roles):
                            await self.add_xp(member, 7)

    async def presence_xp_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            await discord.utils.sleep_until(discord.utils.utcnow() + datetime.timedelta(hours=1))
            
            # Salaries
            async with self.db.execute("SELECT role_id, amount FROM presence_roles") as cursor:
                salaries = {row[0]: row[1] for row in await cursor.fetchall()}
            async with self.db.execute("SELECT guild_id, level100_salary FROM guild_settings") as cursor:
                lvl100_salaries = {row[0]: row[1] for row in await cursor.fetchall()}

            for guild in self.guilds:
                lvl100_amount = lvl100_salaries.get(guild.id, 0)
                for member in guild.members:
                    if member.bot: continue
                    total = 0
                    total += sum(salaries[r.id] for r in member.roles if r.id in salaries)
                    
                    if lvl100_amount > 0:
                        async with self.db.execute("SELECT level FROM users WHERE user_id=? AND guild_id=?", (member.id, guild.id)) as c:
                            ud = await c.fetchone()
                            if ud and ud[0] >= 100:
                                total += lvl100_amount

                    if total > 0: await self.add_xp(member, total)

    async def birthday_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            now = datetime.datetime.now()
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
                    if channel:
                        await channel.send(f"🎂 Happy Birthday <@{user_id}>! Hope you have a fantastic day! 🎉")
            await discord.utils.sleep_until(discord.utils.utcnow() + datetime.timedelta(days=1))

bot = LevelBot()

# --- HELPER ---
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

# --- CHAT EVENT ---
@bot.event
async def on_message(message):
    if message.author.bot or not message.guild: return
    
    current_time = time.time()
    
    # Check Channel Multipliers
    channel_mult = 1.0
    async with bot.db.execute("SELECT multiplier FROM channel_multipliers WHERE channel_id=?", (message.channel.id,)) as c:
        cm_data = await c.fetchone()
        if cm_data: channel_mult = cm_data[0]

    # Cooldown Check
    async with bot.db.execute("SELECT next_xp_time FROM users WHERE user_id=? AND guild_id=?", (message.author.id, message.guild.id)) as cursor:
        data = await cursor.fetchone()
        
    next_time = data[0] if data else 0
    if current_time < next_time: return

    # Add XP
    base_xp = random.randint(15, 25)
    final_amount = int(base_xp * channel_mult)
    
    leveled_up, new_level, custom_msg = await bot.add_xp(message.author, final_amount)
    
    if leveled_up:
        # Routing Message
        async with bot.db.execute("SELECT level_channel_id FROM guild_settings WHERE guild_id=?", (message.guild.id,)) as c:
            s = await c.fetchone()
        
        target_channel = message.channel
        if s and s[0] != 0:
            found_channel = message.guild.get_channel(s[0])
            if found_channel: target_channel = found_channel

        # Messages
        if new_level == 75:
            funny_msgs = [
                f"😲 {message.author.mention} hit **Level 75**! Do you remember what grass looks like?",
                f"💀 {message.author.mention} is **Level 75**. Welcome back from your inactivity...",
                f"🚨 **Level 75 Alert!** {message.author.mention} has officially been here too long."
            ]
            await target_channel.send(random.choice(funny_msgs))
        elif custom_msg:
            msg = custom_msg.replace("{user}", message.author.mention).replace("{level}", str(new_level))
            await target_channel.send(msg)
        else:
            await target_channel.send(f"🎉 {message.author.mention} has reached **Level {new_level}**!")

    # Random Cooldown 15-30s
    new_time = current_time + random.randint(15, 30)
    await bot.db.execute("UPDATE users SET next_xp_time=? WHERE user_id=? AND guild_id=?", (new_time, message.author.id, message.guild.id))
    await bot.db.commit()

# =========================================
# 🛠️ GROUP 1: CONFIGURATION (Admin Only)
# =========================================

class ConfigGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="config", description="Manage Server Settings")

    @app_commands.command(name="set_multiplier", description="Set a role as an XP Booster")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_multiplier(self, interaction: discord.Interaction, role: discord.Role, multiplier: float):
        await bot.db.execute("INSERT OR REPLACE INTO role_multipliers (role_id, guild_id, multiplier) VALUES (?, ?, ?)", (role.id, interaction.guild.id, multiplier))
        await bot.db.commit()
        await interaction.response.send_message(f"✅ **{role.name}** is now a **x{multiplier}** Booster!", ephemeral=True)

    @app_commands.command(name="set_channel_boost", description="Set a channel as an XP Booster")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_channel_boost(self, interaction: discord.Interaction, channel: discord.TextChannel, multiplier: float):
        await bot.db.execute("INSERT OR REPLACE INTO channel_multipliers (channel_id, guild_id, multiplier) VALUES (?, ?, ?)", (channel.id, interaction.guild.id, multiplier))
        await bot.db.commit()
        await interaction.response.send_message(f"✅ **{channel.mention}** now gives **x{multiplier}** XP!", ephemeral=True)

    @app_commands.command(name="level_role", description="Assign a role to a level (Replaces previous level roles)")
    @app_commands.checks.has_permissions(administrator=True)
    async def level_role(self, interaction: discord.Interaction, level: int, role: discord.Role):
        if level < 2: return await interaction.response.send_message("❌ Level must be 2 or higher.", ephemeral=True)
        await bot.db.execute("INSERT OR REPLACE INTO level_roles (level, role_id, guild_id) VALUES (?, ?, ?)", (level, role.id, interaction.guild.id))
        await bot.db.commit()
        await interaction.response.send_message(f"✅ Users reaching **Level {level}** will now receive **{role.mention}** (and lose previous level roles).", ephemeral=True)

    @app_commands.command(name="salary_role", description="Give a role hourly passive XP")
    @app_commands.checks.has_permissions(administrator=True)
    async def salary_role(self, interaction: discord.Interaction, role: discord.Role, hourly_amount: int):
        await bot.db.execute("INSERT OR REPLACE INTO presence_roles (role_id, guild_id, amount) VALUES (?, ?, ?)", (role.id, interaction.guild.id, hourly_amount))
        await bot.db.commit()
        await interaction.response.send_message(f"✅ **{role.name}** now earns **{hourly_amount} XP** every hour.", ephemeral=True)

    @app_commands.command(name="salary_level100", description="Set hourly passive XP for Level 100+")
    @app_commands.checks.has_permissions(administrator=True)
    async def salary_level100(self, interaction: discord.Interaction, hourly_amount: int):
        await bot.db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (interaction.guild.id,))
        await bot.db.execute("UPDATE guild_settings SET level100_salary = ? WHERE guild_id = ?", (hourly_amount, interaction.guild.id))
        await bot.db.commit()
        await interaction.response.send_message(f"✅ Users Level 100+ will now earn **{hourly_amount} XP** every hour.", ephemeral=True)

    @app_commands.command(name="ping_channel", description="Where should level up messages go?")
    @app_commands.checks.has_permissions(administrator=True)
    async def ping_channel(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        cid = channel.id if channel else 0
        await bot.db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (interaction.guild.id,))
        await bot.db.execute("UPDATE guild_settings SET level_channel_id = ? WHERE guild_id = ?", (cid, interaction.guild.id))
        await bot.db.commit()
        dest = channel.mention if channel else "the channel where they chat"
        await interaction.response.send_message(f"✅ Level ups will now be sent to {dest}.", ephemeral=True)

    @app_commands.command(name="birthday_channel", description="Where should birthdays be announced?")
    @app_commands.checks.has_permissions(administrator=True)
    async def birthday_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await bot.db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (interaction.guild.id,))
        await bot.db.execute("UPDATE guild_settings SET birthday_channel_id = ? WHERE guild_id = ?", (channel.id, interaction.guild.id))
        await bot.db.commit()
        await interaction.response.send_message(f"✅ Birthdays will be announced in {channel.mention}.", ephemeral=True)

bot.tree.add_command(ConfigGroup())

# =========================================
# 👤 PROFILE SETTINGS (Bio, Birthday, etc)
# =========================================

class ProfileGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="profile", description="Customize your profile")

    @app_commands.command(name="bio", description="Set your rank card bio (Level 20+)")
    async def bio(self, interaction: discord.Interaction, text: str):
        async with bot.db.execute("SELECT level FROM users WHERE user_id=? AND guild_id=?", (interaction.user.id, interaction.guild.id)) as c:
            d = await c.fetchone()
        if not d or d[0] < 20: return await interaction.response.send_message("❌ You must be **Level 20** to set a bio.", ephemeral=True)
        if len(text) > 100: return await interaction.response.send_message("❌ Bio is too long (Max 100 chars).", ephemeral=True)
        await bot.db.execute("UPDATE users SET bio = ? WHERE user_id=? AND guild_id=?", (text, interaction.user.id, interaction.guild.id))
        await bot.db.commit()
        await interaction.response.send_message("✅ Bio updated!", ephemeral=True)

    @app_commands.command(name="levelup_msg", description="Set custom level up message (Level 20+)")
    async def levelup_msg(self, interaction: discord.Interaction, message: str):
        async with bot.db.execute("SELECT level FROM users WHERE user_id=? AND guild_id=?", (interaction.user.id, interaction.guild.id)) as c:
            d = await c.fetchone()
        if not d or d[0] < 20: return await interaction.response.send_message("❌ You must be **Level 20** to set a custom message.", ephemeral=True)
        if "{user}" not in message and "{level}" not in message: return await interaction.response.send_message("❌ Message must contain `{user}` or `{level}`.", ephemeral=True)
        await bot.db.execute("UPDATE users SET custom_msg = ? WHERE user_id=? AND guild_id=?", (message, interaction.user.id, interaction.guild.id))
        await bot.db.commit()
        await interaction.response.send_message("✅ Message updated!", ephemeral=True)

    @app_commands.command(name="birthday", description="Set your birthday DD-MM (Level 50+)")
    async def birthday(self, interaction: discord.Interaction, day: int, month: int):
        async with bot.db.execute("SELECT level FROM users WHERE user_id=? AND guild_id=?", (interaction.user.id, interaction.guild.id)) as c:
            d = await c.fetchone()
        if not d or d[0] < 50: return await interaction.response.send_message("❌ You must be **Level 50** to set your birthday.", ephemeral=True)
        try:
            bdate = f"{day:02d}-{month:02d}"
            datetime.datetime.strptime(bdate, "%d-%m")
            await bot.db.execute("UPDATE users SET birthday = ? WHERE user_id=? AND guild_id=?", (bdate, interaction.user.id, interaction.guild.id))
            await bot.db.commit()
            await interaction.response.send_message(f"✅ Birthday set to **{bdate}**!", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ Invalid Date.", ephemeral=True)

bot.tree.add_command(ProfileGroup())

# =========================================
# 🎁 USER COMMANDS (Rank, Rebirth, Boost)
# =========================================

@bot.tree.command(name="boost_user", description="Level 150+: Give a 2x XP boost to a friend (1hr)")
async def boost_user(interaction: discord.Interaction, target: discord.Member):
    async with bot.db.execute("SELECT level, last_gift_used FROM users WHERE user_id=? AND guild_id=?", (interaction.user.id, interaction.guild.id)) as c:
        d = await c.fetchone()
    
    if not d or d[0] < 150: return await interaction.response.send_message("❌ You must be **Level 150** to use this.", ephemeral=True)

    last_used = d[1]
    now = time.time()
    if now - last_used < 86400:
        hours_left = int((86400 - (now - last_used)) / 3600)
        return await interaction.response.send_message(f"❌ You can gift again in {hours_left} hours.", ephemeral=True)

    end_time = now + 3600 # 1 hour
    await bot.db.execute("INSERT INTO active_boosts (user_id, guild_id, end_time, multiplier) VALUES (?, ?, ?, ?)", (target.id, interaction.guild.id, end_time, 2.0))
    await bot.db.execute("UPDATE users SET last_gift_used = ? WHERE user_id=? AND guild_id=?", (now, interaction.user.id, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(f"🎁 **GIFT SENT!** {target.mention} now has a **2x XP Boost** for 1 hour!")

@bot.tree.command(name="rank", description="Check your stats")
async def rank(interaction: discord.Interaction):
    async with bot.db.execute("SELECT xp, level, rebirth, bio FROM users WHERE user_id=? AND guild_id=?", (interaction.user.id, interaction.guild.id)) as c:
        data = await c.fetchone()
    
    xp, level, rebirth, bio = data if data else (0, 1, 0, "No bio set.")
    xp_needed = 5 * (level ** 2) + (50 * level) + 100
    
    percent = min(100, max(0, (xp / xp_needed) * 100))
    filled = int(percent / 10)
    bar = "🟦" * filled + "⬜" * (10 - filled)
    
    # Boosts Logic
    boosts_text = ""
    
    # Channel Boost (Top Priority)
    channel_mult = 1.0
    async with bot.db.execute("SELECT multiplier FROM channel_multipliers WHERE channel_id=?", (interaction.channel.id,)) as c:
        cm_data = await c.fetchone()
    if cm_data:
        channel_mult = cm_data[0]
        boosts_text += f"⚡ **THIS CHANNEL HAS AN ACTIVE BOOSTER [x{channel_mult}]**\n"

    # Role Boost
    role_mult = 1.0
    async with bot.db.execute("SELECT role_id, multiplier FROM role_multipliers WHERE guild_id=?", (interaction.guild.id,)) as c:
        db_roles = {row[0]: row[1] for row in await c.fetchall()}
    for role in interaction.user.roles:
        if role.id in db_roles:
            role_mult += (db_roles[role.id] - 1.0)
            boosts_text += f"• **{role.name}**: x{db_roles[role.id]}\n"
    
    # Rebirth Boost
    rebirth_mult = 1.0 + (rebirth * 0.2)
    if rebirth > 0:
        boosts_text += f"• **Rebirth {to_roman(rebirth)}**: x{round(rebirth_mult, 1)}\n"

    # Temp Boost
    temp_mult = 1.0
    async with bot.db.execute("SELECT end_time, multiplier FROM active_boosts WHERE user_id=? AND guild_id=?", (interaction.user.id, interaction.guild.id)) as c:
        temp = await c.fetchone()
    if temp and temp[0] > time.time():
        temp_mult = temp[1]
        boosts_text += f"• **Friend Gift**: x{temp_mult}\n"

    # Grand Total (Multiplied)
    grand_total_mult = role_mult * rebirth_mult * channel_mult * temp_mult

    embed = discord.Embed(title=f"🛡️ {interaction.user.display_name}", description=f"*{bio}*", color=interaction.user.color)
    if interaction.user.display_avatar: embed.set_thumbnail(url=interaction.user.display_avatar.url)
    
    embed.add_field(name="Level", value=str(level), inline=True)
    embed.add_field(name="Rebirth", value=f"**{to_roman(rebirth)}**", inline=True)
    embed.add_field(name="Total Multiplier", value=f"x{round(grand_total_mult, 1)}", inline=True)
    embed.add_field(name=f"Progress", value=f"`{bar}` **{int(percent)}%**\n`{xp} / {xp_needed} XP`", inline=False)
    
    if boosts_text: embed.add_field(name="🚀 Active Boosts", value=boosts_text, inline=False)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="rebirth", description="Reset to Level 1 for a permanent boost (Level 200+)")
async def rebirth(interaction: discord.Interaction):
    async with bot.db.execute("SELECT level, rebirth FROM users WHERE user_id=? AND guild_id=?", (interaction.user.id, interaction.guild.id)) as c:
        data = await c.fetchone()
    
    if not data or data[0] < 200: return await interaction.response.send_message("❌ You must be **Level 200** to rebirth.", ephemeral=True)

    new_rebirth = data[1] + 1
    await bot.db.execute("UPDATE users SET level=1, xp=0, rebirth=? WHERE user_id=? AND guild_id=?", (new_rebirth, interaction.user.id, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(f"🚨 **REBIRTH!** {interaction.user.mention} is now Rebirth **{to_roman(new_rebirth)}**!")

# =========================================
# 🛠️ GROUP 2: DEVELOPER TOOLS (Admin Only)
# =========================================

class DevGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="dev", description="Developer Commands")

    @app_commands.command(name="set_level", description="Force set a user's level")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_level(self, interaction: discord.Interaction, member: discord.Member, level: int):
        await bot.db.execute("INSERT OR IGNORE INTO users (user_id, guild_id) VALUES (?, ?)", (member.id, interaction.guild.id))
        await bot.db.execute("UPDATE users SET level = ?, xp = 0 WHERE user_id = ? AND guild_id = ?", (level, member.id, interaction.guild.id))
        await bot.db.commit()
        await interaction.response.send_message(f"🔧 Set {member.mention} to **Level {level}**.", ephemeral=True)

    @app_commands.command(name="set_rebirth", description="Force set a user's rebirth count")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_rebirth(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        await bot.db.execute("INSERT OR IGNORE INTO users (user_id, guild_id) VALUES (?, ?)", (member.id, interaction.guild.id))
        await bot.db.execute("UPDATE users SET rebirth = ? WHERE user_id = ? AND guild_id = ?", (amount, member.id, interaction.guild.id))
        await bot.db.commit()
        await interaction.response.send_message(f"🔧 Set {member.mention} to **Rebirth {to_roman(amount)}**.", ephemeral=True)

bot.tree.add_command(DevGroup())

bot.run(os.getenv('DISCORD_TOKEN'))