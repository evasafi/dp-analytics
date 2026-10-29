import getpass
import re

from sqlalchemy import text

from app.auth import hash_password
from app.db import engine

username = input("verifier username (3 to 30 letters, digits, . _ -): ").strip()
if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,29}$", username):
    raise SystemExit("invalid username; nothing was saved")
pw1 = getpass.getpass("password (12+ characters): ")
pw2 = getpass.getpass("repeat it: ")
if pw1 != pw2 or len(pw1) < 12:
    raise SystemExit("passwords differ or are too short; nothing was saved")
with engine.begin() as c:
    if c.execute(text("SELECT 1 FROM users WHERE username = :u"), {"u": username}).scalar():
        raise SystemExit("that username already exists; nothing was saved")
    c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, 'verifier')"),
              {"u": username, "h": hash_password(pw1)})
print("created verifier", username)