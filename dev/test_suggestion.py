"""The suggested amount on the pledge form: open amount divided by the average number of donors of finished lira needs in
the same goal band, rounded up to 10 lira, between the floor and the open amount; hidden for euro needs and for bands
with too few finished needs.

Run from the project root as a module: python -m dev.test_suggestion
Needs the database owner's password (hidden prompt). The owner connection is built from the app's own
DP_DATABASE_URL and the test refuses to run against a database without the financial-needs schema.
Everything it creates has the username prefix sg_ and is removed at the end. One check raises
settings.suggestion_min_needs for a moment and puts the old value back.
"""
import getpass
import math
import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.auth import hash_password
from app.config import settings
from app.districts import DISTRICTS_BY_PROVINCE
from app.main import app

PW = "throwaway-password-1"
admin = create_engine(make_url(settings.database_url).set(username="postgres", password=getpass.getpass("postgres password: ")))
USERS = {"sg_req": "requester", "sg_d1": "supporter", "sg_d2": "supporter", "sg_d3": "supporter", "sg_s": "supporter"}
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


def cleanup():
    sub = "(SELECT id FROM users WHERE left(username, 3) = 'sg_')"
    mine = f"(SELECT id FROM needs WHERE requester_id IN {sub})"
    with admin.begin() as c:
        c.execute(text(f"DELETE FROM need_events WHERE actor_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM pledges WHERE supporter_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM needs WHERE requester_id IN {sub}"))
        c.execute(text("DELETE FROM users WHERE left(username, 3) = 'sg_'"))


cleanup()
with admin.begin() as c:
    for u, role in USERS.items():
        c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r)"),
                  {"u": u, "h": hash_password(PW), "r": role})
uid = {u: one("SELECT id FROM users WHERE username = :u", u=u) for u in USERS}
FLOOR = one("SELECT pledge_floor FROM settings")
MIN_NEEDS = one("SELECT suggestion_min_needs FROM settings")
ISTANBUL = DISTRICTS_BY_PROVINCE["Istanbul"][0]


def make(goal, purpose="rent_housing", loc=None, cur="TRY"):
    with admin.begin() as c:
        nid = c.execute(text(
            "INSERT INTO needs (requester_id, purpose, medical_location, currency, province, district, household_size, goal, description, "
            "contact_method, contact, payee_name, payee_details) VALUES (:r, :pu, :loc, :cur, 'Istanbul', :di, 3, :g, "
            "'A need created by the suggestion test.', 'other', 'ask the test', 'Ayse Demir', 'TR330006100519786457841326') RETURNING id"),
            {"r": uid["sg_req"], "pu": purpose, "loc": loc, "cur": cur, "di": ISTANBUL, "g": goal}).scalar_one()
        c.execute(text("UPDATE needs SET status = 'published', public_text = 'Help.', payee_verified = true WHERE id = :i"), {"i": nid})
    return nid


def give(nid, donor, amount, final="confirmed"):
    with admin.begin() as c:
        pid = c.execute(text("INSERT INTO pledges (need_id, supporter_id, amount_try, ack) VALUES (:n, :s, :a, true) RETURNING id"),
                        {"n": nid, "s": uid[donor], "a": amount}).scalar_one()
        for st in {"pledged": [], "confirmed": ["sent", "confirmed"]}[final]:
            c.execute(text("UPDATE pledges SET status = :s WHERE id = :i"), {"s": st, "i": pid})


def band_stats(band):
    with admin.connect() as c:
        return c.execute(text("SELECT finished_needs, avg_donors FROM suggestion_stats WHERE band = :b"), {"b": band}).one_or_none()


def expected(open_try, avg):
    return min(max(math.ceil(open_try / avg / 10) * 10, FLOOR), open_try)


def suggested(page):
    m = re.search(r'id="suggest" data-amount="(\d+)"', page)
    return int(m.group(1)) if m else None


try:
    cl = TestClient(app, base_url="http://127.0.0.1:8000")
    tok = csrf_from(cl.get("/login").text)
    assert cl.post("/login", data={"username": "sg_s", "password": PW, "csrf": tok}, follow_redirects=False).status_code == 303

    # finished lira needs in band 2 (goals of 1,000 to 5,000), with two or three donors each, so the band is above the threshold
    BAND = one("SELECT code FROM amount_bands WHERE lo <= 3000 ORDER BY lo DESC LIMIT 1")
    before = band_stats(BAND)
    for i in range(max(3, MIN_NEEDS - (before.finished_needs if before else 0))):
        f = make(3000 if i % 2 else 2400)
        if i % 2:
            give(f, "sg_d1", 1000), give(f, "sg_d2", 1000), give(f, "sg_d3", 1000)
        else:
            give(f, "sg_d1", 1200), give(f, "sg_d2", 1200)
        assert one("SELECT status FROM needs WHERE id = :n", n=f) == "fulfilled"
    st = band_stats(BAND)
    check(f"the band now has enough finished needs ({st.finished_needs} of at least {MIN_NEEDS}), average {st.avg_donors} donors",
          st.finished_needs >= MIN_NEEDS and st.avg_donors > 0)

    # ---- shown, with the right amount
    n1 = make(4000)
    page = cl.get(f"/support/pledge/{n1}").text
    check(f"a lira need's form offers a suggestion of open / average donors, rounded up to 10: {suggested(page)} (expected {expected(4000, st.avg_donors)})",
          suggested(page) == expected(4000, st.avg_donors) and suggested(page) % 10 == 0)
    check("it explains how it was worked out, with the average and the number of finished needs",
          f"divided by {st.avg_donors:.1f}" in page and f"{st.finished_needs} finished needs" in page)
    check("the button only fills the field: it is not a submit button, and it stays hidden without JavaScript",
          'type="button" id="suggest"' in page and re.search(r'id="suggest"[^>]*hidden', page) is not None)
    give(n1, "sg_d1", 2500, "pledged")
    page = cl.get(f"/support/pledge/{n1}").text
    check(f"it shrinks as the need fills: {suggested(page)} for 1,500 open (expected {expected(1500, st.avg_donors)})",
          suggested(page) == expected(1500, st.avg_donors))
    give(n1, "sg_d2", 1485, "pledged")
    page = cl.get(f"/support/pledge/{n1}").text
    check(f"with 15 lira open it is between the floor and the open amount: {suggested(page)}",
          suggested(page) is not None and FLOOR <= suggested(page) <= 15)
    give(n1, "sg_d3", 10, "pledged")
    page = cl.get(f"/support/pledge/{n1}").text
    check("with less than the floor open there is no form and no suggestion", 'name="amount"' not in page and 'id="suggest"' not in page)

    # ---- hidden
    n2 = make(2000, purpose="medical", loc="abroad", cur="EUR")
    check("a euro need has no suggestion (no goal band)", 'id="suggest"' not in cl.get(f"/support/pledge/{n2}").text)
    n3 = make(3000)
    try:
        with admin.begin() as c:
            c.execute(text("UPDATE settings SET suggestion_min_needs = :m"), {"m": st.finished_needs + 1})
        check("a band with fewer finished needs than the threshold has no suggestion", 'id="suggest"' not in cl.get(f"/support/pledge/{n3}").text)
    finally:
        with admin.begin() as c:
            c.execute(text("UPDATE settings SET suggestion_min_needs = :m"), {"m": MIN_NEEDS})
    check("the threshold is back and the suggestion returns", one("SELECT suggestion_min_needs FROM settings") == MIN_NEEDS
          and suggested(cl.get(f"/support/pledge/{n3}").text) == expected(3000, st.avg_donors))
finally:
    cleanup()
    left = one("SELECT count(*) FROM users WHERE left(username, 3) = 'sg_'")
    print(f"test rows removed (users left with the sg_ prefix: {left}); suggestion_min_needs is {one('SELECT suggestion_min_needs FROM settings')}")

assert not fails, f"failed: {fails}"
print(f"PASS: {total} checks")
