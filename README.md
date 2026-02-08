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

git clone https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git  
cd YOUR_REPO_NAME

Install dependencies:

pip install -r requirements.txt

Create an environment file in the project root named .env:

DISCORD_TOKEN=your_bot_token_here

Database notes:
- The database file levels.db is created automatically on first run

Run the bot:

python main.py

---

## Commands

### User Commands
- /rank – View your level, XP progress, and active boosts
- /rebirth – Reset to Level 1 for a permanent XP multiplier (Level 200 and above)
- /profile bio – Set a profile bio (Level 20 and above)
- /profile levelup_msg – Customize your level-up message (Level 20 and above)
- /profile birthday – Set your birthday (Level 50 and above)
- /boost_user – Gift a 2x XP boost to another user (Level 150 and above)

### Admin and Configuration Commands (/config)
- set_multiplier – Assign XP multipliers to roles
- set_channel_boost – Assign XP multipliers to channels
- level_role – Configure level-based roles using replace mode
- salary_role – Set hourly XP salary for roles
- salary_level100 – Configure Level 100 and above passive salary
- ping_channel – Set the level-up announcement channel
- view – View the current server configuration

### Developer Commands (/dev)
- set_level – Force set a user’s level
- set_rebirth – Force set a user’s rebirth count

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
