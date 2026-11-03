"""Role separation and CSRF at the web layer. Run from the project root: python -m dev.test_roles

Needs the database owner's password (hidden prompt). The owner connection is built from the app's own
DP_DATABASE_URL and the test refuses to run against a database without the financial-needs schema.
It creates throwaway accounts (prefix rl_) and removes them at the end.
"""
import getpass
import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.auth import hash_password
from app.config import settings
from app.db import engine
from app.main import app

PW = "throwaway-password-1"
admin_db = create_engine(make_url(settings.database_url).set(username="postgres", password=getpass.getpass("postgres password: ")))
USERS = {"rl_an": "analyst", "rl_ad": "admin", "rl_rq": "requester", "rl_sp": "supporter", "rl_vf": "verifier"}
NAMES = list(USERS)
# page -> the one role that may open it
OWNER_OF = {"/analyst": "analyst", "/engine/results": "analyst", "/engine/result/1": "analyst", "/engine/result/1/csv": "analyst",
            "/admin": "admin", "/verify": "verifier", "/support": "supporter", "/needs/mine": "requester"}
MUST_OPEN = {"analyst": ("/analyst", "/engine/results"), "admin": ("/admin",)}
fails = []

with admin_db.connect() as c:
    if not c.execute(text("SELECT count(*) FROM information_schema.columns "
                          "WHERE table_name = 'needs' AND column_name = 'payee_name'")).scalar_one():
        raise SystemExit("This database does not have the financial-needs schema; refusing to run.")


def check(label, ok):
    print(("OK   " if ok else "FAIL ") + label)
    if not ok:
        fails.append(label)


def csrf_from(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def spent():
    with engine.connect() as c:
        return c.execute(text("SELECT sum(epsilon_spent) FROM datasets")).scalar_one()


def client_for(u):
    cl = TestClient(app, base_url="http://127.0.0.1:8000")
    tok = csrf_from(cl.get("/login").text)
    code = cl.post("/login", data={"username": u, "password": PW, "csrf": tok}, follow_redirects=False).status_code
    assert code == 303, f"login {u} failed: {code}"
    return cl, tok


def cleanup(c):
    sub = "(SELECT id FROM users WHERE username = ANY(:n))"
    c.execute(text(f"DELETE FROM results WHERE user_id IN {sub}"), {"n": NAMES})
    c.execute(text(f"DELETE FROM audit_log WHERE user_id IN {sub}"), {"n": NAMES})
    c.execute(text("DELETE FROM users WHERE username = ANY(:n)"), {"n": NAMES})


with admin_db.begin() as c:
    cleanup(c)
    for u, role in USERS.items():
        c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r)"),
                  {"u": u, "h": hash_password(PW), "r": role})
try:
    clients = {u: client_for(u) for u in USERS}
    anon = TestClient(app, base_url="http://127.0.0.1:8000")
    before = spent()

    bad403, bad200 = [], []
    for u, role in USERS.items():
        cl, _ = clients[u]
        for path, owner in OWNER_OF.items():
            if owner == role and path not in MUST_OPEN.get(role, ()):
                continue        # the owner's own pages are checked only where they exist in the new schema
            s = cl.get(path).status_code
            if owner != role and s != 403:
                bad403.append(f"{role} {path} gave {s}")
            if path in MUST_OPEN.get(role, ()) and s != 200:
                bad200.append(f"{role} {path} gave {s}")
    check("every role is refused (403) on every page that belongs to another role"
          + ((": " + "; ".join(bad403)) if bad403 else ""), not bad403)
    check("analyst and admin can open their own pages (200)" + ((": " + "; ".join(bad200)) if bad200 else ""), not bad200)

    codes = {p: anon.get(p).status_code for p in OWNER_OF}
    check("anonymous visitors get 401 on every page: " + str(sorted(set(codes.values()))), all(s == 401 for s in codes.values()))

    page = TestClient(app, base_url="http://127.0.0.1:8000")
    r = page.post("/login", data={"username": "rl_an", "password": PW, "csrf": "forged"}, follow_redirects=False)
    check("login with a forged CSRF token: 403", r.status_code == 403)
    cl, _ = clients["rl_an"]
    r = cl.post("/logout", data={"csrf": "forged"}, follow_redirects=False)
    check("logout with a forged CSRF token: 403", r.status_code == 403)
    check("no privacy budget moved during any of this", spent() == before)
finally:
    with admin_db.begin() as c:
        cleanup(c)
    print("throwaway accounts removed")

assert not fails, f"failed: {fails}"
print("PASS")
