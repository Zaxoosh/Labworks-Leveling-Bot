# Labworks Discord Bot — project and remote access

Remote baseline verified on 2026-09-05 before the local audit. The audited bot is deployed and verified on Unraid as `labworkslevelbot:20260906-lifetime-xp` (6 September 2026).

## Source

Imported from `C:\Users\zaxoo\Desktop\Labworks Level Bot`, the repository used by the previous Codex task **Labworks Levelling Bot** (`019dbc61-e9a4-7970-aafc-e52861e003af`).

- Upstream: https://github.com/Zaxoosh/Labworks-Leveling-Bot.git
- Original checkout commit: `fd64d704c5d0e0e0330e5b613cd2ebed039ffc05` (XP Event Fix, 2026-04-28).
- Originally imported 20 tracked files plus the Dockerfile and CSV import utility. The subsequent audit removed the obsolete Fabric integration and its setup guide at the user's request.
- Current project includes the Discord bot, rank-card fonts, Dockerfile, CSV import utility, and regression tests.
- The deployed version adds lifetime XP, legacy no-rebirth backfill, achievements, weekly challenges, idempotent salary runs, SQLite backups, restore tooling, and `/activity`.
- Original repository and this project's Git metadata were preserved; no upstream remote was assigned to this new project.
- Credentials, databases, CSV exports, caches, and build outputs were excluded.

## Verified server

- Host: `FribbetsBrain`, `192.168.0.100`, Unraid `7.2.4`.
- SSH account: `root`; authenticated successfully with the existing dedicated key.
- Container: `labworkslevelbot`, running since `2026-08-30T04:01:51Z`, restart count 0 at inspection.
- Image: `labworkslevelbot:20260906-lifetime-xp`.
- Image ID: `sha256:19d4fcde4e7fef46ed51ee61bb8a07bc6311f60415a24730ad3d8d030449f9ef`.
- Command: `python src/main.py`, working directory `/app`.
- Persistent bind mount: `/mnt/user/appdata/labworkslevelbot` → `/data`, read/write.
- API port: none; the retired HTTP integration is not exposed.
- Current restart policy: preserved from the existing container configuration.
- The `/data` mount contains the migrated database, rank cards, and automatic backups.
- Logs show a successful Discord Gateway connection after deployment.

The original imported `src/main.py` and `requirements.txt` matched the running container. These are the production baseline hashes, not hashes of the locally audited files:

```text
58ed7ed6094b86e33f1b9aeab668b705d100f02615488c8eab1b8a87e193b9e0  src/main.py
5e6123199d215c5a070c1921a8d5caa711bd92c7aa97978b1b475be61efe5c92  requirements.txt
```

## Connect from this PC

The private key remains outside this repository at `C:\Users\zaxoo\AppData\Local\Temp\opencode\unraid_ssh_key`. This is a temporary-directory dependency; preserve it in a secure durable credential location before any temp cleanup.

```powershell
$botKey = 'C:\Users\zaxoo\AppData\Local\Temp\opencode\unraid_ssh_key'
ssh -i $botKey -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes root@192.168.0.100 'docker ps --filter name=labworkslevelbot'
```

SSH host verification uses the existing Windows `known_hosts` entry. The default Windows SSH key is not authorized on this server; use the dedicated key above.

## Remote update capability

Verified root SSH access, Docker daemon access, writable bot source in the container, and a successful temporary-file create/read/delete in the bot's persistent host directory. The temporary probe was removed. No application files were changed and the container was not restarted.

For a future approved bot update:

1. After user approval, inspect the current container configuration privately, preserving its Discord credentials, mount, networking, and image rollback reference. The new bot has no HTTP API: remove the obsolete port 8095 mapping and `MINECRAFT_*` variables when preparing the replacement container.
2. Back up the persistent SQLite database consistently (SQLite backup API or a brief scoped bot stop) and preserve the current image.
3. Upload this project's source and Dockerfile to a dedicated build directory on Unraid via SCP, then build a versioned Docker image remotely. This avoids depending on registry push credentials.
4. Recreate only `labworkslevelbot` with its existing configuration and the new image. Its source is baked into the image, so a plain restart does not deploy local edits; edits inside the container are not durable across recreation.
5. Verify startup logs, Discord connection, `/healthcheck`, `/rank`, `/achievements`, `/challenge`, `/activity`, backup creation, and persisted data. There is no HTTP health endpoint in the new version. Sync commands for every guild scope where they were previously registered so obsolete integration commands disappear; startup currently syncs only the configured test guild. Roll back to the saved image/configuration if verification fails.

The retired integration's tables/columns in an existing database are left untouched and unused. No destructive database cleanup is required to run the new version. Test coverage verifies old schema/data can still be opened safely.

The 6 September 2026 deployment preserved the prior image as `labworkslevelbot-rollback-20260906-lifetime-xp`, saved a persistent data archive, and verified the `lifetime_xp_backfill_v2` migration. Registry push permissions were not required.

## Local development

Use `python -m pip install -r requirements.txt`, set a separate development bot's `DISCORD_TOKEN` in `src/.env`, then run `python src/main.py` from this project. Do not start a second copy using the production token. Python syntax validation passed on import; no Discord messages were sent by verification.
