"""One-off script: create the first PUMA admin user.

Run once, as the puma service account so it can write the DB:
    sudo -u puma /var/www/puma/.venv/bin/python /var/www/puma/app/seed_admin.py

Safe to re-run: if the username already exists it updates the password
instead of creating a duplicate.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from passlib.context import CryptContext
from sqlmodel import Session, SQLModel, create_engine, select

from models import User

DB_PATH = os.environ.get("PUMA_DB_PATH", "/var/lib/puma/appmonitor.db")
engine = create_engine(f"sqlite:///{DB_PATH}")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def main() -> None:
    SQLModel.metadata.create_all(engine)

    username = input("Admin username [admin]: ").strip() or "admin"
    password = getpass.getpass("Admin password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords did not match.")
        sys.exit(1)
    if len(password) < 8:
        print("Use at least 8 characters.")
        sys.exit(1)

    with Session(engine) as session:
        existing = session.exec(
            select(User).where(User.username == username)
        ).first()
        if existing:
            existing.password_hash = pwd_context.hash(password)
            session.add(existing)
            session.commit()
            print(f"Updated password for existing user '{username}'.")
        else:
            user = User(username=username, password_hash=pwd_context.hash(password))
            session.add(user)
            session.commit()
            print(f"Created admin user '{username}'.")


if __name__ == "__main__":
    main()
