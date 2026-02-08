import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
import os
import aiosqlite
import random
import time
import datetime
from dotenv import load_dotenv

load_dotenv()

TEST_GUILD_ID = 1041046184552308776
TEST_GUILD = discord.Object(id=TEST_GUILD_ID)

# --- ROMAN NUMERAL HELPER ---
def to_roman(num):
    val = [1000, 900, 500, 400, 100, 90, 50, 40, 10, 9, 5, 4, 1]
    syb = ["M", "CM", "D", "CD", "C", "XC", "L", "XL", "X", "IX", "V", "IV", "I"]
    roman_num = ''
    i = 0
    while  num > 0:
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
        
    async def setup_hook(self):
        self.db = await aiosqlite.connect("levels.db")
        
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
        # Added global_xp_mult and audit_channel_id
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY, 
                level_channel_id INTEGER DEFAULT 0, 
                birthday_channel_id INTEGER DEFAULT 0, 
                level100_salary INTEGER DEFAULT 0,
                global_xp_mult REAL DEFAULT 1.0,
                audit_channel_id INTEGER DEFAULT 0
            )
        """)
        await self.db.execute("CREATE TABLE IF NOT EXISTS role_multipliers (role_id INTEGER PRIMARY KEY, guild_id INTEGER, multiplier REAL)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS voice_roles (role_id INTEGER PRIMARY KEY, guild_id INTEGER)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS presence_roles (role_id INTEGER PRIMARY KEY, guild_id INTEGER, amount INTEGER)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS channel_multipliers (channel_id INTEGER PRIMARY KEY, guild_id INTEGER, multiplier REAL)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS active_boosts (user_id INTEGER, guild_id INTEGER, end_time REAL, multiplier REAL)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS level_roles (level INTEGER, role_id INTEGER, guild_id INTEGER, PRIMARY KEY (level, guild_id))")
        await self.db.execute("CREATE TABLE IF NOT EXISTS sponsors (user_id INTEGER, guild_id INTEGER, tier_name TEXT, PRIMARY KEY (user_id, guild_id))")
        
        await self.db.commit()
        
        self.loop.create_task(self.voice_xp_loop())
        self.loop.create_task(self.presence_xp_loop())
        self.loop.create_task(self.birthday_loop())
        self.loop.create_task(self.reset_stats_loop())

        self.tree.copy_global_to(guild=TEST_GUILD)
        await self.tree.sync(guild=TEST_GUILD)
        print("✅ Bot Online & Synced")

    async def close(self):
        await self.db.close()
        await super().close()
    
    # --- LOGIC ---
    async def add_xp(self, member, amount):
        # 1. Fetch User Data
        async with self.db.execute("SELECT xp, level, rebirth, custom_msg FROM users WHERE user_id = ? AND guild_id = ?", (member.id, member.guild.id)) as cursor:
            data = await cursor.fetchone()
        
        if not data:
            await self.db.execute("INSERT INTO users (user_id, guild_id, xp, weekly_xp, monthly_xp, level, rebirth) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                                  (member.id, member.guild.id, amount, amount, amount, 1, 0))
            await self.db.commit()
            return False, 1, None

        current_xp, current_level, current_rebirth, custom_msg = data
        
        # 2. Fetch Multipliers
        # A. Rebirth
        rebirth_mult = 1.0 + (current_rebirth * 0.2)
        # B. Role
        role_mult = await calculate_multiplier(member)
        # C. Temp Boosts
        temp_mult = 1.0
        now = time.time()
        await self.db.execute("DELETE FROM active_boosts WHERE end_time < ?", (now,))
        await self.db.commit()
        async with self.db.execute("SELECT multiplier FROM active_boosts WHERE user_id=? AND guild_id=?", (member.id, member.guild.id)) as c:
            boost_data = await c.fetchone()
            if boost_data: temp_mult = boost_data[0]
        # D. Global Event Multiplier
        global_mult = 1.0
        async with self.db.execute("SELECT global_xp_mult, audit_channel_id FROM guild_settings WHERE guild_id=?", (member.guild.id,)) as c:
            g_data = await c.fetchone()
            if g_data: global_mult = g_data[0]
            audit_id = g_data[1] if g_data else 0

        # 3. Calculate Final
        final_xp = int(amount * rebirth_mult * role_mult * temp_mult * global_mult)
        
        # 4. AUDIT: Suspicious Activity Check
        # If a single message grants > 150 XP (High value for standard chatting), flag it.
        if final_xp > 150 and audit_id != 0:
            audit_chan = member.guild.get_channel(audit_id)
            if audit_chan:
                await audit_chan.send(f"⚠️ **SUSPICIOUS ACTIVITY**\nUser: {member.mention}\nGained: **{final_xp} XP** in one action.\nMultipliers: Rb `x{rebirth_mult}` | Role `x{role_mult}` | Global `x{global_mult}`")

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
            async with self.db.execute("SELECT level, role_id FROM level_roles WHERE guild_id = ?", (member.guild.id,)) as c:
                all_level_roles = await c.fetchall()
            level_map = {row[0]: row[1] for row in all_level_roles}
            if current_level in level_map:
                new_role_id = level_map[current_level]
                new_role = member.guild.get_role(new_role_id)
                roles_to_remove = []
                all_ids = set(level_map.values())
                for role in member.roles:
                    if role.id in all_ids and role.id != new_role_id:
                        roles_to_remove.append(role)
                try:
                    if roles_to_remove: await member.remove_roles(*roles_to_remove)
                    if new_role: await member.add_roles(new_role)
                except: pass

        # 7. Save
        await self.db.execute("""
            UPDATE users 
            SET xp = ?, weekly_xp = weekly_xp + ?, monthly_xp = monthly_xp + ?, level = ? 
            WHERE user_id = ? AND guild_id = ?
        """, (new_xp, final_xp, final_xp, current_level, member.id, member.guild.id))
        
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
            async with self.db.execute("SELECT role_id, amount FROM presence_roles") as cursor:
                salaries = {row[0]: row[1] for row in await cursor.fetchall()}
            
            async with self.db.execute("SELECT guild_id, level100_salary FROM guild_settings") as cursor:
                lvl100_salaries = {row[0]: row[1] for row in await cursor.fetchall()}

            for guild in self.guilds:
                lvl100_amount = lvl100_salaries.get(guild.id, 0)
                for member in guild.members:
                    if member.bot: continue
                    total = sum(salaries[r.id] for r in member.roles if r.id in salaries)
                    if lvl100_amount > 0:
                        async with self.db.execute("SELECT level FROM users WHERE user_id=? AND guild_id=?", (member.id, guild.id)) as c:
                            ud = await c.fetchone()
                            if ud and ud[0] >= 100: total += lvl100_amount
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
                    if channel: await channel.send(f"🎂 Happy Birthday <@{user_id}>! Hope you have a fantastic day! 🎉")
            await discord.utils.sleep_until(discord.utils.utcnow() + datetime.timedelta(days=1))

    # RESET LOOP
    async def reset_stats_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            now = datetime.datetime.utcnow()
            if now.day == 1 and now.hour == 0 and now.minute < 5:
                await self.db.execute("UPDATE users SET monthly_xp = 0")
                await self.db.commit()
            if now.weekday() == 0 and now.hour == 0 and now.minute < 5:
                await self.db.execute("UPDATE users SET weekly_xp = 0")
                await self.db.commit()
            await discord.utils.sleep_until(discord.utils.utcnow() + datetime.timedelta(hours=1))

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
async def on_message(message):
    if message.author.bot or not message.guild: return
    current_time = time.time()
    
    channel_mult = 1.0
    async with bot.db.execute("SELECT multiplier FROM channel_multipliers WHERE channel_id=?", (message.channel.id,)) as c:
        cm_data = await c.fetchone()
        if cm_data: channel_mult = cm_data[0]

    async with bot.db.execute("SELECT next_xp_time FROM users WHERE user_id=? AND guild_id=?", (message.author.id, message.guild.id)) as cursor:
        data = await cursor.fetchone()
    if current_time < (data[0] if data else 0): return

    leveled_up, new_level, custom_msg = await bot.add_xp(message.author, int(random.randint(15, 25) * channel_mult))
    
    # Message Count
    await bot.db.execute("UPDATE users SET message_count = message_count + 1 WHERE user_id = ? AND guild_id = ?", (message.author.id, message.guild.id))
    await bot.db.commit()
    
    if leveled_up:
        async with bot.db.execute("SELECT level_channel_id FROM guild_settings WHERE guild_id=?", (message.guild.id,)) as c:
            s = await c.fetchone()
        target = message.guild.get_channel(s[0]) if s and s[0] != 0 else message.channel
        
        if new_level == 75: await target.send(f"💀 {message.author.mention} hit **Level 75**. Welcome back from inactivity...")
        elif custom_msg: await target.send(custom_msg.replace("{user}", message.author.mention).replace("{level}", str(new_level)))
        else: await target.send(f"🎉 {message.author.mention} reached **Level {new_level}**!")

    await bot.db.execute("UPDATE users SET next_xp_time=? WHERE user_id=? AND guild_id=?", (current_time + random.randint(15, 30), message.author.id, message.guild.id))
    await bot.db.commit()

# =========================================
# 🎛️ DEV MENU & DASHBOARDS
# =========================================

# --- MODALS FOR DEV ACTIONS ---
class DevValueModal(ui.Modal, title="Update Player Stats"):
    amount = ui.TextInput(label="Enter Amount", placeholder="10")
    def __init__(self, target_user, mode):
        super().__init__()
        self.target_user = target_user
        self.mode = mode # "level" or "rebirth"

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = int(self.amount.value)
            col = "level" if self.mode == "level" else "rebirth"
            await bot.db.execute(f"INSERT OR IGNORE INTO users (user_id, guild_id) VALUES (?, ?)", (self.target_user.id, interaction.guild.id))
            # Reset XP if setting level to avoid math bugs
            extra_sql = ", xp=0" if self.mode == "level" else ""
            await bot.db.execute(f"UPDATE users SET {col} = ?{extra_sql} WHERE user_id = ? AND guild_id = ?", (val, self.target_user.id, interaction.guild.id))
            await bot.db.commit()
            
            # Audit Log Check
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
            if val > 1.0: await interaction.channel.send(msg) # Announce to channel too
        except: await interaction.response.send_message("❌ Invalid number.", ephemeral=True)

# --- VIEWS FOR DEV MENU ---

class AuditChannelSelect(ui.ChannelSelect):
    def __init__(self):
        super().__init__(channel_types=[discord.ChannelType.text], placeholder="Select Audit Channel...")
    async def callback(self, interaction: discord.Interaction):
        await bot.db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (interaction.guild.id,))
        await bot.db.execute("UPDATE guild_settings SET audit_channel_id = ? WHERE guild_id = ?", (self.values[0].id, interaction.guild.id))
        await bot.db.commit()
        await interaction.response.send_message(f"🔒 Security Log set to {self.values[0].mention}.", ephemeral=True)

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

# =========================================
# 🏆 LEADERBOARD SYSTEM
# =========================================

class LeaderboardSelect(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="All-Time XP", value="xp", emoji="🏆", description="Total Experience gained forever."),
            discord.SelectOption(label="Monthly XP", value="monthly_xp", emoji="📅", description="Experience gained this month."),
            discord.SelectOption(label="Weekly XP", value="weekly_xp", emoji="⏳", description="Experience gained this week."),
            discord.SelectOption(label="Messages", value="message_count", emoji="💬", description="Total chat messages sent.")
        ]
        super().__init__(placeholder="Filter Leaderboard...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        sort_col = self.values[0]
        titles = {
            "xp": "🏆 All-Time XP Leaderboard",
            "monthly_xp": "📅 Monthly XP Leaderboard",
            "weekly_xp": "⏳ Weekly XP Leaderboard",
            "message_count": "💬 Top Chatters (Message Count)"
        }
        async with bot.db.execute(f"SELECT user_id, {sort_col}, level, rebirth FROM users WHERE guild_id = ? ORDER BY {sort_col} DESC LIMIT 10", (interaction.guild.id,)) as c:
            rows = await c.fetchall()
        embed = discord.Embed(title=titles[sort_col], color=discord.Color.gold())
        if not rows: embed.description = "No data found yet! Start chatting."
        else:
            desc = ""
            for index, row in enumerate(rows, 1):
                uid, val, lvl, rebirth = row
                rebirth_str = f" [Rb {to_roman(rebirth)}]" if rebirth > 0 else ""
                stat_str = f"**{val:,}** msgs" if sort_col == "message_count" else f"**{val:,}** XP"
                desc += f"`#{index}` <@{uid}>{rebirth_str} • Lvl {lvl} • {stat_str}\n"
            embed.description = desc
        embed.set_footer(text="Implemented on 08/02/2026. All tracking for messages started after that.")
        await interaction.response.edit_message(embed=embed, view=self.view)

class LeaderboardView(ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(LeaderboardSelect())

@bot.tree.command(name="leaderboard", description="View the server leaderboards")
async def leaderboard(interaction: discord.Interaction):
    async with bot.db.execute("SELECT user_id, xp, level, rebirth FROM users WHERE guild_id = ? ORDER BY xp DESC LIMIT 10", (interaction.guild.id,)) as c:
        rows = await c.fetchall()
    embed = discord.Embed(title="🏆 All-Time XP Leaderboard", color=discord.Color.gold())
    if not rows: embed.description = "No data found yet! Start chatting."
    else:
        desc = ""
        for index, row in enumerate(rows, 1):
            uid, val, lvl, rebirth = row
            rebirth_str = f" [Rb {to_roman(rebirth)}]" if rebirth > 0 else ""
            desc += f"`#{index}` <@{uid}>{rebirth_str} • Lvl {lvl} • **{val:,}** XP\n"
        embed.description = desc
    embed.set_footer(text="Implemented on 08/02/2026. All tracking for messages started after that.")
    await interaction.response.send_message(embed=embed, view=LeaderboardView())

# =========================================
# 🎛️ CONFIG DASHBOARD
# =========================================
# (Standard Config Logic - Roles, Channels, Sponsors)

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
    @ui.button(label="Voice XP", style=discord.ButtonStyle.grey)
    async def set_voice(self, i, b):
        await bot.db.execute("INSERT OR IGNORE INTO voice_roles (role_id, guild_id) VALUES (?, ?)", (self.role.id, i.guild.id))
        await bot.db.commit()
        await i.response.send_message(f"✅ Voice XP enabled.", ephemeral=True)
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

class ConfigDashboard(ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(ConfigSelect())

class ConfigSelect(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Manage Roles", description="Multipliers, Salaries", emoji="🛡️", value="roles"),
            discord.SelectOption(label="Manage Channels", description="Boosts, Routing", emoji="📢", value="channels"),
            discord.SelectOption(label="Sponsors", description="Add/Remove Sponsors", emoji="💎", value="general")
        ]
        super().__init__(placeholder="Config Category...", min_values=1, max_values=1, options=options)
    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "roles":
            view = ui.View()
            role_select = ui.RoleSelect(placeholder="Pick a role...")
            async def role_callback(inter):
                await inter.response.send_message(f"⚙️ **{role_select.values[0].name}**:", view=RoleActionView(role_select.values[0]), ephemeral=True)
            role_select.callback = role_callback
            view.add_item(role_select)
            await interaction.response.send_message(embed=discord.Embed(title="🛡️ Roles", color=discord.Color.blue()), view=view, ephemeral=True)
        elif self.values[0] == "channels":
            view = ui.View()
            chan_select = ui.ChannelSelect(channel_types=[discord.ChannelType.text, discord.ChannelType.voice], placeholder="Pick a channel...")
            async def chan_callback(inter):
                await inter.response.send_message(f"⚙️ **{chan_select.values[0].name}**:", view=ChannelActionView(chan_select.values[0]), ephemeral=True)
            chan_select.callback = chan_callback
            view.add_item(chan_select)
            await interaction.response.send_message(embed=discord.Embed(title="📢 Channels", color=discord.Color.green()), view=view, ephemeral=True)
        elif self.values[0] == "general":
            await interaction.response.send_message(embed=discord.Embed(title="💎 Sponsors", color=discord.Color.gold()), view=SponsorSettingsView(), ephemeral=True)

@bot.tree.command(name="config", description="Open the Server Configuration Dashboard")
@app_commands.checks.has_permissions(administrator=True)
async def config(interaction: discord.Interaction):
    embed = discord.Embed(title="🎛️ Labworks Control Panel", description="Use the dropdown menu below to configure your server's XP system.", color=discord.Color.gold())
    embed.add_field(name="🛡️ Roles", value="Set Multipliers, Salaries, Voice XP, & Level Rewards.", inline=True)
    embed.add_field(name="📢 Channels", value="Set Channel Boosts & Message Routing.", inline=True)
    embed.add_field(name="💎 Sponsors", value="Add/Remove Sponsors.", inline=True)
    await interaction.response.send_message(embed=embed, view=ConfigDashboard(), ephemeral=True)

# =========================================
# 👑 SPONSORS & PROFILES
# =========================================

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
            bdate = f"{day:02d}-{month:02d}"
            datetime.datetime.strptime(bdate, "%d-%m")
            await bot.db.execute("UPDATE users SET birthday = ? WHERE user_id=? AND guild_id=?", (bdate, i.user.id, i.guild.id))
            await bot.db.commit()
            await i.response.send_message(f"✅ Birthday set to **{bdate}**!", ephemeral=True)
        except: await i.response.send_message("❌ Invalid Date.", ephemeral=True)

bot.tree.add_command(ProfileGroup())

@bot.tree.command(name="boost_user", description="Level 150+: Give a 2x XP boost to a friend (1hr)")
async def boost_user(interaction: discord.Interaction, target: discord.Member):
    async with bot.db.execute("SELECT level, last_gift_used FROM users WHERE user_id=? AND guild_id=?", (interaction.user.id, interaction.guild.id)) as c:
        d = await c.fetchone()
    if not d or d[0] < 150: return await interaction.response.send_message("❌ You must be **Level 150** to use this.", ephemeral=True)
    last_used = d[1]
    now = time.time()
    if now - last_used < 86400: return await interaction.response.send_message(f"❌ Cooldown active.", ephemeral=True)
    end_time = now + 3600
    await bot.db.execute("INSERT INTO active_boosts (user_id, guild_id, end_time, multiplier) VALUES (?, ?, ?, ?)", (target.id, interaction.guild.id, end_time, 2.0))
    await bot.db.execute("UPDATE users SET last_gift_used = ? WHERE user_id=? AND guild_id=?", (now, interaction.user.id, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(f"🎁 **GIFT SENT!** {target.mention} now has a **2x XP Boost** for 1 hour!")

@bot.tree.command(name="rank", description="Check your stats or another user's")
async def rank(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    
    # 1. Fetch Basic Data
    async with bot.db.execute("SELECT xp, level, rebirth, bio FROM users WHERE user_id=? AND guild_id=?", (target.id, interaction.guild.id)) as c:
        data = await c.fetchone()
    async with bot.db.execute("SELECT tier_name FROM sponsors WHERE user_id=? AND guild_id=?", (target.id, interaction.guild.id)) as c:
        s_data = await c.fetchone()
    
    xp, level, rebirth, bio = data if data else (0, 1, 0, "No bio set.")
    
    # 2. Calculate Progress
    xp_needed = 5 * (level ** 2) + (50 * level) + 100
    percent = min(100, max(0, (xp / xp_needed) * 100))
    bar = "🟦" * int(percent / 10) + "⬜" * (10 - int(percent / 10))
    
    # 3. CALCULATE MULTIPLIERS (The Missing Part)
    boosts_text = ""
    
    # A. Global Event
    global_mult = 1.0
    async with bot.db.execute("SELECT global_xp_mult FROM guild_settings WHERE guild_id=?", (interaction.guild.id,)) as c:
        g_data = await c.fetchone()
        if g_data and g_data[0] > 1.0:
            global_mult = g_data[0]
            boosts_text += f"🌍 **Global Event**: x{global_mult}\n"

    # B. Channel Boost
    channel_mult = 1.0
    async with bot.db.execute("SELECT multiplier FROM channel_multipliers WHERE channel_id=?", (interaction.channel.id,)) as c:
        cm_data = await c.fetchone()
    if cm_data and cm_data[0] > 1.0:
        channel_mult = cm_data[0]
        boosts_text += f"⚡ **Channel Boost**: x{channel_mult}\n"

    # C. Role Boost
    role_mult = 1.0
    async with bot.db.execute("SELECT role_id, multiplier FROM role_multipliers WHERE guild_id=?", (interaction.guild.id,)) as c:
        db_roles = {row[0]: row[1] for row in await c.fetchall()}
    for role in target.roles:
        if role.id in db_roles:
            bonus = db_roles[role.id] - 1.0
            role_mult += bonus
            boosts_text += f"🛡️ **{role.name}**: x{db_roles[role.id]}\n"
    
    # D. Rebirth Boost
    rebirth_mult = 1.0 + (rebirth * 0.2)
    if rebirth > 0:
        boosts_text += f"🔄 **Rebirth {to_roman(rebirth)}**: x{round(rebirth_mult, 1)}\n"

    # E. Temp/Friend Boost
    temp_mult = 1.0
    async with bot.db.execute("SELECT end_time, multiplier FROM active_boosts WHERE user_id=? AND guild_id=?", (target.id, interaction.guild.id)) as c:
        temp = await c.fetchone()
    if temp and temp[0] > time.time():
        temp_mult = temp[1]
        boosts_text += f"🎁 **Friend Gift**: x{temp_mult}\n"

    # 4. Total Multiplier Calculation
    grand_total = global_mult * channel_mult * role_mult * rebirth_mult * temp_mult

    # 5. Build Embed
    embed = discord.Embed(title=f"🛡️ {target.display_name}", description=f"*{bio}*", color=target.color)
    if target.display_avatar: embed.set_thumbnail(url=target.display_avatar.url)
    
    embed.add_field(name="Level", value=str(level), inline=True)
    embed.add_field(name="Rebirth", value=f"**{to_roman(rebirth)}**", inline=True)
    embed.add_field(name="Total Multiplier", value=f"**x{round(grand_total, 2)}**", inline=True)
    
    embed.add_field(name="Progress", value=f"`{bar}` **{int(percent)}%**\n`{xp} / {xp_needed} XP`", inline=False)
    
    if boosts_text:
        embed.add_field(name="🚀 Active Boosts", value=boosts_text, inline=False)

    if s_data and s_data[0] == "Studio Partner": 
        embed.set_footer(text="💎 Global Studio Partner | Legend")
        
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="rebirth", description="Reset to Level 1 for a permanent boost (Level 200+)")
async def rebirth(interaction: discord.Interaction):
    async with bot.db.execute("SELECT level, rebirth FROM users WHERE user_id=? AND guild_id=?", (interaction.user.id, interaction.guild.id)) as c:
        data = await c.fetchone()
    if not data or data[0] < 200: return await interaction.response.send_message("❌ Need Level 200.", ephemeral=True)
    await bot.db.execute("UPDATE users SET level=1, xp=0, rebirth=? WHERE user_id=? AND guild_id=?", (data[1] + 1, interaction.user.id, interaction.guild.id))
    await bot.db.commit()
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
    if isinstance(e, app_commands.MissingPermissions): await i.response.send_message("🚫 Admin Only.", ephemeral=True)
    else: print(f"Error: {e}")

bot.run(os.getenv('DISCORD_TOKEN'))