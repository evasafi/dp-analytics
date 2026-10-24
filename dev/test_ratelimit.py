import re

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.auth import hash_password
from app.db import engine
from app.main import app

USER, PW = "ratelimit_test", "correct-horse-battery"

with engine.begin() as c:
    c.execute(text("DELETE FROM users WHERE username = :u"), {"u": USER}) if False else None
    exists = c.execute(text("SELECT 1 FROM users WHERE username = :u"), {"u": USER}).scalar()
    if not exists:
        c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, 'analyst')"),
                  {"u": USER, "h": hash_password(PW)})

cl = TestClient(app, base_url="http://127.0.0.1:8000")


def attempt(pw):
    token = re.search(r'name="csrf" value="([^"]+)"', cl.get("/login").text).group(1)
    return cl.post("/login", data={"username": USER, "password": pw, "csrf": token},
                   follow_redirects=False).status_code


print("1. five wrong passwords:", [attempt("wrong") for _ in range(5)], "(expect all 401)")
print("2. sixth attempt:", attempt("wrong"), "(expect 429)")
print("3. correct password during lockout:", attempt(PW), "(expect 429)")

with engine.connect() as c:
    n = c.execute(text("SELECT count(*) FROM login_failures WHERE username = :u"), {"u": USER}).scalar_one()
print("4. failures recorded:", n, "(expect 5; locked attempts are not logged)")
print("Lockout expiry is not tested here: it needs a 5 minute wait (see next step).")
