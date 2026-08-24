"""One-time migration: adds the upgrade_procedure, restore_procedure, and
last_scheduled_backup_at columns to the existing monitored_app table. Same
reasoning as migrate_backup_columns.py - SQLModel.metadata.create_all()
only creates tables that don't exist yet, never adds columns to a table
that already exists, so this has to run explicitly before the app starts
serving requests.

last_scheduled_backup_at is backfilled from the existing last_backup_at
on the row it's added to (not left NULL) - it's a brand new column with
nothing recorded under it yet, and leaving it empty would make every
scheduled app look overdue the moment this deploys, triggering an
unplanned mass backup of everything within the next scheduler tick.
Approximating it from the last known backup of any kind is a reasonable
starting point; from here on, only genuinely scheduled runs update it.

Safe to run more than once.
"""
import sqlite3
import sys

TEXT_COLUMNS = ["upgrade_procedure", "restore_procedure"]


def migrate(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(monitored_app)")}
        added = []

        for name in TEXT_COLUMNS:
            if name not in existing:
                conn.execute(f"ALTER TABLE monitored_app ADD COLUMN {name} TEXT")
                added.append(name)

        if "last_scheduled_backup_at" not in existing:
            conn.execute("ALTER TABLE monitored_app ADD COLUMN last_scheduled_backup_at DATETIME")
            conn.execute(
                "UPDATE monitored_app SET last_scheduled_backup_at = last_backup_at "
                "WHERE last_backup_at IS NOT NULL"
            )
            added.append("last_scheduled_backup_at (backfilled from last_backup_at)")

        conn.commit()
        if added:
            print(f"Added columns to monitored_app: {', '.join(added)}")
        else:
            print("monitored_app already has all procedure/scheduling columns - nothing to do")
    finally:
        conn.close()


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "/var/lib/puma/appmonitor.db"
    migrate(db_path)
