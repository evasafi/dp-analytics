"""The public board and the page for each need: only published needs, no private data, derived totals, filters,
sorting, paging, the map data, expiry of overdue pledges, no cookies, and the rate limit.

Run from the project root as a module: python -m dev.test_board
Needs the database owner's password (hidden prompt). The owner connection is built from the app's own
DP_DATABASE_URL and the test refuses to run against a database without the financial-needs schema.
Everything it creates has the username prefix bd_ and is removed at the end.
"""
import getpass
import html
import json
import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.auth import hash_password
from app.config import settings
from app.districts import DISTRICTS_BY_PROVINCE
from app.main import app
from app.provinces import PROVINCES
from app.routes import board as board_routes

PW = "throwaway-password-1"
admin = create_engine(make_url(settings.database_url).set(username="postgres", password=getpass.getpass("postgres password: ")))
USERS = {"bd_req": "requester", "bd_s1": "supporter", "bd_s2": "supporter", "bd_v": "verifier"}
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


def one(sql, **p):
    with admin.connect() as c:
        return c.execute(text(sql), p).scalar_one()


def rows(sql, **p):
    with admin.connect() as c:
        return [tuple(r) for r in c.execute(text(sql), p).all()]


def cleanup():
    sub = "(SELECT id FROM users WHERE left(username, 3) = 'bd_')"
    mine = f"(SELECT id FROM needs WHERE requester_id IN {sub})"
    with admin.begin() as c:
        c.execute(text(f"DELETE FROM need_events WHERE actor_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM pledges WHERE supporter_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM needs WHERE requester_id IN {sub}"))
        c.execute(text("DELETE FROM users WHERE left(username, 3) = 'bd_'"))


def reset_rate():
    board_routes._RATE.clear()


cleanup()
with admin.begin() as c:
    for u, role in USERS.items():
        c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r)"),
                  {"u": u, "h": hash_password(PW), "r": role})
uid = {u: one("SELECT id FROM users WHERE username = :u", u=u) for u in USERS}
rate = float(one("SELECT eur_try_rate FROM settings"))
HATAY = next(p for p in DISTRICTS_BY_PROVINCE if p == "Hatay") if "Hatay" in DISTRICTS_BY_PROVINCE else next(iter(DISTRICTS_BY_PROVINCE))
HATAY_D = DISTRICTS_BY_PROVINCE[HATAY][0]
OTHER = next(p for p in DISTRICTS_BY_PROVINCE if p != HATAY)
OTHER_D = DISTRICTS_BY_PROVINCE[OTHER][0]
PRIVATE = {"description": "PRIVATE-DESCRIPTION-MARKER", "contact": "PRIVATE-CONTACT-MARKER", "payee_name": "Privatepayee Markerson",
           "iban": "TR330006100519786457841326", "review": "PRIVATE-REVIEW-NOTE-MARKER", "withdrawn": "PRIVATE-WITHDRAWN-REASON-MARKER"}


def make(path, public_text="Public text for this need.", purpose="rent_housing", loc=None, event=None, cur="TRY", prov=HATAY, dist=HATAY_D, goal=48000):
    """Create a need and move it along the path: pending, published, rejected, withdrawn or fulfilled."""
    with admin.begin() as c:
        nid = c.execute(text(
            "INSERT INTO needs (requester_id, purpose, medical_location, event, currency, province, district, household_size, goal, "
            "description, contact_method, contact, payee_name, payee_details) VALUES (:r, :pu, :loc, :ev, :cur, :pv, :di, 7, :g, :de, 'other', :co, :pn, :pd) RETURNING id"),
            {"r": uid["bd_req"], "pu": purpose, "loc": loc, "ev": event, "cur": cur, "pv": prov, "di": dist, "g": goal,
             "de": PRIVATE["description"], "co": PRIVATE["contact"], "pn": PRIVATE["payee_name"], "pd": PRIVATE["iban"]}).scalar_one()
        if path == "rejected":
            c.execute(text("UPDATE needs SET status = 'rejected', review_note = :n WHERE id = :i"), {"n": PRIVATE["review"], "i": nid})
        if path in ("published", "withdrawn", "fulfilled"):
            c.execute(text("UPDATE needs SET status = 'published', public_text = :t, payee_verified = true, reviewed_by = :v WHERE id = :i"),
                      {"t": public_text, "v": uid["bd_v"], "i": nid})
        if path == "withdrawn":
            c.execute(text("UPDATE needs SET status = 'withdrawn', withdrawn_reason = :r WHERE id = :i"), {"r": PRIVATE["withdrawn"], "i": nid})
        if path == "fulfilled":
            c.execute(text("UPDATE needs SET status = 'fulfilled' WHERE id = :i"), {"i": nid})
    return nid


def pledge(nid, sup, amount, final):
    with admin.begin() as c:
        pid = c.execute(text("INSERT INTO pledges (need_id, supporter_id, amount_try, ack) VALUES (:n, :s, :a, true) RETURNING id"),
                        {"n": nid, "s": uid[sup], "a": amount}).scalar_one()
        for st in {"pledged": [], "sent": ["sent"], "confirmed": ["sent", "confirmed"]}[final]:
            c.execute(text("UPDATE pledges SET status = :s WHERE id = :i"), {"s": st, "i": pid})
    return pid


def cards(page_html):
    return [int(x) for x in re.findall(r'data-need="(\d+)"', page_html)]


try:
    cl = TestClient(app, base_url="http://127.0.0.1:8000")
    HOSTILE = "<b>bold</b> & <script>alert(1)</script> done"
    n1 = make("published", "Rent help for a family in need.")
    pledge(n1, "bd_s1", 10000, "confirmed")
    pledge(n1, "bd_s2", 5000, "sent")
    pledge(n1, "bd_s1", 3000, "pledged")
    n2 = make("published", "Surgery abroad for a child.", purpose="medical", loc="abroad", cur="EUR", prov=OTHER, dist=OTHER_D, goal=85000)
    pledge(n2, "bd_s1", 5000, "confirmed")
    n3 = make("published", HOSTILE, purpose="food", goal=3000)
    n4 = make("pending")
    n5 = make("rejected")
    n6 = make("withdrawn", "WITHDRAWN-PUBLIC-MARKER")
    n7 = make("fulfilled", "FULFILLED-PUBLIC-MARKER")
    n8 = make("published", "Repairs after the earthquake.", purpose="disaster_recovery", event="earthquake", goal=180000)
    hidden_ids = (n4, n5, n6, n7)

    # ---- what is shown
    reset_rate()
    r = cl.get("/board")
    page = r.text
    check("the board opens without signing in and sets no cookies", r.status_code == 200 and "set-cookie" not in r.headers and not cl.cookies)
    check("it lists published needs with purpose, place, goal and the verifier's text",
          "Rent help for a family in need." in page and "Rent and housing" in page and HATAY_D in page and "48,000 TRY" in page)
    check("a euro need is marked approximate, shown in euros, and a medical need says abroad",
          "euro goal, amounts approximate" in page and "85,000 EUR" in page and "Medical abroad" in page and "Surgery abroad for a child." in page)
    check("the event is shown for disaster recovery", "Disaster recovery" in page and "(Earthquake)" in page)
    check("pledge totals are derived: confirmed, promised on top, and the number of donors",
          "Confirmed 10,000 TRY, promised 8,000 TRY more, 2 donors." in page and "Confirmed 100 EUR, 1 donor." in page)
    check("a need with no pledges says so", "No pledges yet." in page)
    shown = set(cards(page))
    check("pending, rejected, withdrawn and fulfilled needs are not shown",
          shown.isdisjoint(hidden_ids) and "WITHDRAWN-PUBLIC-MARKER" not in page and "FULFILLED-PUBLIC-MARKER" not in page)
    check("every private detail stays off the board and the map data",
          all(v not in page and v not in cl.get("/board/map-data").text for v in PRIVATE.values()) and "household" not in page.lower())

    # ---- the page for one need
    reset_rate()
    r1 = cl.get(f"/board/{n1}")
    check("a need has its own public page: text, goal, totals, how giving works, no cookies",
          r1.status_code == 200 and "set-cookie" not in r1.headers and "Rent help for a family in need." in r1.text and "48,000 TRY" in r1.text
          and "Confirmed 10,000 TRY, promised 8,000 TRY more, 2 donors." in r1.text and "How giving works" in r1.text and "10 lira or more" in r1.text)
    check("it shows no private detail", all(v not in r1.text for v in PRIVATE.values()))
    r2 = cl.get(f"/board/{n2}")
    check("a euro need's page shows the lira estimate and says it is only an estimate",
          f"{85000 * rate:,.0f} TRY" in r2.text and "estimate" in r2.text and "85,000 EUR" in r2.text)
    codes = [cl.get(f"/board/{i}").status_code for i in hidden_ids] + [cl.get("/board/99999999").status_code]
    check("pending, rejected, withdrawn, fulfilled and unknown needs have no public page (all 404): " + str(codes), all(c == 404 for c in codes))
    check("a malformed id is refused (422)", cl.get("/board/abc").status_code == 422)

    # ---- hostile text
    page3 = cl.get(f"/board/{n3}").text
    check("hostile public text is escaped on the board and on the need page",
          "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script>alert(1)" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page3 and "<script>alert(1)" not in page3)

    # ---- filters, sorting, paging
    reset_rate()
    pub = rows("SELECT id, purpose, province, confirmed, promised, goal FROM public_needs")
    t = lambda html_: int(re.search(r"(\d+) open need", html_).group(1))
    check("the total equals the number of published needs", t(page) == len(pub) == one("SELECT count(*) FROM needs WHERE status = 'published'"))
    r = cl.get("/board?purpose=rent_housing")
    check("filtering by purpose gives exactly the matching needs", t(r.text) == sum(1 for p in pub if p[1] == "rent_housing") and n1 in cards(r.text) and n3 not in cards(r.text))
    r = cl.get(f"/board?province={HATAY}")
    check("filtering by province gives exactly the matching needs", t(r.text) == sum(1 for p in pub if p[2] == HATAY) and n1 in cards(r.text) and n2 not in cards(r.text))
    r = cl.get(f"/board?purpose=rent_housing&province={HATAY}")
    check("both filters together", t(r.text) == sum(1 for p in pub if p[1] == "rent_housing" and p[2] == HATAY))
    bad = [cl.get("/board?purpose=gold").status_code, cl.get("/board?province=Atlantis").status_code, cl.get("/board?sort=random").status_code,
           cl.get("/board?purpose='; DROP TABLE needs;--").status_code, cl.get("/board/map-data?purpose=gold").status_code]
    check("unknown filters are refused (400): " + str(bad), all(c == 400 for c in bad))
    check("page numbers are validated (0 and 1001 give 422)", cl.get("/board?page=0").status_code == 422 and cl.get("/board?page=1001").status_code == 422)
    order = {"new": lambda p: -p[0], "least": lambda p: (float(p[3]) / p[5], -p[0]), "near": lambda p: (-float(p[4]) / p[5], -p[0])}
    firsts = {k: cards(cl.get(f"/board?sort={k}").text)[0] for k in order}
    check("sorting by newest, least funded and closest to the goal puts the right need first: " + str(firsts),
          all(firsts[k] == sorted(pub, key=order[k])[0][0] for k in order))
    for i in range(25):
        make("published", f"Filler need {i}.", purpose="other", goal=1000 + i)
    p1, p2 = cl.get("/board").text, cl.get("/board?page=2").text
    check("a page holds 20 needs, with Older on the first page and Newer on the second",
          len(cards(p1)) == 20 and "Older" in p1 and "Newer" not in p1 and "Newer" in p2 and len(set(cards(p1)) & set(cards(p2))) == 0)

    # ---- the map data
    reset_rate()
    md = cl.get("/board/map-data")
    data = md.json()
    expect = dict(rows("SELECT province, count(*) FROM public_needs GROUP BY province"))
    check("the map data lists every province with counts equal to the published needs, and may be cached for a minute",
          md.status_code == 200 and len(data) == len(PROVINCES) and all(d["count"] == expect.get(d["name"], 0) for d in data)
          and "max-age=60" in md.headers.get("cache-control", "") and "set-cookie" not in md.headers)
    expect_r = dict(rows("SELECT province, count(*) FROM public_needs WHERE purpose = 'rent_housing' GROUP BY province"))
    check("the map data follows the purpose filter",
          all(d["count"] == expect_r.get(d["name"], 0) for d in cl.get("/board/map-data?purpose=rent_housing").json()))

    # ---- the credit line
    check("the map credit is hidden until the map has drawn", 'id="map-credit" hidden' in page)

    # ---- overdue pledges stop counting
    reset_rate()
    n9 = make("published", "Overdue pledge need.", purpose="utilities", goal=6000)
    pid = pledge(n9, "bd_s2", 1300, "pledged")
    before = cl.get("/board").text
    with admin.begin() as c:
        c.execute(text("UPDATE pledges SET expires_at = now() - interval '1 day' WHERE id = :i"), {"i": pid})
    after = cl.get(f"/board/{n9}").text
    check("a pledge that is overdue and unsent stops counting, and is marked expired",
          "No pledges yet." in after and one("SELECT status FROM pledges WHERE id = :i", i=pid) == "expired" and "No pledges yet." in cl.get("/board").text)

    # ---- the rate limit
    reset_rate()
    codes = [cl.get("/board").status_code for _ in range(60)]
    check("60 requests a minute from one address are served", all(c == 200 for c in codes))
    over = [cl.get("/board").status_code, cl.get("/board/map-data").status_code, cl.get(f"/board/{n1}").status_code]
    check("the 61st request, on any public page, gets 429: " + str(over), all(c == 429 for c in over))
finally:
    reset_rate()
    cleanup()
    left = one("SELECT count(*) FROM users WHERE left(username, 3) = 'bd_'")
    print(f"test rows removed (users left with the bd_ prefix: {left})")

assert not fails, f"failed: {fails}"
print(f"PASS: {total} checks")
