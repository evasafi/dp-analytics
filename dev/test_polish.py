"""Wording and layout fixes found while using the app in a browser.

Covers: funding wording when nothing is confirmed, the requester's receipt heading, the pledge form's errors as a list,
the IBAN that must not wrap, the document check being explained to both sides, and the small-cell warning and dataset
name on the analytics pages.

Run from the project root as a module: python -m dev.test_polish
Needs the database owner's password (hidden prompt), refuses to run on a database without the financial-needs schema,
creates throwaway accounts with the prefix pl_, spends no privacy budget, and removes everything it made.
"""
import getpass
import json
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
USERS = {"pl_req": "requester", "pl_s1": "supporter", "pl_s2": "supporter", "pl_s3": "supporter", "pl_v": "verifier", "pl_an": "analyst"}
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


def spent_total():
    return one("SELECT COALESCE(sum(epsilon_spent), 0) FROM datasets")


def cleanup():
    sub = "(SELECT id FROM users WHERE left(username, 3) = 'pl_')"
    mine = f"(SELECT id FROM needs WHERE requester_id IN {sub})"
    with admin.begin() as c:
        c.execute(text(f"DELETE FROM results WHERE user_id IN {sub}"))
        c.execute(text(f"DELETE FROM audit_log WHERE user_id IN {sub}"))
        c.execute(text(f"DELETE FROM need_events WHERE actor_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM pledges WHERE supporter_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM needs WHERE requester_id IN {sub}"))
        c.execute(text("DELETE FROM users WHERE left(username, 3) = 'pl_'"))


def client_for(u):
    cl = TestClient(app, base_url="http://127.0.0.1:8000")
    tok = csrf_from(cl.get("/login").text)
    assert cl.post("/login", data={"username": u, "password": PW, "csrf": tok}, follow_redirects=False).status_code == 303
    return cl


cleanup()
budget_before = spent_total()
with admin.begin() as c:
    for u, role in USERS.items():
        c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r)"),
                  {"u": u, "h": hash_password(PW), "r": role})
uid = {u: one("SELECT id FROM users WHERE username = :u", u=u) for u in USERS}
DIST = DISTRICTS_BY_PROVINCE["Istanbul"][0]


def new_need(published=True, goal=48000):
    with admin.begin() as c:
        nid = c.execute(text(
            "INSERT INTO needs (requester_id, purpose, currency, province, district, household_size, goal, description, "
            "contact_method, contact, payee_name, payee_details) VALUES (:r, 'rent_housing', 'TRY', 'Istanbul', :d, 4, :g, "
            "'A private description for the test.', 'other', 'ask at the corner shop', 'Test Holder', 'TR330006100519786457841326') RETURNING id"),
            {"r": uid["pl_req"], "d": DIST, "g": goal}).scalar_one()
        if published:
            c.execute(text("UPDATE needs SET status = 'published', public_text = 'Rent help.', payee_verified = true, reviewed_by = :v WHERE id = :n"),
                      {"v": uid["pl_v"], "n": nid})
    return nid


def pledge(nid, sup, amount):
    with admin.begin() as c:
        return c.execute(text("INSERT INTO pledges (need_id, supporter_id, amount_try, ack) VALUES (:n, :s, :a, true) RETURNING id"),
                         {"n": nid, "s": uid[sup], "a": amount}).scalar_one()


def set_status(pid, st):
    with admin.begin() as c:
        c.execute(text("UPDATE pledges SET status = :s WHERE id = :i"), {"s": st, "i": pid})


try:
    board_routes._RATE.clear()
    anon = TestClient(app, base_url="http://127.0.0.1:8000")
    rq = client_for("pl_req")

    # ---- the funding line when nothing is confirmed yet
    n1 = new_need()
    p1, p2 = pledge(n1, "pl_s1", 1300), pledge(n1, "pl_s2", 500)
    board, page, mine = anon.get("/board").text, anon.get(f"/board/{n1}").text, rq.get("/needs/mine").text
    SENTENCE = "Nothing confirmed yet, 1,800 TRY promised, 2 donors."
    check("promises with nothing confirmed read in plain words on the board, the need's page and the requester's page",
          SENTENCE in board and SENTENCE in page and SENTENCE in mine)
    check("the odd old wording 'Confirmed 0 TRY, promised ... more' is gone from all three",
          "Confirmed 0 TRY" not in board and "Confirmed 0 TRY" not in page and "Confirmed 0 TRY" not in mine)
    set_status(p1, "sent")
    set_status(p1, "confirmed")
    check("once something is confirmed the familiar wording is unchanged",
          "Confirmed 1,300 TRY, promised 500 TRY more, 2 donors." in anon.get(f"/board/{n1}").text)

    # ---- the requester's receipt heading
    check("with no pledge marked sent there is no receipt section yet", "Money donors say they sent" not in rq.get("/needs/mine").text)
    set_status(p2, "sent")
    mine = rq.get("/needs/mine").text
    check("while a pledge waits for an answer the heading asks the requester to check their account and shows the button",
          "Money donors say they sent" in mine and "Check your account" in mine and "It arrived" in mine)
    set_status(p2, "confirmed")
    mine = rq.get("/needs/mine").text
    check("once every pledge is answered the heading says so and no answer is asked for",
          "Money donors sent" in mine and "All of it has been answered" in mine and "Money donors say they sent" not in mine
          and "Check your account" not in mine and "It arrived" not in mine)

    # ---- the pledge form's errors as a list
    n2 = new_need()
    s3 = client_for("pl_s3")
    tok = csrf_from(s3.get(f"/support/pledge/{n2}").text)
    r = s3.post(f"/support/pledge/{n2}", data={"amount": "5", "contact_method": "phone", "contact": "abc", "csrf": tok})
    box = re.search(r'<div class="error">(.*?)</div>', r.text, re.S)
    items = re.findall(r"<li>(.*?)</li>", box.group(1), re.S) if box else []
    check("a refused pledge lists each problem on its own line: amount, the unticked notice, the contact",
          r.status_code == 400 and len(items) == 3 and any("whole number of at least 10 lira" in i for i in items)
          and any("Tick the box" in i for i in items) and any("must have 7 to 15 digits" in i for i in items))
    check("it says once that nothing was saved, and keeps what was typed",
          r.text.count("Nothing was saved.") == 1 and 'value="5"' in r.text and 'value="abc"' in r.text)
    r = s3.post(f"/support/pledge/{n2}", data={"amount": "99999999", "ack": "on", "contact_method": "phone", "contact": "+90 555 000 00 00", "csrf": tok})
    box = re.search(r'<div class="error">(.*?)</div>', r.text, re.S)
    items = re.findall(r"<li>(.*?)</li>", box.group(1), re.S) if box else []
    check("a single problem is a list of one, still naming how much can be pledged",
          r.status_code == 400 and len(items) == 1 and "more than is still open" in items[0] and "At most 48,000 lira" in items[0])

    # ---- the account number must not break across lines
    r = s3.post(f"/support/pledge/{n2}", data={"amount": "100", "ack": "on", "contact_method": "phone", "contact": "+90 555 000 00 00", "csrf": tok}, follow_redirects=False)
    sup = s3.get("/support").text
    check("a valid pledge still works, and the IBAN is shown in one piece that cannot wrap",
          r.status_code == 303 and '<code style="white-space: nowrap">TR33 0006 1005 1978 6457 8413 26</code>' in sup and "Test Holder" in sup)

    # ---- the document check is explained to both sides
    check("the post form tells the requester a verifier will ask to see a supporting document",
          "ask to see a supporting document" in rq.get("/needs/new").text)
    n3 = new_need(published=False)
    vf = client_for("pl_v")
    review = vf.get(f"/verify/{n3}").text
    check("the review page tells the verifier how to ask for the document and that nothing stores it",
          "ask to see it" in review and "does not store documents" in review and "outside the platform" in review)

    # ---- the analytics pages
    an = client_for("pl_an")
    tok = csrf_from(an.get("/analyst").text)
    before = spent_total()
    r = an.post("/engine/needs/preview", data={"dataset": "needs_synthetic", "grouping": "province", "csrf": tok})
    check("the preview warns that a cell much smaller than the typical error is mostly noise and that suppressed does not mean empty",
          r.status_code == 200 and "mostly noise" in r.text and "does not mean empty" in r.text and spent_total() == before)
    with admin.begin() as c:
        ds = c.execute(text("SELECT id FROM datasets WHERE name = 'needs_synthetic'")).scalar_one()
        aid = c.execute(text("INSERT INTO audit_log (user_id, dataset_id, query_type, params, epsilon_charged, result_hash) "
                             "VALUES (:u, :d, 'needs_count', '{}'::jsonb, 0.1, 'polish-test') RETURNING id"), {"u": uid["pl_an"], "d": ds}).scalar_one()
        spec = {"measure": "count", "group_by": ["province"], "filters": [], "dataset": "needs_synthetic"}
        cells = [[["Adana"], 621], [["Adiyaman"], None], [["Ankara"], 1688]]
        rid = c.execute(text("INSERT INTO results (user_id, dataset_id, audit_id, spec, cells) VALUES (:u, :d, :a, CAST(:s AS jsonb), CAST(:c AS jsonb)) RETURNING id"),
                        {"u": uid["pl_an"], "d": ds, "a": aid, "s": json.dumps(spec), "c": json.dumps(cells)}).scalar_one()
    res = an.get(f"/engine/result/{rid}")
    check("the result page shows the friendly dataset name with the technical one in brackets, and says suppressed does not mean empty",
          res.status_code == 200 and "Synthetic needs (assumed data)" in res.text and "(needs_synthetic)" in res.text
          and "does not mean empty" in res.text and "Counts of needs, not people" in res.text and "suppressed" in res.text)
    check("none of this moved any privacy budget", spent_total() == budget_before)
finally:
    board_routes._RATE.clear()
    cleanup()
    left = one("SELECT count(*) FROM users WHERE left(username, 3) = 'pl_'")
    print(f"test rows removed (users left with the pl_ prefix: {left})")

assert not fails, f"failed: {fails}"
print(f"PASS: {total} checks")
