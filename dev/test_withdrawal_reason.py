"""Withdrawal reason: donors who pledged will read it, so the form says so and the reason gets the same text guard as
the verifier's public text.

Run from the project root as a module: python -m dev.test_withdrawal_reason
Needs the database owner's password (hidden prompt). The owner connection is built from the app's own
DP_DATABASE_URL and the test refuses to run against a database without the financial-needs schema.
Everything it creates has the username prefix wr_ and is removed at the end.
"""
import getpass
import html
import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.auth import hash_password
from app.config import settings
from app.districts import DISTRICTS_BY_PROVINCE
from app.main import app
from app.validation import public_text_problem

PW = "throwaway-password-1"
admin = create_engine(make_url(settings.database_url).set(username="postgres", password=getpass.getpass("postgres password: ")))
USERS = {"wr_req": "requester", "wr_v": "verifier"}
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
    sub = "(SELECT id FROM users WHERE left(username, 3) = 'wr_')"
    mine = f"(SELECT id FROM needs WHERE requester_id IN {sub})"
    with admin.begin() as c:
        c.execute(text(f"DELETE FROM need_events WHERE actor_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM pledges WHERE need_id IN {mine}"))
        c.execute(text(f"DELETE FROM needs WHERE requester_id IN {sub}"))
        c.execute(text("DELETE FROM users WHERE left(username, 3) = 'wr_'"))


cleanup()
with admin.begin() as c:
    for u, role in USERS.items():
        c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r)"),
                  {"u": u, "h": hash_password(PW), "r": role})
uid = {u: one("SELECT id FROM users WHERE username = :u", u=u) for u in USERS}
ISTANBUL = DISTRICTS_BY_PROVINCE["Istanbul"][0]
CONTACT = "ask at the corner shop"
IBAN = "TR330006100519786457841326"
CLEAN = "The family found help elsewhere and no longer needs the money."


def new_need(published=True):
    with admin.begin() as c:
        nid = c.execute(text(
            "INSERT INTO needs (requester_id, purpose, currency, province, district, household_size, goal, description, "
            "contact_method, contact, payee_name, payee_details) VALUES (:r, 'rent_housing', 'TRY', 'Istanbul', :d, 4, 48000, "
            "'A private description of the need for the test.', 'other', :c, 'Ayse Demir', :i) RETURNING id"),
            {"r": uid["wr_req"], "d": ISTANBUL, "c": CONTACT, "i": IBAN}).scalar_one()
        if published:
            c.execute(text("UPDATE needs SET status = 'published', public_text = 'Rent help.', payee_verified = true, reviewed_by = :v WHERE id = :n"),
                      {"v": uid["wr_v"], "n": nid})
    return nid


def status_of(nid):
    return one("SELECT status FROM needs WHERE id = :n", n=nid)


def withdrawn_events(nid):
    return one("SELECT count(*) FROM need_events WHERE need_id = :n AND event = 'withdrawn'", n=nid)


try:
    # ---- the guard function itself
    check("with its defaults the guard gives exactly the old messages (the verifier's text is unchanged)",
          public_text_problem("Short.", "c", "n", "u") == "The public text must be 10 to 300 characters."
          and public_text_problem("Write to me at a@b.co about it", "c", "n", "u") == "The public text must not contain an email address or a link."
          and public_text_problem(f"Ask at {CONTACT} instead", CONTACT, "n", "u") == "The public text contains the requester's contact details.")
    check("the length limit is a parameter: 400 characters pass with a limit of 500 and fail with the default",
          public_text_problem("x" * 400, "c", "n", "u", max_len=500) is None
          and public_text_problem("x" * 400, "c", "n", "u") == "The public text must be 10 to 300 characters.")
    check("the wording follows what the text is and who it belongs to",
          public_text_problem(f"Ask at {CONTACT} instead", CONTACT, "n", "u", what="reason", owner="your") == "The reason contains your contact details."
          and public_text_problem("Posted by wr_req today", "c", "n", "wr_req", what="reason", owner="your") == "The reason contains your username.")

    # ---- the form
    cl = TestClient(app, base_url="http://127.0.0.1:8000")
    tok = csrf_from(cl.get("/login").text)
    assert cl.post("/login", data={"username": "wr_req", "password": PW, "csrf": tok}, follow_redirects=False).status_code == 303
    n1 = new_need()
    page = cl.get("/needs/mine").text
    tok = csrf_from(page)
    check("the withdraw form tells the requester that donors will see the reason, and not to write personal details",
          "Donors who pledged to this need will see your reason" in page and "Do not write names, phone numbers or other personal details" in page)

    # ---- refusals: 400, nothing changed, the typed text comes back
    unsafe = [("an email address", "Write to me at a@b.co about the rest", "email address or a link"),
              ("a link", "See http://example.com for what happened", "email address or a link"),
              ("a web address", "More at www.example.com about all of it", "email address or a link"),
              ("a phone number", "Call 0532 123 45 67 about the money", "phone number or an account number"),
              ("an account number", "The money went to 33000610051978 already", "phone number or an account number"),
              ("the contact details", f"Ask at {CONTACT} instead of here", "contact details"),
              ("part of the account holder's name", "Ayse will not need the money any more", "part of the account holder's name"),
              ("the username", "Posted by wr_req some time ago", "username")]
    for label, reason, expect in unsafe:
        r = cl.post(f"/needs/{n1}/withdraw", data={"reason": reason + " TYPED-MARKER", "csrf": tok}, follow_redirects=False)
        body = html.unescape(r.text)
        check(f"refused: a reason with {label} (400, need unchanged, typed text kept, donors named as readers)",
              r.status_code == 400 and expect in body and "TYPED-MARKER" in body and "Donors who pledged to this need will read it" in body
              and status_of(n1) == "published" and withdrawn_events(n1) == 0
              and one("SELECT withdrawn_reason IS NULL FROM needs WHERE id = :n", n=n1))

    # ---- the existing rules still hold
    short = cl.post(f"/needs/{n1}/withdraw", data={"reason": "too short", "csrf": tok})
    long_ = cl.post(f"/needs/{n1}/withdraw", data={"reason": "x" * 501, "csrf": tok})
    check("the old length rule is unchanged: a short or a 501-character reason is refused with the old message",
          short.status_code == 400 and "without one" in short.text and long_.status_code == 400 and "without one" in long_.text
          and status_of(n1) == "published")
    check("a reason of 400 characters is allowed (the limit for a reason is 500, not 300)",
          public_text_problem("y" * 400, CONTACT, "Ayse Demir", "wr_req", max_len=500, what="reason", owner="your") is None)

    # ---- the clean path
    r = cl.post(f"/needs/{n1}/withdraw", data={"reason": CLEAN, "csrf": tok}, follow_redirects=False)
    check("a clean reason withdraws the need: 303, status withdrawn, the reason stored, one event",
          r.status_code == 303 and status_of(n1) == "withdrawn" and one("SELECT withdrawn_reason FROM needs WHERE id = :n", n=n1) == CLEAN
          and withdrawn_events(n1) == 1)
    again = cl.post(f"/needs/{n1}/withdraw", data={"reason": "Write to me at a@b.co about the rest", "csrf": tok})
    check("withdrawing an already withdrawn need is still refused first with 409, even with an unsafe reason",
          again.status_code == 409 and withdrawn_events(n1) == 1)

    # ---- pending needs get the same guard
    n2 = new_need(published=False)
    tok = csrf_from(cl.get("/needs/mine").text)
    bad = cl.post(f"/needs/{n2}/withdraw", data={"reason": "Call 0532 123 45 67 about the money", "csrf": tok})
    ok = cl.post(f"/needs/{n2}/withdraw", data={"reason": CLEAN, "csrf": tok}, follow_redirects=False)
    check("a pending need has the same guard: an unsafe reason is refused, a clean one withdraws it",
          bad.status_code == 400 and ok.status_code == 303 and status_of(n2) == "withdrawn")
finally:
    cleanup()
    left = one("SELECT count(*) FROM users WHERE left(username, 3) = 'wr_'")
    print(f"test rows removed (users left with the wr_ prefix: {left})")

assert not fails, f"failed: {fails}"
print(f"PASS: {total} checks")
