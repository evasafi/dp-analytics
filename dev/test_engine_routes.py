import getpass
import re
from decimal import Decimal
from urllib.parse import quote

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.auth import hash_password
from app.db import engine
import app.main as main
from app.main import app

PW = "throwaway-password-1"
owner = getpass.getpass("postgres password: ")
admin = create_engine(f"postgresql+psycopg://postgres:{quote(owner, safe='')}@localhost:5432/dp_analytics")
USERS = {"engine_a": "analyst", "engine_b": "analyst", "engine_f": "field_agent", "engine_x": "admin"}
NAMES = list(USERS)


def cleanup(c):
    sub = "(SELECT id FROM users WHERE username = ANY(:n))"
    c.execute(text(f"DELETE FROM results WHERE user_id IN {sub}"), {"n": NAMES})
    c.execute(text(f"DELETE FROM audit_log WHERE user_id IN {sub}"), {"n": NAMES})
    c.execute(text("DELETE FROM users WHERE username = ANY(:n)"), {"n": NAMES})
    c.execute(text("DELETE FROM datasets WHERE name = 'engine_routes_test'"))


def state():
    with engine.connect() as c:
        return (c.execute(text("SELECT epsilon_spent FROM datasets WHERE id=:d"), {"d": DS}).scalar_one(),
                c.execute(text("SELECT count(*) FROM audit_log")).scalar_one(),
                c.execute(text("SELECT count(*) FROM results")).scalar_one())


fails = []


def check(label, ok):
    print(("OK   " if ok else "FAIL ") + label)
    if not ok:
        fails.append(label)


def csrf_from(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def client_for(u):
    cl = TestClient(app, base_url="http://127.0.0.1:8000")
    tok = csrf_from(cl.get("/login").text)
    code = cl.post("/login", data={"username": u, "password": PW, "csrf": tok},
                   follow_redirects=False).status_code
    assert code == 303, f"login {u} failed: {code}"
    return cl, tok


with admin.begin() as c:
    cleanup(c)
    c.execute(text("INSERT INTO datasets (name, epsilon_total) VALUES ('engine_routes_test', 1.0)"))
    DS = c.execute(text("SELECT id FROM datasets WHERE name='engine_routes_test'")).scalar_one()
    for u, role in USERS.items():
        c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r)"),
                  {"u": u, "h": hash_password(PW), "r": role})

try:
    a, ta = client_for("engine_a")
    b, tb = client_for("engine_b")
    f, tf = client_for("engine_f")
    x, tx = client_for("engine_x")
    anon = TestClient(app, base_url="http://127.0.0.1:8000")
    main.BENEFICIARY_OVERRIDE_ID = DS
    def real1():
        with engine.connect() as c:
            return c.execute(text("SELECT epsilon_spent FROM datasets WHERE id=1")).scalar_one()
    real_before = real1()
    base = state()
    form = {"measure": "count", "group_by": ["region"]}

    r = a.post("/engine/preview", data={**form, "csrf": ta})
    check("preview: 200, shows the cost, spends and writes nothing",
          r.status_code == 200 and "0.1" in r.text and state() == base)

    r = a.post("/engine/run", data={**form, "csrf": ta}, follow_redirects=False)
    loc = r.headers.get("location", "")
    after_run = state()
    check("run: 303 to a result page, charged 0.1, one audit row, one result",
          r.status_code == 303 and loc.startswith("/engine/result/")
          and after_run[0] - base[0] == Decimal("0.1") and after_run[1] - base[1] == 1
          and after_run[2] - base[2] == 1)
    rid_url = loc
    check("beneficiary result page is not labelled as needs", "Counts of needs" not in a.get(loc).text)

    r1, r2 = a.get(rid_url), a.get(rid_url)
    check("result page: 200 twice, shows regions, refresh spends nothing",
          r1.status_code == 200 and r2.status_code == 200 and "region_8" in r1.text
          and state() == after_run)

    r = a.get(rid_url + "/csv")
    check("csv: 200, text/csv, header plus 8 rows, spends nothing",
          r.status_code == 200 and "text/csv" in r.headers["content-type"]
          and len(r.text.strip().splitlines()) == 9 and state() == after_run)

    check("other analyst: 404 on someone else's result and csv",
          b.get(rid_url).status_code == 404 and b.get(rid_url + "/csv").status_code == 404)

    refused = []
    for who, cl, tok in (("field", f, tf), ("admin", x, tx)):
        refused += [cl.get(rid_url).status_code, cl.get(rid_url + "/csv").status_code,
                    cl.post("/engine/preview", data={**form, "csrf": tok}).status_code,
                    cl.post("/engine/run", data={**form, "csrf": tok}).status_code]
    check("field agent and admin refused on every engine route (all 403): " + str(refused),
          all(s == 403 for s in refused) and state() == after_run)

    check("anonymous: 401", anon.get(rid_url).status_code == 401)

    bad = [{"measure": "count", "group_by": ["password_hash"]},
           {"measure": "count", "group_by": ["region", "household_size", "transfer_band"]},
           {"measure": "count", "f_region": ["region_99"]},
           {"measure": "median"},
           {"measure": "count", "f_household_size": ["abc"]}]
    codes = []
    for d in bad:
        codes.append(a.post("/engine/preview", data={**d, "csrf": ta}).status_code)
        codes.append(a.post("/engine/run", data={**d, "csrf": ta}).status_code)
    check("tampered forms: all 400, nothing charged: " + str(codes),
          all(s == 400 for s in codes) and state() == after_run)

    r = a.post("/engine/run", data={**form, "csrf": "forged"})
    check("forged CSRF on run: 403, nothing charged", r.status_code == 403 and state() == after_run)
    check("real beneficiary budget untouched by the whole run", real1() == real_before)
finally:
    with admin.begin() as c:
        cleanup(c)
    main.BENEFICIARY_OVERRIDE_ID = None
    print("throwaway accounts and their audit rows and results removed")

assert not fails, f"failed: {fails}"
print("PASS")
