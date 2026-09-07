# Discord bot audit — 5 September 2026

**Status: audited changes deployed and verified on Unraid on 6 September 2026.**

Reviewed the Python bot, SQLite workflows, commands and admin UI, rank cards, background tasks, Docker build, dependency declarations, import utility, and the retired integration. This is a code audit with local regression testing, not a live Discord acceptance test or dependency-vulnerability scan.

## Changes made

| Area | Before | Local change |
| --- | --- | --- |
| Obsolete integration | HTTP activity API, linking/XP commands, environment settings, database setup, and Fabric mod code remained active. | Removed the API and five commands, configuration code, 12 Fabric files, setup guide, exposed port, obsolete ignore rules, and direct `aiohttp` requirement. Discord.py still uses aiohttp internally. Existing database tables are preserved but unused. |
| Concurrent XP | Chat, voice, and salary callbacks could read the same XP balance and overwrite one another. | Serialize XP read/modify/write operations with an asyncio lock; keep Discord network calls outside it. |
| Chat cooldown | The cooldown was saved after XP and Discord announcements, allowing concurrent messages through. | Claim cooldown with a conditional SQL update before awarding XP or making announcement calls. Twelve simultaneous messages produce one XP award while all messages are counted. |
| Gifts and rebirth | Concurrent requests could both pass eligibility checks; `INSERT OR REPLACE` did not replace boosts because the table had no unique key. | Serialize gifts/rebirth/stat edits with XP updates; remove prior recipient boosts before inserting a gift. Select the strongest existing boost during XP calculation. |
| Discord failures | A failed level-role update could prevent earned progress from being saved; failed announcements could skip cooldown persistence. | Save progress first, log Discord HTTP failures, and continue. A failed birthday send no longer stops the birthday loop. |
| Mention abuse | Custom level-up text could ping roles, everyone, or unrelated users. | Disable everyone/role mentions by default; custom level notices can mention only the player earning the level. Limit newly saved custom text to 1,000 characters and rendered notices to 2,000. |
| Admin permission changes | Private admin menus checked permissions only when opened. | Recheck administrator permission on every admin view interaction and modal submission, including after permission is revoked. |
| Invalid configuration | NaN/infinity, negative salaries, impossible levels, and unbounded rebirth edits could enter through forms. | Multipliers: finite 1–100; level edits: 1–200; reward levels: 2–200; role salaries: 0–100,000; rebirth edits: 0–10,000. Existing stored values are not silently rewritten. |
| Command handling | Guild-dependent commands failed in DMs; unexpected errors only printed to stdout; `/rank` could miss its initial response deadline. | Reject DM slash commands clearly, send a generic ephemeral error response, and defer `/rank` before downloads/rendering. |
| Shutdown | Background writers could continue after the shared database closed; failed Discord shutdown announcements could prevent cleanup. | Cancel and await background loops before closing SQLite, with cleanup in `finally`. |
| Testability | Importing the main module attempted to start the production client. | Add a main guard; tests import code without connecting to Discord. |
| Progress and retention | Rebirth made all-time ranking depend on the current level and current-level XP; the first backfill copied only the current-level remainder. | Add lifetime XP, preserve it through rebirth, reconstruct legacy no-rebirth totals from level state, and rank “All-Time XP” from the lifetime total. |
| Scheduled rewards | Restarting in the same hour could pay hourly salaries twice; midnight-only resets could miss a week/month during downtime. | Add idempotent salary run records, stale-run retry handling, startup/15-minute period reconciliation, and explicit manual salary runs. |
| Engagement | There was no durable achievement or weekly challenge state. | Add seeded achievements, user unlock records, rotating weekly challenges, progress tracking, XP rewards, and `/achievements`/`/challenge`. |
| Recovery | There was no in-app backup schedule or validated restore helper. | Add startup and six-hour SQLite backups with retention, `/backup`, and `scripts/restore_backup.py` with integrity validation and pre-restore copies. |
| Operations | XP events were only visible through the configured suspicious-activity channel. | Add an activity ledger and administrator-only `/activity` summary of recent sources, totals, and events. |

Primary code changes are in [src/main.py](src/main.py). Regression coverage is in [tests/test_bot.py](tests/test_bot.py). The original local source remains in the ignored `.local/audit-baseline/` directory for comparison; it is not part of the Docker image or project source to deploy.

## Remaining findings, in priority order

| Priority | Finding and impact | Recommended next work |
| --- | --- | --- |
| High | Shared SQLite connection and transaction ownership remain broader than the XP lock. Other callbacks can commit a partially completed workflow, and process cancellation can occur between cooldown/gift statements. The new lock protects concurrency within this bot process, not multiple bot processes or crash atomicity. | Introduce a database service with explicit transaction boundaries and a consistent writer policy. Add failure-injection tests before migrating. Keep one production bot process. |
| High | `DATA_DIR` is `/app/data` in Docker, while the verified persistent mount is `/data`. Custom rank backgrounds therefore live in the container layer and may disappear on recreation. | Before an approved deployment, back up existing `/app/data/rank_cards`, move them into the persistent mount, and change the data path as one coordinated migration. Do not simply recreate the container first. |
| Medium | `reset_stats_loop` resets only when running at midnight on Monday/the first day of a month. Downtime over that boundary retains last period's totals. Hourly salary scheduling also starts again on process startup. | Persist period IDs and last successful salary period; reconcile on startup and prevent accidental duplicate scheduled payouts. |
| Medium | Pillow rendering and image conversion run on the event loop; compressed file size alone does not bound decoded image memory. Image upload does not defer its initial response. | Add pixel limits, bounded worker execution, upload deferral, and atomic file writes. Test corrupted/oversized images and concurrent `/rank` requests. |
| Medium | Startup syncs only the hard-coded test guild. Removed commands registered in other scopes will stay visible until those scopes are synced. | On approved deployment, inventory existing registration scopes and sync them deliberately. Make the target guild configurable. |
| Medium | Some remaining broad `except` blocks label database/Discord failures as invalid user input; some background task errors can stop a loop. Existing invalid stored multiplier values are not repaired by input validation. | Separate validation from persistence/network errors, validate stored configuration on startup, and add task failure reporting. |
| Medium | `import_data.py` uses `INSERT OR REPLACE`, resets rebirth/message counts and can discard existing profile fields. It also treats imported global points as current-level XP. | Redesign as an explicit migration with schema validation, backups, dry-run, and clearly defined XP conversion before using it on established data. It was not run. |
| Medium | Dependencies and Docker base tags are unpinned; clean builds can differ from production. | Capture the production dependency versions, then create and test a reproducible lock and image digest. No dependency upgrade was deployed in this audit. |
| Low | Legacy users with rebirth history cannot have their pre-migration lifetime total reconstructed from the old schema. Global rankings count guild membership rows rather than unique people. | Preserve existing rebirth counters, use the new activity ledger for future history, and recover older rebirth totals only from an external backup if needed. |
| Low | The bot remains a large module; role reconciliation is best effort and may need `/sync_roles` after Discord failures. | Split database, progression, UI, rendering, and scheduling into modules; add a retry mechanism for failed role reconciliation. |

## Verification

- Tests use temporary SQLite files, mocked Discord operations, and no production credentials.
- Concurrent XP, chat cooldown, gifts, rebirth, Discord failures, permissions, shutdown, and rank response timing are covered.
- Fresh startup creates no retired integration tables or API listener. Compatibility testing adds legacy tables/columns and confirms startup preserves XP, profile data, and quiet-event settings.
- **22 regression tests passed.** Command: `python -m unittest discover -s tests -v`. Detailed local output: `.local/audit-tests.txt`.
- The suite covers lifetime XP, achievements, weekly challenges, backup/restore, salary idempotency, and activity logging in addition to the earlier concurrency and failure tests.
- Two regression tests were also run against the saved original source with client startup disabled: both failed as expected. Thirty concurrent 10-XP awards retained only 10 total XP instead of 300; a simultaneous chat burst bypassed the cooldown. Both tests pass against the audited source. Details: `.local/audit-baseline-regressions.txt`.
- Local runtime: Python 3.12; discord.py 2.7.1; aiosqlite 0.22.1; python-dotenv 1.2.3; Pillow 12.3.0. These are the test environment versions, not a claim about production versions.
- The 6 September deployment was verified with a running container, Discord Gateway connection, matching source hash, persistent `/data` mount, rollback image, and startup/scheduled backups. The live database contains the `lifetime_xp_backfill_v2` migration marker and its all-time values now follow cumulative level progress.

The command checks and mention controls use the documented [discord.py interaction APIs](https://discordpy.readthedocs.io/en/stable/interactions/api.html) and [AllowedMentions API](https://discordpy.readthedocs.io/en/stable/api.html#discord.AllowedMentions).
