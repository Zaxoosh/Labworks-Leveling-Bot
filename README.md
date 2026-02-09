# Labworks Leveling Bot

A multi-server Discord leveling bot built with discord.py and aiosqlite.  
Designed for the Labworks community to encourage engagement through leveling, rebirths, passive XP, and social boosts.

The bot uses a fully isolated per-guild architecture and modern slash commands.

---

## Features

### Core
- Multi-server support with strict guild data isolation
- Slash-command only (no legacy prefix commands)
- Scales cleanly across large servers

### Leveling and Progression
- XP-based leveling system
- Rebirth system:
  - Unlocks at Level 200
  - Resets level back to 1
  - Grants a permanent 1.2x XP multiplier per rebirth
  - Rebirth count displayed using Roman numerals

### Roles and Ranks
- Level-based role rewards
- Replace-mode assignment to avoid role stacking

### XP Sources and Passive Income
- Chat XP
- Voice XP (one third of chat XP rate)
- Hourly presence salary for staff or configured roles
- Automatic hourly XP salary for users Level 100 and above

### Boosting and Social Features
- Level 150 and above users can gift another user a 2x XP boost
- 24 hour cooldown per gifter

### Multipliers
- Role-based XP multipliers
- Channel-specific XP boosts

### Customization
- Custom user bios unlocked at Level 20
- Custom level-up messages unlocked at Level 20

### Birthdays
- Unlocks at Level 50
- Automatic birthday announcements in server

---

## Tech Stack

- Python 3.10 or higher
- discord.py
- SQLite using aiosqlite
- Slash command based architecture

---

## Installation

Clone the repository:
```bash
git clone https://github.com/Zaxoosh/Labworks-Leveling-Bot.git  
cd Labworks-Leveling-Bot
```
Install dependencies:
```bash
pip install -r requirements.txt
```
Create an environment file in the project root named .env:
```bash
DISCORD_TOKEN=your_bot_token_here
```
Database notes:
- The database file levels.db is created automatically on first run

Run the bot:
```bash
python main.py
```
---

## Commands

Here is the updated **Commands** section for your `README.md` in Markdown format. You can copy and paste this directly.

---

### 🎮 Commands

#### **Public Commands**

| Command | Description | Requirement |
| --- | --- | --- |
| `/rank` | View your current Level, XP, Rebirth status, and active multipliers. | None |
| `/leaderboard` | View top players (All-Time, Monthly, Weekly, or Message count). | None |
| `/sponsors` | View the legendary supporters of the Labworks studio. | None |
| `/profile bio` | Set a custom bio for your rank card. | Level 20+ |
| `/profile levelup_msg` | Set a custom message that triggers when you level up. | Level 20+ |
| `/profile birthday` | Set your birthday (DD-MM) for server-wide celebrations. | Level 50+ |
| `/boost_user` | Gift a 1-hour 2x XP boost to a friend (24h Cooldown). | Level 150+ |
| `/rebirth` | Reset to Level 1 for a permanent **x1.2 XP boost** and prestige tag. | Level 200 |

#### **Admin & Developer Commands**

| Command | Description |
| --- | --- |
| `/config` | **Labworks Control Panel**: Manage role salaries, XP multipliers, and channel routing. |
| `/dev` | **Control Center**: Force set player levels/rebirths and manage Global XP Events. |
| `/sync_roles` | Force syncs user roles based on their current level (fixes skipped roles). |
| `/debug_rank` | Analyze exactly why a specific user is or isn't receiving a level role. |
| `!sync` | (Owner Only) Instantly syncs Slash Commands to the current guild. |
| `!clearglobals` | (Owner Only) Wipes all global slash commands for troubleshooting. |

---

### ⚙️ Setup & Configuration

To get the bot fully operational after installation:

1. **Role Mapping:** Use `/config` -> **Manage Roles** -> **Assign to Level** to link your Discord roles to the leveling system.
2. **Channel Routing:** Use `/config` -> **Manage Channels** -> **Route Level Ups** to keep general chat clean.
3. **Security:** Use `/dev` -> **Security & Audit** to set a staff-only channel for logging suspicious XP gains (e.g., >150 XP per message).

---

**Would you like me to generate a "Contribution Guidelines" section next to explain how to submit Pull Requests for new features?**

---

## Roadmap

Planned features and improvements:

- Leaderboards (global and per-server) ✅
- Server boss fights
- Web dashboard for configuration and statistics
- Seasonal XP events
- Advanced anti XP abuse detection
- Database migration support such as PostgreSQL


## Suggestions and Feedback

Suggestions, feature requests, and bug reports are welcome.

Please submit all suggestions and issues through GitHub Issues.  
Using GitHub Issues keeps feedback organised, visible, and easy to track.

---

## Security Notes

- Do not commit the .env file or levels.db to version control
- Use the provided .gitignore file
- Enable the following intents in the Discord Developer Portal:
  - Server Members Intent
  - Message Content Intent

---

## Sponsorship

Support the development of Labworks and unlock in-game and community perks.

Tier: Intern  
Price: 2 USD  
Perks: Sponsor role, 1.1x XP boost, GitHub credits

Tier: Alpha Tester  
Price: 5 USD  
Perks: 75 XP per hour salary, sandbox priority access, voting rights

Tier: Studio Partner  
Price: 30 USD
Perks: Permanent Legend rank, rebirths unlocked at Level 10, double daily gifting

Sponsorships are handled through GitHub Sponsors and synced in Discord using the /sponsors command.

https://github.com/sponsors/Zaxoosh

---

## License and Credits

Developed for the Labworks Community.  
Contributions, issues, and pull requests are welcome.
