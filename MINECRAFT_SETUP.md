# Minecraft Fabric Integration Setup

This integration adds Minecraft activity as an XP source for the existing Discord leveling bot. It does not replace the current leveling logic.

## Bot Setup

Install the updated requirements:

```bash
pip install -r requirements.txt
```

Set a shared API token for the bot:

```text
MINECRAFT_API_TOKEN=use-a-long-random-secret
```

Optional bot settings:

```text
MINECRAFT_API_HOST=0.0.0.0
MINECRAFT_API_PORT=8095
MINECRAFT_DAILY_XP_CAP=1500
MINECRAFT_TARGET_GUILD_ID=1041046184552308776
MINECRAFT_ANNOUNCE_ENABLED=false
```

The bot exposes:

```text
POST /minecraft/activity
GET /minecraft/health
```

If running in Docker or Unraid, expose/map port `8095` or whatever you set as `MINECRAFT_API_PORT`.

## Discord Commands

- `/linkminecraft` generates a short code for a user.
- `/minecraftprofile` shows the linked account and today's Minecraft XP.
- `/unlinkminecraft` removes the user's link.
- `/minecraftxpcap` sets the daily Minecraft XP cap.
- `/minecraftannounce` toggles Minecraft XP announcements and optionally sets a channel.

## Fabric Mod Setup

The server-side mod lives in:

```text
fabric-minecraft-sync/
```

Build it with Gradle from that folder:

```bash
gradle build
```

Place the jar from `fabric-minecraft-sync/build/libs/` into the Minecraft server `mods` folder.

The Minecraft server also needs Fabric API installed.

On first server start, the mod creates:

```text
config/labworks-level-sync.json
```

Edit it so the mod can reach the bot:

```json
{
  "apiUrl": "http://BOT_HOST:8095/minecraft/activity",
  "apiToken": "same-token-as-the-bot"
}
```

## Linking Flow

1. Run `/linkminecraft` in Discord.
2. Copy the generated code.
3. Run `/linkdiscord CODE` in Minecraft.
4. The bot stores the Minecraft UUID to Discord ID link.

## XP Rules

Defaults:

- `10` active minutes = `25 XP`
- Normal advancement = `100 XP`
- Major advancement = `300 XP`
- Boss/rare advancement = `500 XP`
- Every `1,000` blocks travelled = `20 XP`
- Every `5,000` blocks travelled = bonus `100 XP`
- Every `50` hostile mobs killed = `60 XP`
- Daily Minecraft XP cap = `1,500 XP`

The Fabric mod sends milestone events. The bot applies the daily cap, duplicate checks, and then awards XP using the existing Discord leveling function.
