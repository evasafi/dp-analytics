"""Admin page: budgets, audit log, no raw results, role separation. Run: python -m dev.test_admin

Needs the database owner's password (hidden prompt). The owner connection is built from the app's own
DP_DATABASE_URL and the test refuses to run against a database without the financial-needs schema.
It creates throwaway accounts (prefix ad_) and one audit row for them, and removes both at the end.
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
USERS = {"ad_ad": "admin", "ad_an": "analyst", "ad_rq": "requester"}
NAMES = list(USERS)
FULL_HASH = "0123456789abcdef0123456789abcdef0123456789abcdef"
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
    return cl


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
    c.execute(text("INSERT INTO audit_log (user_id, dataset_id, query_type, params, epsilon_charged, result_hash) "
                   "SELECT u.id, d.id, 'admin_test', '{}'::jsonb, 0.1, :h FROM users u, datasets d "
                   "WHERE u.username = 'ad_an' AND d.name = 'needs_synthetic'"), {"h": FULL_HASH})
    names = [r[0] for r in c.execute(text("SELECT name FROM datasets ORDER BY id")).all()]
try:
    admin = client_for("ad_ad")
    analyst = client_for("ad_an")
    requester = client_for("ad_rq")
    anon = TestClient(app, base_url="http://127.0.0.1:8000")
    before = spent()

    r = admin.get("/admin")
    check("admin GET /admin: 200", r.status_code == 200)
    check("the page shows the audit log and a budget line for every dataset",
          "Audit log" in r.text and "Privacy budget" in r.text and all(n in r.text for n in names))
    check("the page shows only the 12-character hash prefix, never the full hash",
          f"<td>{FULL_HASH[:12]}</td>" in r.text and FULL_HASH not in r.text)
    check("analyst and requester are refused on /admin (403, 403)",
          analyst.get("/admin").status_code == 403 and requester.get("/admin").status_code == 403)
    check("anonymous GET /admin: 401", anon.get("/admin").status_code == 401)
    check("admin is refused on the analyst pages (403)",
          admin.get("/analyst").status_code == 403 and admin.get("/engine/results").status_code == 403)
    check("no privacy budget moved", spent() == before)
finally:
    with admin_db.begin() as c:
        cleanup(c)
    print("throwaway accounts and their audit row removed")

assert not fails, f"failed: {fails}"
print("PASS")
