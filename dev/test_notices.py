"""The notices that tell both sides what a bank transfer reveals.

Run from the project root as a module: python -m dev.test_notices
Needs the database owner's password (hidden prompt), refuses to run on a database without the financial-needs schema,
creates throwaway accounts with the prefix nt_, spends no privacy budget, and removes everything it made.
"""
import getpass
import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.auth import hash_password
from app.config import settings
from app.districts import DISTRICTS_BY_PROVINCE
from app.main import app
from app.routes import board as board_routes

PW = "throwaway-password-1"
admin = create_engine(make_url(settings.database_url).set(username="postgres", password=getpass.getpass("postgres password: ")))
USERS = {"nt_req": "requester", "nt_s": "supporter", "nt_v": "verifier"}
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


def csrf_from(page):
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def one(sql, **p):
    with admin.connect() as c:
        return c.execute(text(sql), p).scalar_one()


def cleanup():
    sub = "(SELECT id FROM users WHERE left(username, 3) = 'nt_')"
    mine = f"(SELECT id FROM needs WHERE requester_id IN {sub})"
    with admin.begin() as c:
        c.execute(text(f"DELETE FROM need_events WHERE actor_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM pledges WHERE supporter_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM needs WHERE requester_id IN {sub}"))
        c.execute(text("DELETE FROM users WHERE left(username, 3) = 'nt_'"))


def client_for(u):
    cl = TestClient(app, base_url="http://127.0.0.1:8000")
    tok = csrf_from(cl.get("/login").text)
    assert cl.post("/login", data={"username": u, "password": PW, "csrf": tok}, follow_redirects=False).status_code == 303
    return cl


cleanup()
with admin.begin() as c:
    for u, role in USERS.items():
        c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r)"),
                  {"u": u, "h": hash_password(PW), "r": role})
uid = {u: one("SELECT id FROM users WHERE username = :u", u=u) for u in USERS}

try:
    board_routes._RATE.clear()
    with admin.begin() as c:
        nid = c.execute(text(
            "INSERT INTO needs (requester_id, purpose, currency, province, district, household_size, goal, description, "
            "contact_method, contact, payee_name, payee_details) VALUES (:r, 'rent_housing', 'TRY', 'Istanbul', :d, 4, 48000, "
            "'A private description for the test.', 'other', 'ask at the corner shop', 'Test Holder', 'TR330006100519786457841326') RETURNING id"),
            {"r": uid["nt_req"], "d": DISTRICTS_BY_PROVINCE["Istanbul"][0]}).scalar_one()
        c.execute(text("UPDATE needs SET status = 'published', public_text = 'Rent help.', payee_verified = true, reviewed_by = :v WHERE id = :n"),
                  {"v": uid["nt_v"], "n": nid})
    sup = client_for("nt_s")
    page = sup.get(f"/support/pledge/{nid}").text
    check("the pledge form tells the donor that their bank shows their name and account to the requester",
          "Your bank will show your name and account to the requester when the money arrives" in page and "this site cannot hide that" in page)
    check("the old promise is still there, and still accurate: the site never gives the requester the donor's contact",
          "never to the requester or on the board" in page)
    tok = csrf_from(page)
    bad = sup.post(f"/support/pledge/{nid}", data={"amount": "5", "contact_method": "phone", "contact": "abc", "csrf": tok})
    check("the notice is still there after a refused pledge, when the form is shown again",
          bad.status_code == 400 and "Your bank will show your name and account to the requester" in bad.text)
    need = TestClient(app, base_url="http://127.0.0.1:8000").get(f"/board/{nid}").text
    check("the public need page tells a visitor that both sides see each other's name, and that the site never holds the money",
          "You will see the account holder's name, and your bank will show them yours" in need and "This site never holds it" in need)
    req = client_for("nt_req").get("/needs/new").text
    check("the post form tells the requester that every donor will see their name and IBAN, and they will see each donor's name",
          "Every donor who pledges will see the account holder's name and IBAN" in req and "each donor's name when money arrives" in req)
    check("nowhere does a page promise anonymity", all("anonymous" not in p.lower() for p in (page, need, req)))
    check("the donor's name is still not shown anywhere on the requester's own page",
          "nt_s" not in client_for("nt_req").get("/needs/mine").text)
finally:
    board_routes._RATE.clear()
    cleanup()
    left = one("SELECT count(*) FROM users WHERE left(username, 3) = 'nt_'")
    print(f"test rows removed (users left with the nt_ prefix: {left})")

assert not fails, f"failed: {fails}"
print(f"PASS: {total} checks")
