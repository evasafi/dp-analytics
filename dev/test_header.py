"""Board header: signed-in visitors see their name, everyone else sees the sign-in links, and the board sets no cookie.

Run from the project folder:  python -m dev.test_header
Only reads the database (it picks an existing user); it writes nothing.
"""
import sys

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.auth import COOKIE, make_session
from app.db import engine
from app.main import app

fails = []
count = 0


def check(name, ok, extra=""):
    global count
    count += 1
    if not ok:
        fails.append(name)
        print("FAIL", name, extra)


def board(cookie_value=None):
    c = TestClient(app)
    headers = {"Cookie": f"{COOKIE}={cookie_value}"} if cookie_value else {}
    r = c.get("/board", headers=headers)
    return r


with engine.connect() as conn:
    user = conn.execute(text("SELECT id, username, role FROM users ORDER BY id LIMIT 1")).one()

r = board()
check("signed out: page loads", r.status_code == 200, r.status_code)
check("signed out: shows sign-in link", 'href="/login"' in r.text and "Create an account" in r.text)
check("signed out: no name shown", "Signed in as" not in r.text)
check("signed out: no cookie set", "set-cookie" not in r.headers)

r = board(make_session(user.id))
check("signed in: page loads", r.status_code == 200, r.status_code)
check("signed in: shows the user name", f"Signed in as <strong>{user.username}</strong>" in r.text)
check("signed in: shows the role", f"({user.role})" in r.text)
check("signed in: links to my page", 'href="/me"' in r.text)
check("signed in: no sign-in link", 'href="/login"' not in r.text)
check("signed in: no cookie set", "set-cookie" not in r.headers)

r = board("not-a-real-token")
check("garbage session: treated as visitor", r.status_code == 200 and 'href="/login"' in r.text, r.status_code)

r = board(make_session(999999999))
check("unknown user id: treated as visitor", r.status_code == 200 and 'href="/login"' in r.text, r.status_code)

if fails:
    print(f"{len(fails)} of {count} checks FAILED")
    sys.exit(1)
print(f"header: all {count} checks passed")
