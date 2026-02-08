# 🧪 Labworks Leveling Bot

A high-performance, multi-server Discord leveling bot built with **discord.py** and **aiosqlite**. Designed for the Labworks community to drive engagement through leveling, rebirths, passive income, and social boosting.

---

## 🚀 Key Features

### 🏗️ Core Systems

* **Multi-Server Architecture:** All level data and configurations are strictly isolated per guild.
* **Discord Slash Commands:** Fully modern interaction model.

### 🔄 Progression

* **Leveling System:** Scales cleanly across servers.
* **Rebirth System:**

  * Available at **Level 200+**
  * Resets user to Level 1
  * Grants a permanent **x1.2 XP multiplier per rebirth**
  * Rebirth count displayed in **Roman Numerals**

### 🎭 Roles & Ranks

* **Level Roles (Replace Mode):**

  * Automatically assigns roles at specific levels
  * New roles replace old ones to keep the member list clean

### 💰 Passive Income & XP Sources

* **Presence Salary:**

  * Configurable hourly XP for staff or specific roles
* **Level 100 Perk:**

  * Automatic hourly XP salary for all users Level 100+
* **Voice XP:**

  * Earn XP while in voice channels
  * Fixed rate at **⅓ of chat XP**

### 🎁 Social & Boosting

* **User Boost Gifting:**

  * Level 150+ users can gift another user a **2× XP boost**
  * 24-hour cooldown per user

### 🗺️ Dynamic Multipliers

* Role-based XP multipliers
* Channel-specific XP boosts

### 🎨 Personalization

* **Custom Bios:** Unlock at Level 20+
* **Custom Level-Up Messages:** Unlock at Level 20+

### 🎂 Birthdays

* Unlock at Level 50+
* Automatic birthday announcements in-server

---

## 🛠️ Technical Stack

* **Language:** Python 3.10+
* **Library:** [discord.py](https://github.com/Rapptz/discord.py)
* **Database:** SQLite via [aiosqlite](https://github.com/omnilib/aiosqlite)
* **Architecture:** Slash-command based

---

## 📦 Installation & Setup

### 1️⃣ Clone the Repository

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git
cd YOUR_REPO_NAME
```

### 2️⃣ Install Dependencies

```bash
pip install -r requirements.txt
```

### 3️⃣ Environment Configuration

Create a `.env` file in the project root:

```env
DISCORD_TOKEN=your_bot_token_here
```

### 4️⃣ Database Initialization

* The bot automatically creates `levels.db` on first run.

### 5️⃣ Run the Bot

```bash
python main.py
```

---

## 🎮 Command Reference

### 👤 User Commands

* `/rank` — View level, XP progress bar, and active boosts
* `/rebirth` — Reset to Level 1 for a permanent XP multiplier (Level 200+)
* `/profile bio` — Set your profile bio (Level 20+)
* `/profile levelup_msg` — Customize your level-up message (Level 20+)
* `/profile birthday` — Set your birthday (Level 50+)
* `/boost_user` — Gift a 1-hour 2× XP boost (Level 150+)

### 🛠️ Admin / Configuration (`/config`)

* `set_multiplier` — Assign XP multipliers to roles
* `set_channel_boost` — Assign XP multipliers to channels
* `level_role` — Map roles to levels (replace mode)
* `salary_role` — Set hourly XP salary for specific roles
* `salary_level100` — Configure Level 100+ passive salary
* `ping_channel` — Set channel for level-up announcements
* `view` — View all current server settings

### 🧪 Developer Tools (`/dev`)

* `set_level` — Force set a user's level
* `set_rebirth` — Force set a user's rebirth count

---

## ⚠️ Security Notes

* **Never** commit `.env` or `levels.db` to GitHub
* Use the provided `.gitignore`
* Enable the following intents in the Discord Developer Portal:

  * **Server Members Intent**
  * **Message Content Intent**

---

*Developed for the Labworks Community.*
