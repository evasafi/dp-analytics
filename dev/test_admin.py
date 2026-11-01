import getpass
import re
from urllib.parse import quote

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.auth import hash_password
from app.db import engine
from app.main import app

PW = "throwaway-password-1"
owner = getpass.getpass("postgres password: ")
admin_db = create_engine(f"postgresql+psycopg://postgres:{quote(owner, safe='')}@localhost:5432/dp_analytics")
USERS = {"ad_a": "admin", "ad_n": "analyst", "ad_f": "field_agent"}
NAMES = list(USERS)
fails = []


def check(label, ok):
    print(("OK   " if ok else "FAIL ") + label)
    if not ok:
        fails.append(label)


def csrf_from(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def spent():
    with engine.connect() as c:
        return c.execute(text("SELECT epsilon_spent FROM datasets WHERE id = 1")).scalar_one()


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


def make_users():
    with admin_db.begin() as c:
        cleanup(c)
        for u, role in USERS.items():
            c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r)"),
                      {"u": u, "h": hash_password(PW), "r": role})
make_users()
try:
    admin, atoken = client_for("ad_a")
    analyst, _ = client_for("ad_n")
    field, _ = client_for("ad_f")
    anon = TestClient(app, base_url="http://127.0.0.1:8000")
    r = admin.get("/admin")
    check("admin GET /admin: 200", r.status_code == 200)
    check("page shows budget and audit log", "Audit log" in r.text and "Privacy budget" in r.text)
    check("page shows only hash prefixes, no raw values (needs at least one audit row in the database)",
          re.search(r"<td>[0-9a-f]{12}</td>", r.text) is not None)
    check("analyst and field_agent refused on /admin (403, 403)",
          analyst.get("/admin").status_code == 403 and field.get("/admin").status_code == 403)
    check("anonymous GET /admin: 401", anon.get("/admin").status_code == 401)
    before = spent()
    atoken = csrf_from(admin.get("/admin").text)
    codes = [admin.get("/analyst").status_code]
    for path, data in [("/engine/preview", {"measure": "count", "group_by": ["region"]}),
                       ("/engine/run", {"measure": "count", "group_by": ["region"]}),
                       ("/engine/run", {"measure": "mean", "group_by": ["region"]}),
                       ("/engine/needs/preview", {"grouping": "category"}),
                       ("/engine/needs/run", {"grouping": "category"}),
                       ("/field/submit", {"region": "region_1", "household_size": 2, "monthly_transfer": "50"})]:
        codes.append(admin.post(path, data={**data, "csrf": atoken}).status_code)
    check("admin refused on analyst, engine, needs and field routes (all 403): " + str(codes),
          all(c == 403 for c in codes))
    check("budget of dataset 1 unchanged by refused requests", spent() == before)
finally:
    with admin_db.begin() as c:
        cleanup(c)
    print("throwaway accounts removed")
assert not fails, f"failed: {fails}"
print("PASS")