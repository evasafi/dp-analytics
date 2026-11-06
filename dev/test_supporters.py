"""Supporter pledge flow: access, the pledge form, input checks, the goal cap (also under concurrent pledges), the limit
on unconfirmed pledges, the account shown only to the donor and only while the pledge is open, marking as sent,
cancelling, expiry, and a withdrawn need.

Run from the project root as a module: python -m dev.test_supporters
Needs the database owner's password (hidden prompt). The owner connection is built from the app's own
DP_DATABASE_URL and the test refuses to run against a database without the financial-needs schema.
Everything it creates has the username prefix sp_ and is removed at the end.
"""
import getpass
import re
import threading

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
USERS = {"sp_req": "requester", "sp_s1": "supporter", "sp_s2": "supporter", "sp_s3": "supporter",
         "sp_v": "verifier", "sp_an": "analyst", "sp_ad": "admin"}
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
    sub = "(SELECT id FROM users WHERE left(username, 3) = 'sp_')"
    mine = f"(SELECT id FROM needs WHERE requester_id IN {sub})"
    with admin.begin() as c:
        c.execute(text(f"DELETE FROM need_events WHERE actor_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM pledges WHERE supporter_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM needs WHERE requester_id IN {sub}"))
        c.execute(text("DELETE FROM users WHERE left(username, 3) = 'sp_'"))


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
uid = {u: one("SELECT id FROM users WHERE username = :u", u=u) for u in USERS}
s = {k: one(f"SELECT {k} FROM settings") for k in ("eur_try_rate", "pledge_floor", "pledge_expiry_days", "max_unconfirmed_pledges")}
ISTANBUL = DISTRICTS_BY_PROVINCE["Istanbul"][0]
PAYEE, IBAN, IBAN_SHOWN = "Ayse Demir", "TR330006100519786457841326", "TR33 0006 1005 1978 6457 8413 26"
PRIVATE = {"description": "PRIVATE-DESCRIPTION-MARKER", "contact": "PRIVATE-REQUESTER-CONTACT"}
DONOR = "donor-one@example.com"


def make(path="published", goal=48000, purpose="rent_housing", loc=None, cur="TRY", public_text="Public text for this need."):
    with admin.begin() as c:
        nid = c.execute(text(
            "INSERT INTO needs (requester_id, purpose, medical_location, currency, province, district, household_size, goal, "
            "description, contact_method, contact, payee_name, payee_details) VALUES (:r, :pu, :loc, :cur, 'Istanbul', :di, 4, :g, :de, 'other', :co, :pn, :pd) "
            "RETURNING id"), {"r": uid["sp_req"], "pu": purpose, "loc": loc, "cur": cur, "di": ISTANBUL, "g": goal,
                              "de": PRIVATE["description"], "co": PRIVATE["contact"], "pn": PAYEE, "pd": IBAN}).scalar_one()
        if path == "rejected":
            c.execute(text("UPDATE needs SET status = 'rejected', review_note = 'not enough' WHERE id = :i"), {"i": nid})
        if path in ("published", "withdrawn", "fulfilled"):
            c.execute(text("UPDATE needs SET status = 'published', public_text = :t, payee_verified = true WHERE id = :i"), {"t": public_text, "i": nid})
        if path == "withdrawn":
            c.execute(text("UPDATE needs SET status = 'withdrawn', withdrawn_reason = 'No longer needed at all.' WHERE id = :i"), {"i": nid})
        if path == "fulfilled":
            c.execute(text("UPDATE needs SET status = 'fulfilled' WHERE id = :i"), {"i": nid})
    return nid


def post_pledge(cl, tok, nid, amount, ack=True, method="email", contact=DONOR):
    data = {"amount": str(amount), "contact_method": method, "contact": contact, "csrf": tok}
    if ack:
        data["ack"] = "on"
    return cl.post(f"/support/pledge/{nid}", data=data, follow_redirects=False)


def pledges_on(nid):
    return one("SELECT count(*) FROM pledges WHERE need_id = :n", n=nid)


def pstat(pid):
    return one("SELECT status FROM pledges WHERE id = :p", p=pid)


def last_pledge(sup):
    return one("SELECT max(id) FROM pledges WHERE supporter_id = :s", s=uid[sup])


def events(nid, kind):
    return one("SELECT count(*) FROM need_events WHERE need_id = :n AND event = :k", n=nid, k=kind)


def row_of(page, pid):
    m = re.search(rf'<tr data-pledge="{pid}">(.*?)</tr>', page, re.S)
    return m.group(1) if m else ""


try:
    board_routes._RATE.clear()
    s1, t1 = client_for("sp_s1")
    s2, t2 = client_for("sp_s2")
    s3, t3 = client_for("sp_s3")
    others = {u: client_for(u) for u in ("sp_req", "sp_v", "sp_an", "sp_ad")}
    anon = TestClient(app, base_url="http://127.0.0.1:8000")
    n1 = make(goal=48000, public_text="Rent help for a family.")

    # ---- access
    check("a supporter lands on /support after sign-in", s1.get("/", follow_redirects=False).headers.get("location") == "/support")
    codes = []
    for u, (cl, tok) in others.items():
        codes += [cl.get("/support").status_code, cl.get(f"/support/pledge/{n1}").status_code,
                  post_pledge(cl, tok, n1, 100).status_code,
                  cl.post("/support/1/sent", data={"csrf": tok}).status_code, cl.post("/support/1/cancel", data={"csrf": tok}).status_code]
    check("requesters, verifiers, analysts and admins are refused (403) on every supporter route", set(codes) == {403})
    codes = [anon.get("/support").status_code, anon.get(f"/support/pledge/{n1}").status_code,
             anon.post(f"/support/pledge/{n1}", data={"amount": "100", "ack": "on", "contact_method": "email", "contact": DONOR, "csrf": "x"}).status_code]
    check("anonymous visitors get 401: " + str(codes), set(codes) == {401})
    check("no pledge was made by any of those requests", pledges_on(n1) == 0)

    # ---- the form
    form = s1.get(f"/support/pledge/{n1}")
    check("the pledge form shows the public text, what is still open, the floor and the notice",
          form.status_code == 200 and "Rent help for a family." in form.text and "48,000 TRY" in form.text
          and f"smallest pledge is {s['pledge_floor']} lira" in form.text and "cannot refund" in form.text and 'name="ack"' in form.text)
    check("before pledging, the form shows neither the account nor any private detail",
          all(v not in form.text for v in (PAYEE, IBAN, IBAN_SHOWN, *PRIVATE.values())))
    board_routes._RATE.clear()
    check("the public need page links to the pledge form", f'href="/support/pledge/{n1}"' in anon.get(f"/board/{n1}").text)
    hidden = [make("pending"), make("rejected"), make("withdrawn"), make("fulfilled")]
    codes = [s1.get(f"/support/pledge/{h}").status_code for h in hidden] + [s1.get("/support/pledge/99999999").status_code]
    codes += [post_pledge(s1, t1, h, 100).status_code for h in hidden]
    check("pending, rejected, withdrawn, fulfilled and unknown needs cannot be pledged to (404): " + str(codes), set(codes) == {404})
    check("a malformed need id is refused (422)", s1.get("/support/pledge/abc").status_code == 422)

    # ---- input checks
    bad = {"below the floor": dict(amount=s["pledge_floor"] - 1), "not a number": dict(amount="ten"), "a decimal": dict(amount="10.5"),
           "a negative amount": dict(amount="-50"), "more than the goal": dict(amount=48001), "no tick on the notice": dict(amount=100, ack=False),
           "a bad email": dict(amount=100, method="email", contact="not-an-email"), "an empty contact": dict(amount=100, method="other", contact="")}
    for label, kw in bad.items():
        r = post_pledge(s1, t1, n1, **kw)
        check(f"refused: {label} (400, nothing saved)", r.status_code == 400 and "Nothing was saved." in r.text and pledges_on(n1) == 0)
    r = post_pledge(s1, t1, n1, 777, ack=False, contact="kept@example.com")
    check("a refused pledge gives the typed amount and contact back", 'value="777"' in r.text and 'value="kept@example.com"' in r.text)
    r = s1.post(f"/support/pledge/{n1}", data={"amount": "100", "ack": "on", "contact_method": "email", "contact": DONOR, "csrf": "forged"})
    check("a pledge with a forged CSRF token is refused (403)", r.status_code == 403 and pledges_on(n1) == 0)

    # ---- a valid pledge
    r = post_pledge(s1, t1, n1, 12000)
    p1 = last_pledge("sp_s1")
    check("a valid pledge: 303 to /support?done=pledged", r.status_code == 303 and r.headers["location"] == "/support?done=pledged")
    with admin.connect() as c:
        row = c.execute(text("SELECT amount_try, amount_need, status, ack, contact_method, contact, "
                             "round(extract(epoch FROM expires_at - created_at) / 86400) AS days FROM pledges WHERE id = :p"), {"p": p1}).one()
    check("it is stored as pledged, with the amount, the notice accepted, the donor's contact and the expiry",
          (row.amount_try, float(row.amount_need), row.status, row.ack, row.contact_method, row.contact, int(row.days))
          == (12000, 12000.0, "pledged", True, "email", DONOR, s["pledge_expiry_days"]))
    check("a 'pledged' event is written", events(n1, "pledged") == 1)
    page = s1.get("/support?done=pledged").text
    mine = row_of(page, p1)
    check("my pledges show the amount, the deadline, the account holder and the IBAN in groups",
          "12,000 TRY" in mine and "by 20" in mine and PAYEE in mine and IBAN_SHOWN in mine and "I have sent it" in mine and "Send the money" in page)
    check("my pledges show no private detail of the need", all(v not in page for v in PRIVATE.values()))
    check("another supporter does not see that account", PAYEE not in s2.get("/support").text and IBAN_SHOWN not in s2.get("/support").text)
    board_routes._RATE.clear()
    pub = anon.get("/board").text + anon.get(f"/board/{n1}").text
    check("the board counts the pledge as promised", "promised 12,000 TRY more, 1 donor." in pub or "12,000 TRY promised, 1 donor." in pub)
    check("the donor's contact appears on no public page and not on the requester's page",
          DONOR not in pub and DONOR not in others["sp_req"][0].get("/needs/mine").text)
    check("the form now shows the smaller open amount", "36,000 TRY" in s2.get(f"/support/pledge/{n1}").text)

    # ---- the goal cap
    n2 = make(goal=1000, purpose="food")
    post_pledge(s1, t1, n2, 900)
    r = post_pledge(s2, t2, n2, 200)
    check("a pledge above what is still open is refused (400) with the open amount", r.status_code == 400 and "At most 100 lira" in r.text and pledges_on(n2) == 1)
    post_pledge(s2, t2, n2, 100)
    full = s3.get(f"/support/pledge/{n2}").text
    check("a fully pledged need says so and shows no form", "fully pledged" in full and 'name="amount"' not in full)
    n3 = make(goal=1000, purpose="utilities")
    barrier, lock, out = threading.Barrier(2), threading.Lock(), []

    def worker(cl, tok):
        barrier.wait(timeout=30)
        r_ = post_pledge(cl, tok, n3, 600)
        with lock:
            out.append(r_.status_code)

    ts = [threading.Thread(target=worker, args=a) for a in ((s1, t1), (s2, t2))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    promised = one("SELECT COALESCE(sum(amount_need), 0) FROM pledges WHERE need_id = :n AND status IN ('pledged', 'sent', 'confirmed', 'disputed')", n=n3)
    # the loser gets 409 from the database trigger, or 400 from the form check if the winner committed first
    check(f"two simultaneous pledges of 600 on a goal of 1000: one accepted, one refused {sorted(out)}, promised {promised:.0f}",
          out.count(303) == 1 and len(out) == 2 and set(out) - {303} <= {400, 409} and float(promised) == 600 and pledges_on(n3) == 1)

    # ---- a euro need
    n4 = make(goal=2000, purpose="medical", loc="abroad", cur="EUR", public_text="Treatment abroad.")
    ef = s3.get(f"/support/pledge/{n4}").text
    check("a euro need's form gives the open amount in lira as an estimate at the placeholder rate",
          f"about <strong>{2000 * float(s['eur_try_rate']):,.0f} TRY" in ef and "placeholder rate" in ef)
    post_pledge(s3, t3, n4, 5000)
    pe = last_pledge("sp_s3")
    check("a euro pledge is stored in both currencies at the rate of the day",
          float(one("SELECT amount_need FROM pledges WHERE id = :p", p=pe)) == round(5000 / float(s["eur_try_rate"]), 2)
          and float(one("SELECT rate_used FROM pledges WHERE id = :p", p=pe)) == float(s["eur_try_rate"]))
    check("my pledges show the euro equivalent", f"about {5000 / float(s['eur_try_rate']):,.0f} EUR" in row_of(s3.get("/support").text, pe))

    # ---- marking as sent
    r = s2.post(f"/support/{p1}/sent", data={"csrf": t2}, follow_redirects=False)
    check("another supporter cannot mark my pledge as sent (404, unchanged)", r.status_code == 404 and pstat(p1) == "pledged")
    r = s1.post(f"/support/{p1}/sent", data={"csrf": "forged"}, follow_redirects=False)
    check("marking as sent with a forged CSRF token is refused (403)", r.status_code == 403 and pstat(p1) == "pledged")
    r = s1.post(f"/support/{p1}/sent", data={"csrf": t1}, follow_redirects=False)
    check("marking as sent: 303, status sent, time recorded, event written",
          r.status_code == 303 and r.headers["location"] == "/support?done=sent" and pstat(p1) == "sent"
          and one("SELECT sent_at IS NOT NULL FROM pledges WHERE id = :p", p=p1) and events(n1, "pledge_sent") == 1)
    mine = row_of(s1.get("/support").text, p1)
    check("a sent pledge still shows the account, and no buttons", IBAN_SHOWN in mine and "Waiting for the requester" in mine and "<button" not in mine)
    r = s1.post(f"/support/{p1}/sent", data={"csrf": t1}, follow_redirects=False)
    r2 = s1.post(f"/support/{p1}/cancel", data={"csrf": t1}, follow_redirects=False)
    check("a sent pledge can be neither sent again nor cancelled (409)", r.status_code == 409 and r2.status_code == 409 and pstat(p1) == "sent")
    check("a malformed pledge id is refused (422)", s1.post("/support/abc/sent", data={"csrf": t1}).status_code == 422)

    # ---- cancelling
    n5 = make(goal=5000, purpose="debt")
    post_pledge(s2, t2, n5, 3000)
    p5 = last_pledge("sp_s2")
    r = s2.post(f"/support/{p5}/cancel", data={"csrf": t2}, follow_redirects=False)
    check("cancelling: 303, status cancelled, event written",
          r.status_code == 303 and r.headers["location"] == "/support?done=cancelled" and pstat(p5) == "cancelled" and events(n5, "pledge_cancelled") == 1)
    mine = row_of(s2.get("/support").text, p5)
    board_routes._RATE.clear()
    check("a cancelled pledge hides the account and frees the amount", IBAN_SHOWN not in mine and PAYEE not in mine
          and "Cancelled" in mine and "No pledges yet." in anon.get(f"/board/{n5}").text)

    # ---- expiry
    n6 = make(goal=5000, purpose="education")
    post_pledge(s2, t2, n6, 2000)
    p6 = last_pledge("sp_s2")
    with admin.begin() as c:
        c.execute(text("UPDATE pledges SET expires_at = now() - interval '1 minute' WHERE id = :p"), {"p": p6})
    mine = row_of(s2.get("/support").text, p6)
    check("an overdue unsent pledge shows as expired, without the account", pstat(p6) == "expired" and "Expired" in mine and IBAN_SHOWN not in mine)
    with admin.begin() as c:
        p7 = c.execute(text("INSERT INTO pledges (need_id, supporter_id, amount_try, ack, contact_method, contact) "
                            "VALUES (:n, :s, 1000, true, 'email', :d) RETURNING id"), {"n": n6, "s": uid["sp_s2"], "d": DONOR}).scalar_one()
        c.execute(text("UPDATE pledges SET expires_at = now() - interval '1 minute' WHERE id = :p"), {"p": p7})
    r = s2.post(f"/support/{p7}/sent", data={"csrf": t2}, follow_redirects=False)
    check("an overdue pledge cannot be marked as sent (409, expired)", r.status_code == 409 and pstat(p7) == "expired")

    # ---- a withdrawn need
    n8 = make(goal=9000, purpose="other")
    post_pledge(s3, t3, n8, 1000)
    p8a = last_pledge("sp_s3")
    post_pledge(s3, t3, n8, 2000)
    p8b = last_pledge("sp_s3")
    s3.post(f"/support/{p8b}/sent", data={"csrf": t3})
    with admin.begin() as c:
        c.execute(text("UPDATE needs SET status = 'withdrawn', withdrawn_reason = 'The family moved in with relatives.' WHERE id = :n"), {"n": n8})
    page = s3.get("/support").text
    ra, rb = row_of(page, p8a), row_of(page, p8b)
    check("when the need is withdrawn, an unsent pledge is cancelled and hides the account; a sent one keeps it",
          pstat(p8a) == "cancelled" and IBAN_SHOWN not in ra and pstat(p8b) == "sent" and IBAN_SHOWN in rb
          and "Need: Withdrawn" in ra and "Need: Withdrawn" in rb)
    reason_line = "The requester withdrew this need: The family moved in with relatives."
    check("a donor with pledges on the withdrawn need sees the reason on those rows", reason_line in ra and reason_line in rb)
    check("the reason is not on that donor's rows for other needs", row_of(page, pe) != "" and "moved in with relatives" not in row_of(page, pe))
    check("a donor with pledges only on other needs does not see it",
          s2.get("/support").text.count("<tr data-pledge=") > 0 and "moved in with relatives" not in s2.get("/support").text)

    # ---- the limit on unconfirmed pledges
    n9 = make(goal=300000, purpose="other")
    open_now = one("SELECT count(*) FROM pledges WHERE supporter_id = :s AND status IN ('pledged', 'sent', 'disputed')", s=uid["sp_s3"])
    with admin.begin() as c:
        for _ in range(s["max_unconfirmed_pledges"] - open_now):
            c.execute(text("INSERT INTO pledges (need_id, supporter_id, amount_try, ack) VALUES (:n, :s, 100, true)"), {"n": n9, "s": uid["sp_s3"]})
    before = pledges_on(n9)
    r = post_pledge(s3, t3, n9, 100)
    check(f"a supporter with {s['max_unconfirmed_pledges']} unconfirmed pledges cannot pledge again (429)",
          r.status_code == 429 and "not confirmed yet" in r.text and pledges_on(n9) == before)
    check("the page counts them", f"{s['max_unconfirmed_pledges']} of {s['max_unconfirmed_pledges']} not yet confirmed" in s3.get("/support").text)

    # ---- hostile public text
    n10 = make(goal=5000, purpose="food", public_text="<script>alert(1)</script> food")
    hp = s1.get(f"/support/pledge/{n10}").text
    check("hostile public text is escaped on the pledge form", "&lt;script&gt;alert(1)&lt;/script&gt;" in hp and "<script>alert(1)" not in hp)
finally:
    board_routes._RATE.clear()
    cleanup()
    left = one("SELECT count(*) FROM users WHERE left(username, 3) = 'sp_'")
    print(f"test rows removed (users left with the sp_ prefix: {left})")

assert not fails, f"failed: {fails}"
print(f"PASS: {total} checks")
