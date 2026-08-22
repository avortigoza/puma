"""One-time migration: adds the upgrade_procedure and restore_procedure
columns to the existing monitored_app table. Same reasoning as
migrate_backup_columns.py - SQLModel.metadata.create_all() only creates
tables that don't exist yet, never adds columns to a table that already
exists, so this has to run explicitly before the app starts serving
requests.

Safe to run more than once.
"""
import sqlite3
import sys

COLUMNS = ["upgrade_procedure", "restore_procedure"]


def migrate(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(monitored_app)")}
        added = []
        for name in COLUMNS:
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE monitored_app ADD COLUMN {name} TEXT")
            added.append(name)
        conn.commit()
        if added:
            print(f"Added columns to monitored_app: {', '.join(added)}")
        else:
            print("monitored_app already has both procedure columns - nothing to do")
    finally:
        conn.close()


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "/var/lib/puma/appmonitor.db"
    migrate(db_path)
