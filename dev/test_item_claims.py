"""Part 5: donor offers on item needs, contact release by a verifier, decline, cancel and expiry."""
import getpass
import re
import sys

from fastapi.testclient import TestClient
from markupsafe import escape
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.auth import hash_password
from app.config import settings
from app.districts import DISTRICTS_BY_PROVINCE
from app.main import app

admin = create_engine(make_url(settings.database_url).set(username="postgres", password=getpass.getpass("postgres password: ")))
PW = "Itc-test-pass-1234"
PREFIX = "itc_"
BASE = "http://127.0.0.1:8000"
D1, D2, RC = "itc_donor1@example.com", "itc_donor2@example.com", "itc_req_contact@example.com"
fails = 0
total = 0


def check(label, ok):
    global fails, total
    total += 1
    if not ok:
        fails += 1
    print(("ok   " if ok else "FAIL ") + label)


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
bed_id, bed_name = one("SELECT id, name FROM item_types WHERE enabled AND category = 'bedding' ORDER BY id LIMIT 1")


def new_item_need(requester_id, qty, publish):
    nid = run("INSERT INTO needs (requester_id, need_kind, item_type_id, quantity_needed, used_ok, handover_method, province, "
              "district, household_size, description, contact_method, contact) VALUES (:u, 'item', :t, :q, true, 'public_place', "
              ":p, :d, 3, 'ITC test need for review', 'email', :c) RETURNING id", u=requester_id, t=bed_id, q=qty, p=prov, d=dist, c=RC)
    if publish:
        run("UPDATE needs SET status = 'published', public_text = 'Public text for the offer tests' WHERE id = :i", i=nid)
    return nid


def post_offer(cl, nid, token, **over):
    data = {"units": "1", "ack": "on", "contact_method": "email", "contact": D1, "csrf": token, **over}
    return cl.post("/support/item/%d" % nid, data=data, follow_redirects=False)


def n_claims(donor_id):
    return one("SELECT count(*) FROM item_claims WHERE donor_id = :d", d=donor_id)[0]


def claim(cid):
    return one("SELECT status, units, ack, released_at, released_by FROM item_claims WHERE id = :c", c=cid)


def events(nid, name):
    return one("SELECT count(*) FROM need_events WHERE need_id = :n AND event = :e", n=nid, e=name)[0]


cleanup()
orig_max = one("SELECT max_unconfirmed_pledges FROM settings")[0]
try:
    ver = make_user("itc_ver", "verifier")
    req = make_user("itc_req", "requester")
    req2 = make_user("itc_req2", "requester")
    s1, s2, s3 = (make_user("itc_sup%d" % i, "supporter") for i in (1, 2, 3))
    N = new_item_need(req, 3, True)
    P = new_item_need(req, 1, False)
    v, r, r2 = client_for("itc_ver"), client_for("itc_req"), client_for("itc_req2")
    c1, c2, c3 = client_for("itc_sup1"), client_for("itc_sup2"), client_for("itc_sup3")
    anon = TestClient(app, base_url=BASE)
    t1, t2, t3 = (csrf_from(c.get("/support/item/%d" % N).text) for c in (c1, c2, c3))
    tv = csrf_from(v.get("/verify").text)

    print("-- the offer form")
    page = c1.get("/support/item/%d" % N)
    check("1. the form loads with the item, the open units and the notice",
          page.status_code == 200 and str(escape(bed_name)) in page.text and "Still open: <strong>3</strong>" in page.text
          and "passes your contact to the requester" in page.text)
    check("2. visitor 401, requester 403, verifier 403",
          anon.get("/support/item/%d" % N).status_code == 401 and r.get("/support/item/%d" % N).status_code == 403
          and v.get("/support/item/%d" % N).status_code == 403)
    bad = [{"units": "0"}, {"units": "4"}, {"units": "two"}, {"units": ""}, {"ack": ""}, {"contact": "not-an-email"}]
    check("3. bad units, a missing tick or a bad contact give 400 and save nothing",
          all(post_offer(c1, N, t1, **b).status_code == 400 for b in bad) and n_claims(s1) == 0)
    check("4. a forged csrf gives 403 and saves nothing", post_offer(c1, N, "forged").status_code == 403 and n_claims(s1) == 0)

    print("-- offering")
    resp = post_offer(c1, N, t1, units="2")
    check("5. a valid offer redirects to /support?done=claimed", resp.status_code == 303 and resp.headers.get("location") == "/support?done=claimed")
    cid1 = one("SELECT id FROM item_claims WHERE donor_id = :d", d=s1)[0]
    row = claim(cid1)
    exp = one("SELECT expires_at > now() + interval '6 days' AND expires_at < now() + interval '8 days' FROM item_claims WHERE id = :c", c=cid1)[0]
    check("6. saved as pledged, 2 units, unreleased, expiring in about 7 days, with a 'claimed' event",
          row.status == "pledged" and row.units == 2 and row.ack is True and row.released_at is None and exp and events(N, "claimed") == 1)
    mine = c1.get("/support?done=claimed").text
    check("7. the donor's page shows the offer, its status and a cancel button",
          str(escape(bed_name)) in mine and "Offered. A verifier will pass your contact" in mine and "/support/claims/%d/cancel" % cid1 in mine)
    check("8. offering more than is left gives 400", post_offer(c2, N, t2, units="2", contact=D2).status_code == 400 and n_claims(s2) == 0)
    check("9. a second donor offers the last unit", post_offer(c2, N, t2, units="1", contact=D2).status_code == 303)
    cid2 = one("SELECT id FROM item_claims WHERE donor_id = :d", d=s2)[0]
    check("10. the board counts the units", "Nothing confirmed yet, 3 promised, 2 donors." in anon.get("/board/%d" % N).text)
    full = c3.get("/support/item/%d" % N)
    check("11. a third donor sees the need is fully offered and gets no form",
          full.status_code == 200 and "fully offered" in full.text and '<form method="post" action="/support/item/' not in full.text)
    money = one("SELECT id FROM public_needs WHERE need_kind = 'money' ORDER BY id LIMIT 1")
    check("12. an unpublished need and a money need give 404",
          c1.get("/support/item/%d" % P).status_code == 404 and (money is None or c1.get("/support/item/%d" % money.id).status_code == 404))

    print("-- before release")
    rp = r.get("/needs/mine").text
    check("13. the requester sees no donor contact and no donor name yet", D1 not in rp and D2 not in rp and "itc_sup1" not in rp)
    q = v.get("/verify").text
    check("14. the verifier queue lists both offers", "/verify/claims/%d" % cid1 in q and "/verify/claims/%d" % cid2 in q)
    kp = v.get("/verify/claims/%d" % cid1)
    check("15. the claim page shows both contacts, the donor and the preferred handover",
          kp.status_code == 200 and D1 in kp.text and RC in kp.text and "itc_sup1" in kp.text and "Meet in a public place" in kp.text)
    check("16. a requester and a supporter get 403, a visitor 401, on the claim page",
          r.get("/verify/claims/%d" % cid1).status_code == 403 and c1.get("/verify/claims/%d" % cid1).status_code == 403
          and anon.get("/verify/claims/%d" % cid1).status_code == 401)

    print("-- release")
    resp = v.post("/verify/claims/%d/release" % cid1, data={"csrf": "forged"}, follow_redirects=False)
    check("17. a forged csrf gives 403 and nothing is released", resp.status_code == 403 and claim(cid1).released_at is None)
    run("UPDATE item_claims SET expires_at = now() + interval '1 day' WHERE id = :c", c=cid1)
    resp = v.post("/verify/claims/%d/release" % cid1, data={"csrf": tv}, follow_redirects=False)
    row = claim(cid1)
    exp = one("SELECT expires_at > now() + interval '6 days' FROM item_claims WHERE id = :c", c=cid1)[0]
    check("18. release: 303, released by the verifier, the 7 days restart, event recorded",
          resp.status_code == 303 and resp.headers.get("location") == "/verify?done=released" and row.released_by == ver
          and row.released_at is not None and exp and events(N, "claim_released") == 1)
    resp = v.post("/verify/claims/%d/release" % cid1, data={"csrf": tv}, follow_redirects=False)
    check("19. releasing again gives 409 and adds no event", resp.status_code == 409 and events(N, "claim_released") == 1)
    rp = r.get("/needs/mine").text
    check("20. the requester now sees donor 1's contact and not donor 2's", D1 in rp and "2" in rp and D2 not in rp)
    check("21. another requester does not see it", D1 not in r2.get("/needs/mine").text)
    p1, p2 = c1.get("/support").text, c2.get("/support").text
    check("22. donor 1 sees that the contact was passed on and the preferred handover; donor 2 does not",
          "Your contact was passed to the requester" in p1 and "Meet in a public place" in p1 and "Meet in a public place" not in p2)

    print("-- decline")
    resp = v.post("/verify/claims/%d/decline" % cid2, data={"csrf": tv, "reason": "short"}, follow_redirects=False)
    check("23. a short reason gives 400 and the offer stays pledged", resp.status_code == 400 and claim(cid2).status == "pledged")
    reason = "Could not confirm the donor."
    resp = v.post("/verify/claims/%d/decline" % cid2, data={"csrf": tv, "reason": reason}, follow_redirects=False)
    note = one("SELECT note FROM need_events WHERE need_id = :n AND event = 'claim_declined'", n=N)
    check("24. a good reason: 303, cancelled, 'claim_declined' event with the reason",
          resp.status_code == 303 and resp.headers.get("location") == "/verify?done=declined" and claim(cid2).status == "cancelled"
          and note is not None and note.note.startswith("claim %d: " % cid2))
    p2 = c2.get("/support").text
    check("25. donor 2 sees it was declined and why; the requester never saw donor 2's contact",
          "Declined by a verifier" in p2 and reason in p2 and D2 not in r.get("/needs/mine").text)
    resp = v.post("/verify/claims/%d/release" % cid2, data={"csrf": tv}, follow_redirects=False)
    check("26. a declined offer cannot be released", resp.status_code == 409)

    print("-- cancel")
    check("27. another donor cannot cancel it", c2.post("/support/claims/%d/cancel" % cid1, data={"csrf": t2}, follow_redirects=False).status_code == 404)
    resp = c1.post("/support/claims/%d/cancel" % cid1, data={"csrf": t1}, follow_redirects=False)
    check("28. the donor cancels: 303, cancelled, event recorded",
          resp.status_code == 303 and resp.headers.get("location") == "/support?done=claim_cancelled"
          and claim(cid1).status == "cancelled" and events(N, "claim_cancelled") == 1)
    check("29. the requester no longer sees the donor's contact", D1 not in r.get("/needs/mine").text)
    check("30. cancelling again gives 409", c1.post("/support/claims/%d/cancel" % cid1, data={"csrf": t1}, follow_redirects=False).status_code == 409)
    check("31. the units are free again on the board", "No pledges yet." in anon.get("/board/%d" % N).text)

    print("-- expiry, database guard, limit")
    check("32a. a third donor offers", post_offer(c3, N, t3, units="1", contact="itc_donor3@example.com").status_code == 303)
    cid3 = one("SELECT id FROM item_claims WHERE donor_id = :d", d=s3)[0]
    run("UPDATE item_claims SET expires_at = now() - interval '1 hour' WHERE id = :c", c=cid3)
    p3 = c3.get("/support").text
    check("32. an offer past its date shows as expired", claim(cid3).status == "expired" and "Expired" in p3)
    try:
        run("UPDATE item_claims SET released_at = now(), released_by = :v WHERE id = :c", v=ver, c=cid2)
        guard = False
    except Exception as e:
        guard = "only a pledged claim" in str(e)
    check("33. the database refuses to release a claim that is not pledged", guard)
    run("UPDATE settings SET max_unconfirmed_pledges = 1")
    first = post_offer(c3, N, t3, units="1", contact="itc_donor3@example.com")
    second = post_offer(c3, N, t3, units="1", contact="itc_donor3@example.com")
    check("34. the unconfirmed-claims limit gives 429", first.status_code == 303 and second.status_code == 429)
    check("35. the form for a need that is not open to this donor still renders after errors", c3.get("/support/item/%d" % N).status_code == 200)
finally:
    run("UPDATE settings SET max_unconfirmed_pledges = :m", m=orig_max)
    cleanup()

print("\n%d of %d checks passed" % (total - fails, total))
sys.exit(1 if fails else 0)
