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
USERS = {"fd_f": "field_agent", "fd_a": "analyst"}
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
with admin_db.connect() as c:
    ids_before = set(c.execute(text("SELECT beneficiary_id FROM beneficiaries WHERE dataset_id = 1")).scalars())


def rows():
    with engine.connect() as c:
        return c.execute(text("SELECT count(*) FROM beneficiaries WHERE dataset_id = 1")).scalar_one()


def submits():
    with engine.connect() as c:
        return c.execute(text("SELECT count(*) FROM audit_log WHERE query_type = 'submit' AND user_id IN "
                              "(SELECT id FROM users WHERE username = 'fd_f')")).scalar_one()


make_users()
try:
    field, ftoken = client_for("fd_f")
    analyst, _ = client_for("fd_a")
    anon = TestClient(app, base_url="http://127.0.0.1:8000")
    token = csrf_from(field.get("/field").text)
    good = {"region": "region_8", "household_size": 3, "monthly_transfer": "120.50"}
    n0, a0 = rows(), submits()
    r = field.post("/field/submit", data={**good, "csrf": token}, follow_redirects=False)
    check("valid submit: 303 to /field?done=1", r.status_code == 303 and r.headers.get("location") == "/field?done=1")
    check("exactly one row and one audit row added", rows() - n0 == 1 and submits() - a0 == 1)
    bad = [("region_9", 3, "100"), ("region_1", 13, "100"), ("region_1", 0, "100"),
           ("region_1", 3, "501"), ("region_1", 3, "-1"), ("region_1", 3, "abc"),
           ("region_1", 3, "NaN"), ("region_1", 3, "1e999")]
    codes = [field.post("/field/submit", data={"region": a, "household_size": b, "monthly_transfer": c, "csrf": token}).status_code
             for a, b, c in bad]
    check("invalid submissions all 400: " + str(codes), all(c == 400 for c in codes))
    check("nothing saved by invalid ones", rows() - n0 == 1 and submits() - a0 == 1)
    check("forged CSRF: 403", field.post("/field/submit", data={**good, "csrf": "forged"}).status_code == 403)
    atoken = csrf_from(analyst.get("/analyst").text)
    check("analyst refused on /field and /field/submit (403, 403)",
          analyst.get("/field").status_code == 403
          and analyst.post("/field/submit", data={**good, "csrf": atoken}).status_code == 403)
    check("anonymous: GET 401, POST 401 or 403",
          anon.get("/field").status_code == 401
          and anon.post("/field/submit", data={**good, "csrf": "x"}).status_code in (401, 403))
    check("no extra rows after all refused requests", rows() - n0 == 1)
finally:
    with admin_db.begin() as c:
        ids_after = set(c.execute(text("SELECT beneficiary_id FROM beneficiaries WHERE dataset_id = 1")).scalars())
        new = sorted(ids_after - ids_before)
        if new:
            c.execute(text("DELETE FROM beneficiaries WHERE dataset_id = 1 AND beneficiary_id = ANY(:i)"), {"i": new})
        cleanup(c)
    print(f"throwaway accounts removed; {len(new)} test row(s) removed from dataset 1")
assert not fails, f"failed: {fails}"
print("PASS")