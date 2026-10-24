import getpass

from sqlalchemy import text

from app.auth import hash_password
from app.db import engine

ACCOUNTS = [
    ("field1", "field_agent"),
    ("analyst1", "analyst"),
    ("admin1", "admin"),
]

with engine.begin() as c:
    for username, role in ACCOUNTS:
        exists = c.execute(
            text("SELECT 1 FROM users WHERE username = :u"), {"u": username}
        ).scalar()
        if exists:
            print(f"{username}: already exists, skipping")
            continue
        pw = getpass.getpass(f"password for {username} ({role}), min 12 chars: ")
        if len(pw) < 12:
            raise SystemExit("password too short; nothing was saved")
        c.execute(
            text("INSERT INTO users (username, password_hash, role) "
                 "VALUES (:u, :h, :r)"),
            {"u": username, "h": hash_password(pw), "r": role},
        )
        print(f"created {username}")

with engine.connect() as c:
    for r in c.execute(text("SELECT id, username, role FROM users ORDER BY id")):
        print(tuple(r))
