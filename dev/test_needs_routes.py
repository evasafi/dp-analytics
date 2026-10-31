import getpass
import re
from decimal import Decimal
from urllib.parse import quote

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

import app.main as main
import app.needs_engine as ne
from app.auth import hash_password
from app.db import engine
from app.main import app

PW = "throwaway-password-1"
owner = getpass.getpass("postgres password: ")
admin = create_engine(f"postgresql+psycopg://postgres:{quote(owner, safe='')}@localhost:5432/dp_analytics")
USERS = {"nr_a": "analyst", "nr_b": "analyst", "nr_f": "field_agent", "nr_x": "admin"}
NAMES = list(USERS)
DSNAME = "needs_route_test"


def cleanup(c):
    sub = "(SELECT id FROM users WHERE username = ANY(:n))"
    ds = f"(SELECT id FROM datasets WHERE name = '{DSNAME}')"
    c.execute(text(f"DELETE FROM results WHERE user_id IN {sub} OR dataset_id IN {ds}"), {"n": NAMES})
    c.execute(text(f"DELETE FROM audit_log WHERE user_id IN {sub} OR dataset_id IN {ds}"), {"n": NAMES})
    c.execute(text("DELETE FROM users WHERE username = ANY(:n)"), {"n": NAMES})
    c.execute(text("DELETE FROM datasets WHERE name = :s"), {"s": DSNAME})


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
    c.execute(text("INSERT INTO datasets (name, epsilon_total) VALUES (:s, 1.0)"), {"s": DSNAME})
    DS = c.execute(text("SELECT id FROM datasets WHERE name = :s"), {"s": DSNAME}).scalar_one()
    for u, role in USERS.items():
        c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r)"),
                  {"u": u, "h": hash_password(PW), "r": role})


def state():
    with engine.connect() as c:
        q = lambda s, **p: c.execute(text(s), p).scalar_one()
        return (q("SELECT epsilon_spent FROM datasets WHERE id=:d", d=DS),
                q("SELECT count(*) FROM audit_log WHERE dataset_id=:d", d=DS),
                q("SELECT count(*) FROM results WHERE dataset_id=:d", d=DS),
                q("SELECT epsilon_spent FROM datasets WHERE id=1"),
                q("SELECT epsilon_spent FROM datasets WHERE id=7"))


ne.SOURCES[DS] = "needs_synth"
main.NEEDS_DATASET_ID = DS
try:
    a, ta = client_for("nr_a")
    b, tb = client_for("nr_b")
    f, tf = client_for("nr_f")
    x, tx = client_for("nr_x")
    anon = TestClient(app, base_url="http://127.0.0.1:8000")
    base = state()

    r = a.get("/analyst")
    check("analyst page: 200, shows the needs form and its budget",
          r.status_code == 200 and "Needs (synthetic data)" in r.text
          and "/engine/needs/preview" in r.text and "1.0</strong> remaining of 1.0" in r.text)

    groupings = ["category", "province", "status", "category_status"]
    codes = []
    for g in groupings:
        r = a.post("/engine/needs/preview", data={"grouping": g, "csrf": ta})
        codes.append((r.status_code, "0.1" in r.text, 'action="/engine/needs/run"' in r.text))
    check("needs preview: 200 for all four groupings, shows cost, posts to the needs run route: " + str(codes),
          all(c == (200, True, True) for c in codes) and state() == base)

    r = a.post("/engine/preview", data={"measure": "count", "group_by": ["region"], "csrf": ta})
    check("beneficiary preview still posts to /engine/run and spends nothing",
          r.status_code == 200 and 'action="/engine/run"' in r.text and state() == base)

    bad = ["password_hash", "province_status", "", "category,status", "Category"]
    codes = []
    for g in bad:
        codes.append(a.post("/engine/needs/preview", data={"grouping": g, "csrf": ta}).status_code)
        codes.append(a.post("/engine/needs/run", data={"grouping": g, "csrf": ta}).status_code)
    check("bad grouping keys: all 400 (or 422 for empty), nothing charged: " + str(codes),
          all(s in (400, 422) for s in codes) and state() == base)

    r = a.post("/engine/needs/run", data={"grouping": "category", "csrf": ta}, follow_redirects=False)
    loc = r.headers.get("location", "")
    s1 = state()
    check("needs run: 303 to a result page, throwaway charged 0.1, one audit row, one result, datasets 1 and 7 untouched",
          r.status_code == 303 and loc.startswith("/engine/result/")
          and s1[0] - base[0] == Decimal("0.1") and s1[1] - base[1] == 1 and s1[2] - base[2] == 1
          and s1[3] == base[3] and s1[4] == base[4])
    with engine.connect() as c:
        qt = c.execute(text("SELECT query_type FROM audit_log WHERE dataset_id=:d"), {"d": DS}).scalar_one()
    check("audit row has query_type needs_count", qt == "needs_count")

    r = a.get(loc)
    check("result page: 200, shows category codes, refresh spends nothing",
          r.status_code == 200 and "baby_child" in r.text and "street_animals" in r.text and state() == s1)

    r = a.post("/engine/needs/run", data={"grouping": "province", "csrf": ta}, follow_redirects=False)
    loc2 = r.headers.get("location", "")
    s2 = state()
    r = a.get(loc2 + "/csv")
    lines = r.text.strip().splitlines()
    check("province csv: 200, text/csv, header plus 81 rows, includes Istanbul and Ordu",
          r.status_code == 200 and "text/csv" in r.headers["content-type"] and len(lines) == 82
          and "Istanbul" in r.text and "Ordu" in r.text and state() == s2)

    r = a.post("/engine/needs/run", data={"grouping": "category_status", "csrf": ta}, follow_redirects=False)
    r = a.get(r.headers["location"] + "/csv")
    check("category x status csv: header plus 50 rows", len(r.text.strip().splitlines()) == 51)

    check("other analyst: 404 on someone else's needs result and csv",
          b.get(loc).status_code == 404 and b.get(loc + "/csv").status_code == 404)

    s3 = state()
    refused = []
    for cl, tok in ((f, tf), (x, tx)):
        for route in ("/engine/needs/preview", "/engine/needs/run"):
            refused.append(cl.post(route, data={"grouping": "category", "csrf": tok}).status_code)
    check("field agent and admin refused on both needs routes (all 403): " + str(refused),
          all(s == 403 for s in refused) and state() == s3)

    anon_codes = [anon.post(rt, data={"grouping": "category", "csrf": "x"}).status_code
                  for rt in ("/engine/needs/preview", "/engine/needs/run")]
    check("anonymous POST on both needs routes: 401: " + str(anon_codes),
          all(s == 401 for s in anon_codes) and state() == s3)

    r1 = a.post("/engine/needs/run", data={"grouping": "category", "csrf": "forged"})
    r2 = a.post("/engine/needs/preview", data={"grouping": "category", "csrf": "forged"})
    check("forged CSRF on both needs routes: 403, nothing charged",
          r1.status_code == 403 and r2.status_code == 403 and state() == s3)
finally:
    main.NEEDS_DATASET_ID = 7
    ne.SOURCES.pop(DS, None)
    with admin.begin() as c:
        cleanup(c)
    print("throwaway accounts, dataset, audit rows and results removed")

assert not fails, f"failed: {fails}"
print("PASS")