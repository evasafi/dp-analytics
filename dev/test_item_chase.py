"""Part 6b: handed-over offers the requester has not answered. The verifier queue lists them, and a verifier
confirms on the requester's behalf or opens a dispute."""
import getpass
import re
import sys

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.auth import hash_password
from app.config import settings
from app.districts import DISTRICTS_BY_PROVINCE
from app.main import app

admin = create_engine(make_url(settings.database_url).set(username="postgres", password=getpass.getpass("postgres password: ")))
PW = "Itw-test-pass-1234"
PREFIX = "itw_"
BASE = "http://127.0.0.1:8000"
fails = 0
total = 0


def check(label, ok, info=""):
    global fails, total
    total += 1
    if not ok:
        fails += 1
    print("%s%02d. %s%s" % ("ok   " if ok else "FAIL ", total, label, "" if ok or info == "" else "  [" + str(info) + "]"))


def csrf_from(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def one(sql, **p):
    with admin.connect() as c:
        return c.execute(text(sql), p).first()


def run(sql, **p):
    with admin.begin() as c:
        r = c.execute(text(sql), p)
        return r.scalar_one() if r.returns_rows else None


def cleanup():
    sub = "(SELECT id FROM needs WHERE requester_id IN (SELECT id FROM users WHERE username LIKE :p))"
    with admin.begin() as c:
        c.execute(text("DELETE FROM item_claims WHERE need_id IN " + sub), {"p": PREFIX + "%"})
        c.execute(text("DELETE FROM need_events WHERE need_id IN " + sub), {"p": PREFIX + "%"})
        c.execute(text("DELETE FROM needs WHERE requester_id IN (SELECT id FROM users WHERE username LIKE :p)"), {"p": PREFIX + "%"})
        c.execute(text("DELETE FROM login_failures WHERE username LIKE :p"), {"p": PREFIX + "%"})
        c.execute(text("DELETE FROM users WHERE username LIKE :p"), {"p": PREFIX + "%"})


def make_user(u, role):
    return run("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r) RETURNING id", u=u, h=hash_password(PW), r=role)


def client_for(u):
    cl = TestClient(app, base_url=BASE)
    tok = csrf_from(cl.get("/login").text)
    code = cl.post("/login", data={"username": u, "password": PW, "csrf": tok}, follow_redirects=False).status_code
    assert code == 303, "login for %s gave %s" % (u, code)
    return cl


prov = next(iter(DISTRICTS_BY_PROVINCE))
dist = DISTRICTS_BY_PROVINCE[prov][0]
bed_id = one("SELECT id FROM item_types WHERE enabled AND category = 'bedding' ORDER BY id LIMIT 1")[0]


def new_need(requester_id):
    nid = run("INSERT INTO needs (requester_id, need_kind, item_type_id, quantity_needed, used_ok, handover_method, province, "
              "district, household_size, description, contact_method, contact) VALUES (:u, 'item', :t, 1, true, 'public_place', "
              ":p, :d, 3, 'ITW test need for review', 'email', 'itw_req_contact@example.com') RETURNING id",
              u=requester_id, t=bed_id, p=prov, d=dist)
    run("UPDATE needs SET status = 'published', public_text = 'Public text for the nudge tests' WHERE id = :i", i=nid)
    return nid


def sent_claim(nid, donor_id, tag, days_ago):
    cid = run("INSERT INTO item_claims (need_id, donor_id, units, ack, contact_method, contact) "
              "VALUES (:n, :d, 1, true, 'email', :c) RETURNING id", n=nid, d=donor_id, c="itw_%s@example.com" % tag)
    run("UPDATE item_claims SET released_at = now(), released_by = :v WHERE id = :c", v=VER, c=cid)
    run("UPDATE item_claims SET status = 'sent' WHERE id = :c", c=cid)
    if days_ago:
        run("UPDATE item_claims SET sent_at = now() - make_interval(days => :d) WHERE id = :c", d=days_ago, c=cid)
    return cid


def claim_status(cid):
    return one("SELECT status FROM item_claims WHERE id = :c", c=cid)[0]


def need_status(nid):
    return one("SELECT status FROM needs WHERE id = :i", i=nid)[0]


def ev(name, cid):
    return one("SELECT note FROM need_events WHERE event = :e AND (note = :a OR starts_with(note, :b)) ORDER BY id DESC LIMIT 1",
               e=name, a="claim %d" % cid, b="claim %d: " % cid)


def chase(cl, cid, token, outcome, reason="The requester told me by phone"):
    return cl.post("/verify/claims/%d/chase" % cid, data={"csrf": token, "outcome": outcome, "reason": reason}, follow_redirects=False)


cleanup()
try:
    VER = make_user("itw_ver", "verifier")
    req = make_user("itw_req", "requester")
    d1, d2, d3 = (make_user("itw_d%d" % i, "supporter") for i in (1, 2, 3))
    W1, W2, W3 = new_need(req), new_need(req), new_need(req)
    K1 = sent_claim(W1, d1, "k1", 60)
    K2 = sent_claim(W2, d2, "k2", 60)
    K3 = sent_claim(W3, d3, "k3", 0)
    v, r, s = client_for("itw_ver"), client_for("itw_req"), client_for("itw_d1")
    tv, tr, ts = csrf_from(v.get("/verify").text), csrf_from(r.get("/needs/mine").text), csrf_from(s.get("/support").text)

    q = v.get("/verify").text
    check("the queue lists the two overdue offers and not the fresh one",
          'data-waiting="%d"' % K1 in q and 'data-waiting="%d"' % K2 in q and 'data-waiting="%d"' % K3 not in q)
    check("an overdue offer's page has the chase form, a fresh one does not",
          "/verify/claims/%d/chase" % K1 in v.get("/verify/claims/%d" % K1).text and "/verify/claims/%d/chase" % K3 not in v.get("/verify/claims/%d" % K3).text)
    check("chasing a fresh offer gives 409 and changes nothing", chase(v, K3, tv, "confirm").status_code == 409 and claim_status(K3) == "sent")
    check("a requester and a supporter get 403, a forged csrf gets 403, and nothing changes",
          chase(r, K1, tr, "confirm").status_code == 403 and chase(s, K1, ts, "confirm").status_code == 403
          and chase(v, K1, "forged", "confirm").status_code == 403 and claim_status(K1) == "sent")
    check("a short reason gives 400 and an unknown outcome gives 400",
          chase(v, K1, tv, "confirm", "short").status_code == 400 and chase(v, K1, tv, "maybe").status_code == 400 and claim_status(K1) == "sent")
    resp = chase(v, K1, tv, "confirm")
    note = ev("claim_confirmed", K1)
    check("confirm on the requester's behalf: 303, confirmed, event says a verifier did it, the need is fulfilled",
          resp.status_code == 303 and resp.headers.get("location") == "/verify?done=chased" and claim_status(K1) == "confirmed"
          and note is not None and "verifier" in note.note and need_status(W1) == "fulfilled", resp.status_code)
    check("chasing it again gives 409", chase(v, K1, tv, "confirm").status_code == 409)
    resp = chase(v, K2, tv, "dispute", "I could not reach the requester")
    note = ev("claim_disputed", K2)
    check("open a dispute: 303, disputed, event with the reason",
          resp.status_code == 303 and claim_status(K2) == "disputed" and note is not None and "could not reach" in note.note, resp.status_code)
    q = v.get("/verify").text
    check("the disputed offer now shows in the disputes section and no longer in the waiting section",
          'data-dispute="%d"' % K2 in q and 'data-waiting="%d"' % K2 not in q)
    check("the reason shows on the claim page", "could not reach the requester" in v.get("/verify/claims/%d" % K2).text)
    resp = v.post("/verify/claims/%d/settle" % K2, data={"csrf": tv, "outcome": "cancel", "reason": "The donor could not show the handover"}, follow_redirects=False)
    check("the existing settle flow then works on it", resp.status_code == 303 and claim_status(K2) == "cancelled")
    check("the chased message shows", "chased" not in v.get("/verify?done=chased").text.lower() or "Offer updated" in v.get("/verify?done=chased").text)
finally:
    cleanup()

print("\n%d of %d checks passed" % (total - fails, total))
sys.exit(1 if fails else 0)
