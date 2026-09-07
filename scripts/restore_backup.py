"""Validate and restore a Labworks SQLite backup.

Run this while the bot container is stopped. The command creates a copy of the
current database before replacing it, and only accepts files in data/backups.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import time
from pathlib import Path


def validate(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"users", "guild_settings", "bot_meta"}
        if not required.issubset(tables):
            raise ValueError(f"Missing expected tables: {', '.join(sorted(required - tables))}")
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise ValueError(f"SQLite integrity check failed: {result}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore a validated Labworks bot backup")
    parser.add_argument("backup", type=Path, help="Backup filename from the backups directory")
    parser.add_argument("--database", type=Path, default=Path("/data/levels.db"))
    parser.add_argument("--apply", action="store_true", help="Actually replace the database")
    args = parser.parse_args()

    backup = args.backup.resolve()
    backup_dir = (args.database.parent / "backups").resolve()
    if backup.parent != backup_dir:
        raise SystemExit(f"Backup must be inside {backup_dir}")
    if not backup.is_file():
        raise SystemExit(f"Backup does not exist: {backup}")
    validate(backup)
    print(f"Validated backup: {backup}")
    if not args.apply:
        print("Dry run only. Re-run with --apply while the bot is stopped to restore it.")
        return

    database = args.database.resolve()
    if database.exists():
        safety = database.with_name(f"{database.name}.before-restore-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
        shutil.copy2(database, safety)
        print(f"Saved current database as {safety}")
    temporary = database.with_suffix(".restore.tmp")
    shutil.copy2(backup, temporary)
    temporary.replace(database)
    print(f"Restored {database}")


if __name__ == "__main__":
    main()
