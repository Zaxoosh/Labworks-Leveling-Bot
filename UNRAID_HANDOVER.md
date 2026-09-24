# Labworks Discord Bot — project and remote access

Latest deployment verified on 2026-09-24: `labworkslevelbot:20260924-remove-sus-ping` is running on Unraid and connected to Discord.

## Source

Imported from `C:\Users\zaxoo\Desktop\Labworks Level Bot`, the repository used by the previous Codex task **Labworks Levelling Bot** (`019dbc61-e9a4-7970-aafc-e52861e003af`).

- Upstream: https://github.com/Zaxoosh/Labworks-Leveling-Bot.git
- Original checkout commit: `fd64d704c5d0e0e0330e5b613cd2ebed039ffc05` (XP Event Fix, 2026-04-28).
- Originally imported 20 tracked files plus the Dockerfile and CSV import utility. The subsequent audit removed the obsolete Fabric integration and its setup guide at the user's request.
- Current project includes the Discord bot, rank-card fonts, Dockerfile, CSV import utility, and regression tests.
- The deployed version adds lifetime XP, legacy no-rebirth backfill, achievements, weekly challenges, idempotent salary runs, SQLite backups, restore tooling, and `/activity`.
- GitHub remote: https://github.com/Zaxoosh/Labworks-Leveling-Bot.git (`origin`, branch `main`).
- Credentials, databases, CSV exports, caches, and build outputs were excluded.

## Verified server

- Host: `FribbetsBrain`, `192.168.0.100`, Unraid `7.2.4`.
- SSH account: `root`; authenticated with the dedicated key at `C:\Users\zaxoo\.ssh\labworks_unraid_codex`.
- Container: `labworkslevelbot`, running with restart count 0 at verification on 2026-09-24.
- Image: `labworkslevelbot:20260924-remove-sus-ping`.
- Image ID: `sha256:d978aff6f02619f7bd4c30b9504e50a5ee691b0584b7f7c5eb6fceb5fc373985`.
- Command: `python src/main.py`, working directory `/app`.
- Persistent bind mount: `/mnt/user/appdata/labworkslevelbot` → `/data`, read/write.
- API port: none; the retired HTTP integration is not exposed.
- Restart policy: `no` (preserved from the existing container configuration).
- The `/data` mount contains the migrated database, rank cards, and automatic backups.
- No host ports are published.
- Startup logs show `Bot Online & Synced` and a successful Discord Gateway connection. SQLite `PRAGMA integrity_check` returned `ok`.
- The deployed `src/main.py` SHA-256 is `dd1fcddb6538a41763db1b24eb8a2e8eefb9c05234f49b089da7810e4eb12335`.
- Pre-deployment data archive: `/mnt/user/appdata/labworkslevelbot-deploy/20260924-remove-sus-ping/data-backup.tar` (20,428,800 bytes, mode `0600`).
- Rollback container: `labworkslevelbot-rollback-20260924-remove-sus-ping`, retaining image `labworkslevelbot:20260907-sapphire-reminders`.

The original imported `src/main.py` and `requirements.txt` matched the running container. These are the production baseline hashes, not hashes of the locally audited files:

```text
58ed7ed6094b86e33f1b9aeab668b705d100f02615488c8eab1b8a87e193b9e0  src/main.py
5e6123199d215c5a070c1921a8d5caa711bd92c7aa97978b1b475be61efe5c92  requirements.txt
```

## Connect from this PC

The private key is stored outside this repository at `C:\Users\zaxoo\.ssh\labworks_unraid_codex`; only its public key is installed on Unraid. Never copy the private key into the repository or share it.

```powershell
$botKey = 'C:\Users\zaxoo\.ssh\labworks_unraid_codex'
ssh -i $botKey -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes root@192.168.0.100 'docker ps --filter name=labworkslevelbot'
```

SSH host verification uses the existing Windows `known_hosts` entry. The default Windows SSH key is not authorized on this server; use the dedicated key above.

## Remote update capability

Verified root SSH access, Docker daemon access, and writes to the dedicated deployment directory on Unraid. The currently authorized key is the dedicated key above.

For a future bot update:

1. Inspect the current container configuration privately, preserving its Discord credentials, mount, networking, restart policy, and image rollback reference.
2. Stop the bot briefly and archive `/data`; tag and retain the current image/container for rollback.
3. Upload the source and Dockerfile to a versioned build directory, then build a versioned image on Unraid. This avoids depending on registry push credentials.
4. Recreate only `labworkslevelbot` with its existing configuration and the new image. Its source is baked into the image, so a plain restart does not deploy local edits; edits inside the container are not durable across recreation. The bot has no HTTP API or published host ports.
5. Verify startup logs, Discord Gateway connection, SQLite integrity, and persisted data. Roll back to the saved image/configuration if startup fails.

The retired integration's tables/columns in an existing database are left untouched and unused. No destructive database cleanup is required to run the new version. Test coverage verifies old schema/data can still be opened safely.

The 6 September 2026 deployment preserved the prior image as `labworkslevelbot-rollback-20260906-lifetime-xp`, saved a persistent data archive, and verified the `lifetime_xp_backfill_v2` migration. Registry push permissions were not required.

The 24 September 2026 deployment committed as `ad77801` removed the suspicious-XP alert, preserved the prior container and image for rollback, archived `/data`, and verified successful startup, Gateway connection, and SQLite integrity. The same code is pushed to GitHub `main`.

## Local development

Use `python -m pip install -r requirements.txt`, set a separate development bot's `DISCORD_TOKEN` in `src/.env`, then run `python src/main.py` from this project. Do not start a second copy using the production token. Python syntax validation passed on import; no Discord messages were sent by verification.
