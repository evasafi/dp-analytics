"""Analyst pages on the financial schema: who may use them, the free preview, running a query, the result page and CSV,
refusals that charge nothing, and an exhausted budget.

Run from the project root as a module: python -m dev.test_analytics_routes
Needs the database owner's password (hidden prompt). The owner connection is built from the app's own
DP_DATABASE_URL and the test refuses to run against a database without the financial-needs schema.
Queries run only on throwaway datasets (names starting an_test_); the real budgets are previewed, never charged,
and the test checks that. Users have the prefix ar_.
"""
import getpass
import re
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import app.needs_engine as ne
from app.auth import hash_password
from app.config import settings
from app.main import app

PW = "throwaway-password-1"
admin = create_engine(make_url(settings.database_url).set(username="postgres", password=getpass.getpass("postgres password: ")))
EPS = settings.epsilon_per_count
USERS = {"ar_an": "analyst", "ar_an2": "analyst", "ar_req": "requester", "ar_s": "supporter", "ar_v": "verifier", "ar_ad": "admin"}
GROUPINGS = ("need_type", "province", "status", "need_type_status", "band")
fails, total = [], 0

with admin.connect() as c:
    if not c.execute(text("SELECT count(*) FROM information_schema.columns "
                          "WHERE table_name = 'needs' AND column_name = 'payee_name'")).scalar_one():
        raise SystemExit("This database does not have the financial-needs schema; refusing to run.")


def check(label, ok):
    global total
    total += 1
    print(("OK   " if ok else "FAIL ") + label)
    if not ok:
        fails.append(label)


def csrf_from(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def one(sql, **p):
    with admin.connect() as c:
        return c.execute(text(sql), p).scalar_one()


def spent(name):
    return one("SELECT epsilon_spent FROM datasets WHERE name = :n", n=name)


def audits(name):
    return one("SELECT count(*) FROM audit_log a JOIN datasets d ON d.id = a.dataset_id WHERE d.name = :n", n=name)


def cleanup():
    with admin.begin() as c:
        tests = "(SELECT id FROM datasets WHERE left(name, 8) = 'an_test_')"
        c.execute(text(f"DELETE FROM results WHERE dataset_id IN {tests}"))
        c.execute(text(f"DELETE FROM audit_log WHERE dataset_id IN {tests}"))
        c.execute(text("DELETE FROM datasets WHERE left(name, 8) = 'an_test_'"))
        c.execute(text("DELETE FROM users WHERE left(username, 3) = 'ar_'"))


def client_for(u):
    cl = TestClient(app, base_url="http://127.0.0.1:8000")
    tok = csrf_from(cl.get("/login").text)
    code = cl.post("/login", data={"username": u, "password": PW, "csrf": tok}, follow_redirects=False).status_code
    assert code == 303, f"login {u} failed: {code}"
    return cl, tok


cleanup()
with admin.begin() as c:
    for u, role in USERS.items():
        c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r)"),
                  {"u": u, "h": hash_password(PW), "r": role})
    c.execute(text("INSERT INTO datasets (name, epsilon_total) VALUES ('an_test_routes', :t), ('an_test_unlisted', 1.0)"),
              {"t": Decimal(str(EPS)) * 2})
ne.SOURCES["an_test_routes"] = "synth_analytics"
REAL = {n: spent(n) for n in ("needs_synthetic", "needs_live")}
REAL_AUDITS = {n: audits(n) for n in REAL}

try:
    an, ta = client_for("ar_an")
    an2, ta2 = client_for("ar_an2")
    others = {u: client_for(u) for u in ("ar_req", "ar_s", "ar_v", "ar_ad")}
    anon = TestClient(app, base_url="http://127.0.0.1:8000")
    form = {"dataset": "an_test_routes", "grouping": "need_type"}

    # ---- who may use the pages
    codes = []
    for u, (cl, tok) in others.items():
        codes += [cl.get("/analyst").status_code, cl.post("/engine/needs/preview", data={**form, "csrf": tok}).status_code,
                  cl.post("/engine/needs/run", data={**form, "csrf": tok}).status_code]
    check("requesters, supporters, verifiers and admins are refused (403) on the analyst pages", set(codes) == {403})
    codes = [anon.get("/analyst").status_code, anon.post("/engine/needs/preview", data={**form, "csrf": "x"}).status_code,
             anon.post("/engine/needs/run", data={**form, "csrf": "x"}).status_code]
    check("anonymous visitors get 401: " + str(codes), set(codes) == {401})
    check("none of that charged anything", spent("an_test_routes") == 0 and audits("an_test_routes") == 0)

    # ---- the analyst page
    page = an.get("/analyst")
    check("the analyst page shows both budgets, both datasets, the five groupings and the cost",
          page.status_code == 200 and "needs_synthetic" in page.text and "needs_live" in page.text
          and 'value="needs_synthetic"' in page.text and 'value="needs_live"' in page.text
          and all(f'value="{g}"' in page.text for g in GROUPINGS) and f"costs {EPS}" in page.text)

    # ---- the preview is free
    previews = [an.post("/engine/needs/preview", data={"dataset": "an_test_routes", "grouping": g, "csrf": ta}) for g in GROUPINGS]
    check("a preview of each grouping: 200, shows the cost and a run button, and charges nothing",
          all(r.status_code == 200 and f"Run and charge {EPS}" in r.text and 'action="/engine/needs/run"' in r.text for r in previews)
          and spent("an_test_routes") == 0 and audits("an_test_routes") == 0)
    check("the preview names the dataset and grouping and carries them to the run form",
          'name="dataset" value="an_test_routes"' in previews[0].text and 'name="grouping" value="need_type"' in previews[0].text
          and "Cells returned: 9" in previews[0].text and "Cells returned: 81" in previews[1].text)
    real = [an.post("/engine/needs/preview", data={"dataset": d, "grouping": "status", "csrf": ta}) for d in ("needs_synthetic", "needs_live")]
    check("previews on the real datasets work and charge nothing",
          all(r.status_code == 200 for r in real) and {n: spent(n) for n in REAL} == REAL and "Synthetic needs" in real[0].text
          and "Needs on the platform" in real[1].text)

    # ---- refusals charge nothing
    bad = {"an unknown grouping": {"dataset": "an_test_routes", "grouping": "household_size"},
           "no grouping": {"dataset": "an_test_routes", "grouping": ""},
           "an unlisted dataset": {"dataset": "an_test_unlisted", "grouping": "status"},
           "a table name as dataset": {"dataset": "needs", "grouping": "status"},
           "injection as grouping": {"dataset": "an_test_routes", "grouping": "status'; DROP TABLE needs;--"}}
    for label, data in bad.items():
        r1 = an.post("/engine/needs/preview", data={**data, "csrf": ta})
        r2 = an.post("/engine/needs/run", data={**data, "csrf": ta})
        check(f"refused (400) on preview and run, nothing charged: {label}",
              r1.status_code == 400 and r2.status_code == 400 and "Nothing was charged" in r2.text
              and spent("an_test_routes") == 0 and spent("an_test_unlisted") == 0)
    r1 = an.post("/engine/needs/preview", data={**form, "csrf": "forged"})
    r2 = an.post("/engine/needs/run", data={**form, "csrf": "forged"})
    check("a forged CSRF token is refused (403) on preview and run", r1.status_code == 403 and r2.status_code == 403 and spent("an_test_routes") == 0)

    # ---- running a query
    r = an.post("/engine/needs/run", data={"dataset": "an_test_routes", "grouping": "band", "csrf": ta}, follow_redirects=False)
    loc = r.headers.get("location", "")
    check("a run: 303 to its result page, one epsilon charged, one audit row",
          r.status_code == 303 and loc.startswith("/engine/result/") and spent("an_test_routes") == Decimal(str(EPS)) and audits("an_test_routes") == 1)
    res = an.get(loc)
    check("the result page names the dataset, says counts of needs not people, explains bands, and shows 6 rows",
          res.status_code == 200 and "an_test_routes" in res.text and "Counts of needs, not people" in res.text
          and "Euro needs have no goal band" in res.text and res.text.count("<tr>") == 7)
    rid = loc.rsplit("/", 1)[1]
    check("another analyst cannot open it (404)", an2.get(loc).status_code == 404 and an2.get(f"{loc}/csv").status_code == 404)
    csv = an.get(f"{loc}/csv")
    check("the CSV download is free and has a header and 6 rows",
          csv.status_code == 200 and csv.text.splitlines()[0].strip() == "band,value" and len(csv.text.strip().splitlines()) == 7
          and spent("an_test_routes") == Decimal(str(EPS)))
    check("it is listed under past results", f"/engine/result/{rid}" in an.get("/engine/results").text)
    r = an2.post("/engine/needs/run", data={"dataset": "an_test_routes", "grouping": "need_type_status", "csrf": ta2}, follow_redirects=False)
    check("a second analyst's run spends the same budget", r.status_code == 303 and spent("an_test_routes") == Decimal(str(EPS)) * 2)

    # ---- an exhausted budget
    r = an.post("/engine/needs/run", data={"dataset": "an_test_routes", "grouping": "status", "csrf": ta})
    check("a run on an exhausted budget is refused (409) and releases nothing",
          r.status_code == 409 and "Privacy budget exhausted" in r.text and audits("an_test_routes") == 2 and spent("an_test_routes") == Decimal(str(EPS)) * 2)
    p = an.post("/engine/needs/preview", data={"dataset": "an_test_routes", "grouping": "status", "csrf": ta})
    check("its preview says the query would be refused", p.status_code == 200 and "would be refused" in p.text)

    # ---- the real budgets
    check("needs_synthetic and needs_live were not charged and have no new audit rows",
          {n: spent(n) for n in REAL} == REAL and {n: audits(n) for n in REAL} == REAL_AUDITS)
finally:
    ne.SOURCES.pop("an_test_routes", None)
    cleanup()
    left = one("SELECT count(*) FROM datasets WHERE left(name, 8) = 'an_test_'")
    print(f"throwaway datasets and users removed (datasets left with the an_test_ prefix: {left})")

assert not fails, f"failed: {fails}"
print(f"PASS: {total} checks")
