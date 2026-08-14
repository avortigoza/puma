"""One-time migration: adds the backup-related columns to the existing
monitored_app table. SQLModel.metadata.create_all() only creates tables
that don't exist yet - it never alters an existing table's columns - so
this has to run explicitly before the app starts serving requests.

Safe to run more than once: checks PRAGMA table_info() first and only
adds a column if it isn't already there. Does not touch existing rows'
data beyond giving new columns their default values.
"""
import sqlite3
import sys

COLUMNS = [
    ("backup_strategy", "VARCHAR"),
    ("backup_config", "JSON DEFAULT '{}'"),
    ("backup_schedule_enabled", "BOOLEAN DEFAULT 0"),
    ("backup_interval_minutes", "INTEGER DEFAULT 1440"),
    ("last_backup_at", "DATETIME"),
    ("last_backup_status", "VARCHAR"),
]


def migrate(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(monitored_app)")}
        added = []
        for name, coltype in COLUMNS:
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE monitored_app ADD COLUMN {name} {coltype}")
            added.append(name)
        conn.commit()
        if added:
            print(f"Added columns to monitored_app: {', '.join(added)}")
        else:
            print("monitored_app already has all backup columns - nothing to do")
    finally:
        conn.close()


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "/var/lib/puma/appmonitor.db"
    migrate(db_path)
