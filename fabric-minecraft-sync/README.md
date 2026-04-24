# Labworks Minecraft Level Sync

Server-side Fabric mod for sending Minecraft activity milestones to the Labworks Discord leveling bot.

## Target

- Minecraft `1.21.1`
- Fabric Loader `0.16.x`
- Java `21`
- Server-side only

## Build

From this folder:

```bash
./gradlew build
```

The mod jar will be created under:

```text
build/libs/
```

Place the built jar into your Minecraft server `mods` folder alongside Fabric API.

## Config

On first server start the mod writes:

```text
config/labworks-level-sync.json
```

Set:

```json
{
  "apiUrl": "http://YOUR_BOT_HOST:8095/minecraft/activity",
  "apiToken": "same-token-as-the-bot"
}
```

The token must match the bot environment variable:

```text
MINECRAFT_API_TOKEN=same-token-as-the-mod
```

## Linking Flow

1. In Discord, run `/linkminecraft`.
2. Discord gives the user a short code.
3. In Minecraft, run `/linkdiscord CODE`.
4. The mod posts the UUID, player name, and code to the bot.
5. The bot stores the UUID to Discord ID link.

## XP Sources

The mod sends batched milestone events only:

- Active playtime every configured active-minute milestone.
- Advancements once per advancement.
- Distance milestones every configured block threshold.
- Distance bonus milestones every configured bonus threshold.
- Hostile mob kill milestones every configured kill threshold.

The bot applies the daily cap and duplicate checks before awarding Discord XP.

## Bot Environment

Required:

```text
MINECRAFT_API_TOKEN=change-this-to-a-long-secret
```

Optional:

```text
MINECRAFT_API_HOST=0.0.0.0
MINECRAFT_API_PORT=8095
MINECRAFT_DAILY_XP_CAP=1500
MINECRAFT_TARGET_GUILD_ID=1041046184552308776
MINECRAFT_ANNOUNCE_ENABLED=false
```

If `MINECRAFT_TARGET_GUILD_ID` is not set, the bot awards XP in the first guild where it can find the linked Discord member.

## Discord Commands

- `/linkminecraft`
- `/minecraftprofile`
- `/unlinkminecraft`
- `/minecraftxpcap`
- `/minecraftannounce`

## Unraid Notes

Expose or map the bot container port configured by `MINECRAFT_API_PORT`, default `8095`.

The Minecraft server must be able to reach:

```text
http://BOT_HOST:8095/minecraft/activity
```
