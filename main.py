import discord
from discord import app_commands, ui
from discord.ext import commands
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
        await self.db.execute("""CREATE TABLE IF NOT EXISTS users (user_id INTEGER, guild_id INTEGER, xp INTEGER DEFAULT 0, level INTEGER DEFAULT 1, rebirth INTEGER DEFAULT 0, next_xp_time REAL DEFAULT 0, bio TEXT DEFAULT 'No bio set.', custom_msg TEXT DEFAULT NULL, birthday TEXT DEFAULT NULL, last_gift_used REAL DEFAULT 0, PRIMARY KEY (user_id, guild_id))""")
        await self.db.execute("CREATE TABLE IF NOT EXISTS role_multipliers (role_id INTEGER PRIMARY KEY, guild_id INTEGER, multiplier REAL)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS voice_roles (role_id INTEGER PRIMARY KEY, guild_id INTEGER)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS presence_roles (role_id INTEGER PRIMARY KEY, guild_id INTEGER, amount INTEGER)")
        # Note: level100_salary column kept for DB compatibility but logic removed from config
        await self.db.execute("""CREATE TABLE IF NOT EXISTS guild_settings (guild_id INTEGER PRIMARY KEY, level_channel_id INTEGER DEFAULT 0, birthday_channel_id INTEGER DEFAULT 0, level100_salary INTEGER DEFAULT 0)""")
        await self.db.execute("CREATE TABLE IF NOT EXISTS channel_multipliers (channel_id INTEGER PRIMARY KEY, guild_id INTEGER, multiplier REAL)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS active_boosts (user_id INTEGER, guild_id INTEGER, end_time REAL, multiplier REAL)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS level_roles (level INTEGER, role_id INTEGER, guild_id INTEGER, PRIMARY KEY (level, guild_id))")
        await self.db.execute("CREATE TABLE IF NOT EXISTS sponsors (user_id INTEGER, guild_id INTEGER, tier_name TEXT, PRIMARY KEY (user_id, guild_id))")
        
        await self.db.commit()
        
        self.loop.create_task(self.voice_xp_loop())
        self.loop.create_task(self.presence_xp_loop())
        self.loop.create_task(self.birthday_loop())

        self.tree.copy_global_to(guild=TEST_GUILD)
        await self.tree.sync(guild=TEST_GUILD)
        print("✅ Bot Online & Synced")

    async def close(self):
        await self.db.close()
        await super().close()
    
    # --- LOGIC ---
    async def add_xp(self, member, amount):
        async with self.db.execute("SELECT xp, level, rebirth, custom_msg FROM users WHERE user_id = ? AND guild_id = ?", (member.id, member.guild.id)) as cursor:
            data = await cursor.fetchone()
        if not data:
            await self.db.execute("INSERT INTO users (user_id, guild_id, xp, level, rebirth) VALUES (?, ?, ?, ?, ?)", (member.id, member.guild.id, amount, 1, 0))
            await self.db.commit()
            return False, 1, None

        current_xp, current_level, current_rebirth, custom_msg = data
        
        # Multipliers
        rebirth_mult = 1.0 + (current_rebirth * 0.2)
        role_mult = await calculate_multiplier(member)
        temp_mult = 1.0
        now = time.time()
        await self.db.execute("DELETE FROM active_boosts WHERE end_time < ?", (now,))
        await self.db.commit()
        async with self.db.execute("SELECT multiplier FROM active_boosts WHERE user_id=? AND guild_id=?", (member.id, member.guild.id)) as c:
            boost_data = await c.fetchone()
            if boost_data: temp_mult = boost_data[0]

        final_xp = int(amount * rebirth_mult * role_mult * temp_mult)
        new_xp = current_xp + final_xp
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

        await self.db.execute("UPDATE users SET xp = ?, level = ? WHERE user_id = ? AND guild_id = ?", (new_xp, current_level, member.id, member.guild.id))
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
            
            # Note: We keep this loop reading the DB just in case, but the UI to set it is gone.
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
# 🎛️ DASHBOARD UI (THE CONFIG MENU)
# =========================================

# --- INPUT MODALS ---

class MultiplierModal(ui.Modal, title="XP Multiplier"):
    amount = ui.TextInput(label="Multiplier (e.g. 1.5)", placeholder="1.5")
    def __init__(self, target_id, is_role=True):
        super().__init__()
        self.target_id = target_id
        self.is_role = is_role

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = float(self.amount.value)
            if val < 1.0: raise ValueError
            table = "role_multipliers" if self.is_role else "channel_multipliers"
            col = "role_id" if self.is_role else "channel_id"
            await bot.db.execute(f"INSERT OR REPLACE INTO {table} ({col}, guild_id, multiplier) VALUES (?, ?, ?)", (self.target_id, interaction.guild.id, val))
            await bot.db.commit()
            await interaction.response.send_message(f"✅ Set **x{val}** multiplier.", ephemeral=True)
        except:
            await interaction.response.send_message("❌ Invalid number.", ephemeral=True)

class SalaryModal(ui.Modal, title="Hourly Salary"):
    amount = ui.TextInput(label="XP Amount (e.g. 50)", placeholder="50")
    def __init__(self, role_id):
        super().__init__()
        self.role_id = role_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = int(self.amount.value)
            await bot.db.execute("INSERT OR REPLACE INTO presence_roles (role_id, guild_id, amount) VALUES (?, ?, ?)", (self.role_id, interaction.guild.id, val))
            await bot.db.commit()
            await interaction.response.send_message(f"✅ Set salary to **{val} XP/hr**.", ephemeral=True)
        except:
            await interaction.response.send_message("❌ Invalid integer.", ephemeral=True)

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
        except:
            await interaction.response.send_message("❌ Invalid Level (Must be 2+).", ephemeral=True)

# --- VIEWS & SELECTS ---

class RoleActionView(ui.View):
    def __init__(self, role):
        super().__init__()
        self.role = role

    @ui.button(label="Set XP Booster", style=discord.ButtonStyle.blurple)
    async def set_boost(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(MultiplierModal(self.role.id, is_role=True))

    @ui.button(label="Set Salary", style=discord.ButtonStyle.green)
    async def set_salary(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(SalaryModal(self.role.id))

    @ui.button(label="Enable Voice XP", style=discord.ButtonStyle.grey)
    async def set_voice(self, interaction: discord.Interaction, button: ui.Button):
        await bot.db.execute("INSERT OR IGNORE INTO voice_roles (role_id, guild_id) VALUES (?, ?)", (self.role.id, interaction.guild.id))
        await bot.db.commit()
        await interaction.response.send_message(f"✅ Voice XP enabled for {self.role.mention}.", ephemeral=True)
        
    @ui.button(label="Assign to Level...", style=discord.ButtonStyle.primary)
    async def set_level_role(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(LevelRoleModal(self.role.id))

class ChannelActionView(ui.View):
    def __init__(self, channel):
        super().__init__()
        self.channel = channel

    @ui.button(label="Set XP Multiplier", style=discord.ButtonStyle.blurple)
    async def set_boost(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(MultiplierModal(self.channel.id, is_role=False))

    @ui.button(label="Route Level Ups Here", style=discord.ButtonStyle.green)
    async def set_route(self, interaction: discord.Interaction, button: ui.Button):
        await bot.db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (interaction.guild.id,))
        await bot.db.execute("UPDATE guild_settings SET level_channel_id = ? WHERE guild_id = ?", (self.channel.id, interaction.guild.id))
        await bot.db.commit()
        await interaction.response.send_message(f"✅ Level ups routed to {self.channel.mention}.", ephemeral=True)

    @ui.button(label="Route Birthdays Here", style=discord.ButtonStyle.primary)
    async def set_bday(self, interaction: discord.Interaction, button: ui.Button):
        await bot.db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (interaction.guild.id,))
        await bot.db.execute("UPDATE guild_settings SET birthday_channel_id = ? WHERE guild_id = ?", (self.channel.id, interaction.guild.id))
        await bot.db.commit()
        await interaction.response.send_message(f"✅ Birthdays routed to {self.channel.mention}.", ephemeral=True)

# --- SPONSOR MANAGEMENT VIEWS ---

class SponsorTierSelect(ui.Select):
    def __init__(self, target_user):
        self.target_user = target_user
        options = [
            discord.SelectOption(label="Intern", description="$2-5 Tier", emoji="🟢", value="Intern"),
            discord.SelectOption(label="Alpha Tester", description="$10-15 Tier", emoji="🔵", value="Alpha Tester"),
            discord.SelectOption(label="Studio Partner", description="$30+ Tier", emoji="🟡", value="Studio Partner")
        ]
        super().__init__(placeholder="Select Sponsorship Tier...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await bot.db.execute("INSERT OR REPLACE INTO sponsors (user_id, guild_id, tier_name) VALUES (?, ?, ?)", 
                             (self.target_user.id, interaction.guild.id, self.values[0]))
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
            await interaction.response.send_message(f"Select a tier for **{user.name}**:", view=view, ephemeral=True)
        else:
            await bot.db.execute("DELETE FROM sponsors WHERE user_id=? AND guild_id=?", (user.id, interaction.guild.id))
            await bot.db.commit()
            await interaction.response.send_message(f"🗑️ Removed **{user.name}** from sponsors.", ephemeral=True)

class SponsorSettingsView(ui.View):
    @ui.button(label="Add Sponsor", style=discord.ButtonStyle.green, emoji="➕")
    async def add_sponsor_btn(self, interaction: discord.Interaction, button: ui.Button):
        view = ui.View()
        view.add_item(SponsorUserSelect(mode="add"))
        await interaction.response.send_message("Select a user to **ADD** as a sponsor:", view=view, ephemeral=True)

    @ui.button(label="Remove Sponsor", style=discord.ButtonStyle.red, emoji="➖")
    async def remove_sponsor_btn(self, interaction: discord.Interaction, button: ui.Button):
        view = ui.View()
        view.add_item(SponsorUserSelect(mode="remove"))
        await interaction.response.send_message("Select a user to **REMOVE** from sponsors:", view=view, ephemeral=True)

# --- CONFIG DASHBOARD ---

class ConfigDashboard(ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(ConfigSelect())

class ConfigSelect(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Manage Roles", description="Multipliers, Salaries, Voice XP", emoji="🛡️", value="roles"),
            discord.SelectOption(label="Manage Channels", description="Channel Boosts, Routing", emoji="📢", value="channels"),
            discord.SelectOption(label="Sponsor Management", description="Add/Remove Sponsors", emoji="💎", value="general")
        ]
        super().__init__(placeholder="Select a category...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "roles":
            embed = discord.Embed(title="🛡️ Role Configuration", description="Select a role below to configure its Salary, Multiplier, or Voice settings.", color=discord.Color.blue())
            view = ui.View()
            role_select = ui.RoleSelect(placeholder="Pick a role to edit...")
            
            async def role_callback(inter: discord.Interaction):
                role = role_select.values[0]
                await inter.response.send_message(f"⚙️ Configure **{role.name}**:", view=RoleActionView(role), ephemeral=True)
            
            role_select.callback = role_callback
            view.add_item(role_select)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        elif self.values[0] == "channels":
            embed = discord.Embed(title="📢 Channel Configuration", description="Select a channel below to configure Multipliers or Message Routing.", color=discord.Color.green())
            view = ui.View()
            chan_select = ui.ChannelSelect(channel_types=[discord.ChannelType.text, discord.ChannelType.voice], placeholder="Pick a channel...")
            
            async def chan_callback(inter: discord.Interaction):
                chan = chan_select.values[0]
                await inter.response.send_message(f"⚙️ Configure **{chan.name}**:", view=ChannelActionView(chan), ephemeral=True)
                
            chan_select.callback = chan_callback
            view.add_item(chan_select)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        elif self.values[0] == "general":
            embed = discord.Embed(title="💎 Sponsor Management", description="Add or Remove sponsors to grant them tier perks.", color=discord.Color.gold())
            await interaction.response.send_message(embed=embed, view=SponsorSettingsView(), ephemeral=True)

# --- CONFIG COMMAND ---
@bot.tree.command(name="config", description="Open the Server Configuration Dashboard")
@app_commands.checks.has_permissions(administrator=True)
async def config(interaction: discord.Interaction):
    embed = discord.Embed(title="🎛️ Labworks Control Panel", description="Use the dropdown menu below to configure your server's XP system.", color=discord.Color.gold())
    embed.add_field(name="🛡️ Roles", value="Set Multipliers, Salaries, Voice XP, & Level Rewards.", inline=True)
    embed.add_field(name="📢 Channels", value="Set Channel Boosts & Message Routing.", inline=True)
    embed.add_field(name="💎 Sponsors", value="Add/Remove Sponsors.", inline=True)
    await interaction.response.send_message(embed=embed, view=ConfigDashboard(), ephemeral=True)

# =========================================
# 👑 SPONSOR SYSTEM
# =========================================

@bot.tree.command(name="sponsors", description="View the legendary supporters of Labworks")
async def sponsors(interaction: discord.Interaction):
    async with bot.db.execute("SELECT user_id, tier_name FROM sponsors WHERE guild_id = ?", (interaction.guild.id,)) as cursor:
        rows = await cursor.fetchall()

    embed = discord.Embed(
        title="🛡️ Labworks Studio Sponsors",
        description="The incredible individuals helping us build the future of gaming.",
        color=discord.Color.gold()
    )

    if not rows:
        embed.add_field(name="Current Sponsors", value="No sponsors yet! Be the first to support us.")
    else:
        tiers = {"Studio Partner": [], "Alpha Tester": [], "Intern": []}
        for user_id, tier_name in rows:
            if tier_name in tiers:
                tiers[tier_name].append(f"<@{user_id}>")
        
        for tier, members in tiers.items():
            if members:
                embed.add_field(name=f"✨ {tier}", value="\n".join(members), inline=False)

    embed.set_footer(text="Want to support Labworks? Visit github.com/sponsors/Zaxoosh")
    await interaction.response.send_message(embed=embed)

# =========================================
# 👤 PROFILE & RANK
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

@bot.tree.command(name="rank", description="Check your stats or another user's")
async def rank(interaction: discord.Interaction, member: discord.Member = None):
    # Determine target (Member provided, or Author)
    target = member or interaction.user

    # Fetch User Data
    async with bot.db.execute("SELECT xp, level, rebirth, bio FROM users WHERE user_id=? AND guild_id=?", (target.id, interaction.guild.id)) as c:
        data = await c.fetchone()
    
    # Fetch Sponsor Data
    async with bot.db.execute("SELECT tier_name FROM sponsors WHERE user_id=? AND guild_id=?", (target.id, interaction.guild.id)) as c:
        sponsor_data = await c.fetchone()
    sponsor_tier = sponsor_data[0] if sponsor_data else None

    xp, level, rebirth, bio = data if data else (0, 1, 0, "No bio set.")
    xp_needed = 5 * (level ** 2) + (50 * level) + 100
    
    percent = min(100, max(0, (xp / xp_needed) * 100))
    filled = int(percent / 10)
    bar = "🟦" * filled + "⬜" * (10 - filled)
    
    # Boosts Logic
    boosts_text = ""
    channel_mult = 1.0
    async with bot.db.execute("SELECT multiplier FROM channel_multipliers WHERE channel_id=?", (interaction.channel.id,)) as c:
        cm_data = await c.fetchone()
    if cm_data:
        channel_mult = cm_data[0]
        if channel_mult > 1.0:
            boosts_text += f"⚡ **THIS CHANNEL HAS AN ACTIVE BOOSTER [x{channel_mult}]**\n"

    role_mult = 1.0
    async with bot.db.execute("SELECT role_id, multiplier FROM role_multipliers WHERE guild_id=?", (interaction.guild.id,)) as c:
        db_roles = {row[0]: row[1] for row in await c.fetchall()}
    for role in target.roles:
        if role.id in db_roles:
            role_mult += (db_roles[role.id] - 1.0)
            boosts_text += f"• **{role.name}**: x{db_roles[role.id]}\n"
    
    rebirth_mult = 1.0 + (rebirth * 0.2)
    if rebirth > 0:
        boosts_text += f"• **Rebirth {to_roman(rebirth)}**: x{round(rebirth_mult, 1)}\n"

    temp_mult = 1.0
    async with bot.db.execute("SELECT end_time, multiplier FROM active_boosts WHERE user_id=? AND guild_id=?", (target.id, interaction.guild.id)) as c:
        temp = await c.fetchone()
    if temp and temp[0] > time.time():
        temp_mult = temp[1]
        boosts_text += f"• **Friend Gift**: x{temp_mult}\n"

    grand_total_mult = role_mult * rebirth_mult * channel_mult * temp_mult

    embed = discord.Embed(title=f"🛡️ {target.display_name}", description=f"*{bio}*", color=target.color)
    if target.display_avatar: embed.set_thumbnail(url=target.display_avatar.url)
    
    embed.add_field(name="Level", value=str(level), inline=True)
    embed.add_field(name="Rebirth", value=f"**{to_roman(rebirth)}**", inline=True)
    embed.add_field(name="Total Multiplier", value=f"x{round(grand_total_mult, 1)}", inline=True)
    embed.add_field(name=f"Progress", value=f"`{bar}` **{int(percent)}%**\n`{xp} / {xp_needed} XP`", inline=False)
    
    if boosts_text: embed.add_field(name="🚀 Active Boosts", value=boosts_text, inline=False)
    
    # SPONSOR FOOTER LOGIC
    if sponsor_tier == "Studio Partner":
        embed.set_footer(text="💎 Global Studio Partner | Legend")
    
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
# 🛠️ DEVELOPER & ERROR
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

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("🚫 **Access Denied:** This command is reserved for Administrators.", ephemeral=True)
    else:
        print(f"Command Error: {error}")

bot.run(os.getenv('DISCORD_TOKEN'))