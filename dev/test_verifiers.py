"""Verifier flow: access, the review queue, private details, the two checks, public-text guard, approve, reject,
decisions that race, and the look-again list.

Run from the project root as a module: python -m dev.test_verifiers
Needs the database owner's password (hidden prompt). The owner connection is built from the app's own
DP_DATABASE_URL and the test refuses to run against a database without the financial-needs schema.
Everything it creates has the username prefix vf_ and is removed at the end.
"""
import getpass
import html
import re
import threading

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.auth import hash_password
from app.config import settings
from app.db import engine
from app.districts import DISTRICTS_BY_PROVINCE
from app.main import app

PW = "throwaway-password-1"
admin = create_engine(make_url(settings.database_url).set(username="postgres", password=getpass.getpass("postgres password: ")))
USERS = {"vf_a": "verifier", "vf_b": "verifier", "vf_req": "requester", "vf_s": "supporter", "vf_an": "analyst", "vf_ad": "admin"}
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


def rows(sql, **p):
    with admin.connect() as c:
        return [tuple(r) for r in c.execute(text(sql), p).all()]


def cleanup():
    sub = "(SELECT id FROM users WHERE left(username, 3) = 'vf_')"
    mine = f"(SELECT id FROM needs WHERE requester_id IN {sub})"
    with admin.begin() as c:
        c.execute(text(f"DELETE FROM need_events WHERE actor_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM pledges WHERE supporter_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM needs WHERE requester_id IN {sub}"))
        c.execute(text("DELETE FROM users WHERE left(username, 3) = 'vf_'"))


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
ISTANBUL = DISTRICTS_BY_PROVINCE["Istanbul"][0]
IBAN = "TR330006100519786457841326"
CONTACT = "ask at the corner shop"


def new_need(requester="vf_req", **over):
    d = {"r": uid[requester], "pu": "rent_housing", "loc": None, "ev": None, "cur": "TRY", "pv": "Istanbul", "di": ISTANBUL, "hh": 4,
         "g": 48000, "de": "The landlord is ending the lease; we need a deposit for a new flat.", "cm": "other", "co": CONTACT,
         "pn": "Ayse Demir", "pd": IBAN}
    d.update(over)
    with admin.begin() as c:
        return c.execute(text(
            "INSERT INTO needs (requester_id, purpose, medical_location, event, currency, province, district, household_size, goal, "
            "description, contact_method, contact, payee_name, payee_details) VALUES (:r, :pu, :loc, :ev, :cur, :pv, :di, :hh, :g, :de, :cm, :co, :pn, :pd) "
            "RETURNING id"), d).scalar_one()


def status_of(nid):
    return one("SELECT status FROM needs WHERE id = :n", n=nid)


def events_of(nid, kind):
    return one("SELECT count(*) FROM need_events WHERE need_id = :n AND event = :k", n=nid, k=kind)


GOOD_TEXT = "A family needs help with a deposit and the first rent payments for a new flat."


def approve(cl, tok, nid, public_text=GOOD_TEXT, payee=True, doc=True):
    data = {"public_text": public_text, "csrf": tok}
    if payee:
        data["check_payee"] = "on"
    if doc:
        data["check_document"] = "on"
    return cl.post(f"/verify/{nid}/approve", data=data, follow_redirects=False)


try:
    va, ta = client_for("vf_a")
    vb, tb = client_for("vf_b")
    others = {u: client_for(u) for u in ("vf_req", "vf_s", "vf_an", "vf_ad")}
    anon = TestClient(app, base_url="http://127.0.0.1:8000")
    anon_tok = csrf_from(anon.get("/login").text)
    n1 = new_need()

    # ---- access
    bad = []
    for u, (cl, tok) in others.items():
        for s in (cl.get("/verify").status_code, cl.get(f"/verify/{n1}").status_code,
                  cl.post(f"/verify/{n1}/approve", data={"public_text": GOOD_TEXT, "check_payee": "on", "check_document": "on", "csrf": tok}).status_code,
                  cl.post(f"/verify/{n1}/reject", data={"note": "A reason that is long enough.", "csrf": tok}).status_code):
            if s != 403:
                bad.append(f"{u} got {s}")
    check("requesters, supporters, analysts and admins are refused (403) on every verifier route" + ((": " + "; ".join(bad)) if bad else ""), not bad)
    anon_codes = [anon.get("/verify").status_code, anon.get(f"/verify/{n1}").status_code,
                  anon.post(f"/verify/{n1}/approve", data={"public_text": GOOD_TEXT, "csrf": anon_tok}).status_code,
                  anon.post(f"/verify/{n1}/reject", data={"note": "A reason that is long enough.", "csrf": anon_tok}).status_code]
    check("anonymous visitors get 401 on every verifier route: " + str(anon_codes), all(s == 401 for s in anon_codes))
    check("after all those refusals the need is still pending and untouched", status_of(n1) == "pending" and events_of(n1, "approved") == 0)

    # ---- queue and private details
    decided = new_need(pu="food", g=3000)
    with admin.begin() as c:
        c.execute(text("UPDATE needs SET status = 'rejected', review_note = 'decided earlier' WHERE id = :n"), {"n": decided})
    q = va.get("/verify")
    check("the queue lists pending needs with purpose, place and goal, and leaves out decided ones",
          q.status_code == 200 and f"/verify/{n1}" in q.text and "Rent and housing" in q.text and ISTANBUL in q.text and "48,000 TRY" in q.text
          and f"/verify/{decided}" not in q.text)
    d = va.get(f"/verify/{n1}")
    check("a verifier sees the private details: household size, description, contact, account holder and the IBAN in groups",
          d.status_code == 200 and "The landlord is ending the lease" in d.text and CONTACT in d.text and "Ayse Demir" in d.text
          and "TR33 0006 1005 1978 6457 8413 26" in d.text and "Household size" in d.text)
    check("the requester's history is shown", "Requester's earlier needs" in d.text and "1 rejected" in d.text)
    check("a need that does not exist gives 404", va.get("/verify/99999999").status_code == 404)

    # ---- the two checks
    outcomes = [approve(va, ta, n1, payee=p, doc=dd) for p, dd in ((True, False), (False, True), (False, False))]
    check("approval is refused (400) unless both checks are ticked, and the need stays pending: " + str([o.status_code for o in outcomes]),
          all(o.status_code == 400 and "Tick both checks" in o.text for o in outcomes) and status_of(n1) == "pending")

    # ---- the public-text guard
    unsafe = [("too short", "Short.", "must be 10 to 300"), ("too long", "x" * 301, "must be 10 to 300"),
              ("an email address", "Write to someone at a@b.co for details please", "email address or a link"),
              ("a link", "See http://example.com/help for the details here", "email address or a link"),
              ("a web address", "More at www.example.com about the whole story", "email address or a link"),
              ("a phone number", "Call 0532 123 45 67 for the full details", "phone number or an account number"),
              ("an account number", "Send to 33000610051978 for the family", "phone number or an account number"),
              ("the requester's contact details", f"Ask about this at {CONTACT} for more", "requester's contact details"),
              ("part of the account holder's name", "The family of Ayse needs help with a deposit", "part of the account holder's name"),
              ("the requester's username", "Message from vf_req about the rent for a flat", "requester's username")]
    for label, t_, expect in unsafe:
        r = approve(va, ta, n1, public_text=t_)
        check(f"refused: public text with {label} (400, need stays pending)", r.status_code == 400 and expect in html.unescape(r.text) and status_of(n1) == "pending")
    r = approve(va, ta, n1, public_text="Call 0532 123 45 67 now, keep this typed text")
    check("a refused approval gives the typed text and the ticks back", r.status_code == 400 and "keep this typed text" in r.text and r.text.count("checked") >= 2)

    # ---- approve
    r = approve(va, ta, n1, public_text="  " + GOOD_TEXT + "  ")
    row = rows("SELECT status, public_text, payee_verified, reviewed_by, review_note FROM needs WHERE id = :n", n=n1)[0]
    check("a valid approval: 303, published, text stored trimmed, payee verified, reviewer recorded",
          r.status_code == 303 and r.headers["location"] == "/verify?done=approved"
          and row == ("published", GOOD_TEXT, True, uid["vf_a"], None))
    ev = rows("SELECT actor_id, note FROM need_events WHERE need_id = :n AND event = 'approved'", n=n1)
    check("the approval event records who and that both checks were done",
          len(ev) == 1 and ev[0][0] == uid["vf_a"] and "name matches" in ev[0][1] and "supporting document" in ev[0][1])
    pub = rows("SELECT public_text, goal, currency FROM public_needs WHERE id = :n", n=n1)
    check("the approved need is on the public board view with only the public text", pub == [(GOOD_TEXT, 48000, "TRY")])
    check("the queue no longer lists it and says so", f"/verify/{n1}" not in va.get("/verify?done=approved").text and "Approved and published" in va.get("/verify?done=approved").text)

    # ---- a second decision
    second = approve(vb, tb, n1)
    rej = vb.post(f"/verify/{n1}/reject", data={"note": "Trying to reject it later.", "csrf": tb})
    check("a second decision on a published need gets 409 and changes nothing",
          second.status_code == 409 and rej.status_code == 409 and status_of(n1) == "published" and events_of(n1, "approved") == 1
          and events_of(n1, "rejected") == 0)

    # ---- reject
    n2 = new_need(g=9500, pu="education")
    short = va.post(f"/verify/{n2}/reject", data={"note": "no", "csrf": ta})
    check("a rejection without a real reason is refused (400) and the need stays pending",
          short.status_code == 400 and "10 to 500" in short.text and status_of(n2) == "pending")
    check("rejecting with a forged CSRF token is refused (403)",
          va.post(f"/verify/{n2}/reject", data={"note": "A reason that is long enough.", "csrf": "forged"}).status_code == 403
          and status_of(n2) == "pending")
    check("approving with a forged CSRF token is refused (403)",
          va.post(f"/verify/{n2}/approve", data={"public_text": GOOD_TEXT, "check_payee": "on", "check_document": "on", "csrf": "forged"}).status_code == 403
          and status_of(n2) == "pending")
    r = va.post(f"/verify/{n2}/reject", data={"note": "The document does not match the amount.", "csrf": ta}, follow_redirects=False)
    check("a valid rejection: 303, status rejected, reason and reviewer stored, event written",
          r.status_code == 303 and rows("SELECT status, review_note, reviewed_by FROM needs WHERE id = :n", n=n2) == [("rejected", "The document does not match the amount.", uid["vf_a"])]
          and events_of(n2, "rejected") == 1)
    check("the requester sees the verifier's reason on their page", "Verifier note: The document does not match the amount." in others["vf_req"][0].get("/needs/mine").text)

    # ---- a verifier cannot decide their own need
    own = new_need(requester="vf_a")
    check("a verifier cannot approve or reject a need they posted themselves (409, still pending)",
          approve(va, ta, own).status_code == 409 and va.post(f"/verify/{own}/reject", data={"note": "Rejecting my own need.", "csrf": ta}).status_code == 409
          and status_of(own) == "pending")

    # ---- decisions that race
    n3 = new_need(g=7000)
    cl = [client_for(u) for u in ("vf_a", "vf_b") * 3]
    barrier, out, lock = threading.Barrier(6), [], threading.Lock()

    def worker(i, c_, t_):
        barrier.wait(timeout=30)
        r = approve(c_, t_, n3) if i % 2 == 0 else c_.post(f"/verify/{n3}/reject", data={"note": "A reason that is long enough.", "csrf": t_}, follow_redirects=False)
        with lock:
            out.append(r.status_code)

    ts = [threading.Thread(target=worker, args=(i, *c_)) for i, c_ in enumerate(cl)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    decided_events = events_of(n3, "approved") + events_of(n3, "rejected")
    check(f"6 simultaneous decisions by 2 verifiers on one need: {out.count(303)} accepted, {out.count(409)} refused, {decided_events} event written",
          out.count(303) == 1 and out.count(409) == 5 and decided_events == 1 and status_of(n3) in ("published", "rejected"))

    # ---- the look-again list
    w1, w2 = new_need(g=40000, pu="debt"), new_need(g=5000, pu="food")
    with admin.begin() as c:
        for w in (w1, w2):
            c.execute(text("UPDATE needs SET status = 'published', public_text = 'Help.', payee_verified = true WHERE id = :n"), {"n": w})
        pid = c.execute(text("INSERT INTO pledges (need_id, supporter_id, amount_try, ack) VALUES (:n, :s, 6000, true) RETURNING id"),
                        {"n": w1, "s": uid["vf_s"]}).scalar_one()
        c.execute(text("UPDATE pledges SET status = 'sent' WHERE id = :i"), {"i": pid})
        c.execute(text("UPDATE pledges SET status = 'confirmed' WHERE id = :i"), {"i": pid})
        for w, why in ((w1, "The requester settled the debt with a family loan."), (w2, "I no longer need the money at all.")):
            c.execute(text("UPDATE needs SET status = 'withdrawn', withdrawn_reason = :r WHERE id = :n"), {"r": why, "n": w})
    q = va.get("/verify").text
    check("a need withdrawn after money was confirmed is on the look-again list with the amount and reason",
          f"/verify/{w1}" in q and "6,000 TRY" in q and "settled the debt with a family loan" in q)
    check("a need withdrawn with no confirmed money is not on the list", f"/verify/{w2}" not in q)
    d = va.get(f"/verify/{w1}")
    check("its page shows the withdrawal reason and no decision forms",
          d.status_code == 200 and "Withdrawn because" in d.text and "already been decided" in d.text and "check_payee" not in d.text)
finally:
    cleanup()
    left = one("SELECT count(*) FROM users WHERE left(username, 3) = 'vf_'")
    print(f"test rows removed (users left with the vf_ prefix: {left})")

assert not fails, f"failed: {fails}"
print(f"PASS: {total} checks")
