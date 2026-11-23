"""Handover without photo proof: the donor says the items were handed over, the requester confirms or disputes,
a verifier settles, and the need is fulfilled when the confirmed units reach the quantity."""
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
PW = "Ith-test-pass-1234"
PREFIX = "ith_"
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


def new_need(requester_id, qty):
    nid = run("INSERT INTO needs (requester_id, need_kind, item_type_id, quantity_needed, used_ok, handover_method, province, "
              "district, household_size, description, contact_method, contact) VALUES (:u, 'item', :t, :q, true, 'public_place', "
              ":p, :d, 3, 'ITH test need for review', 'email', 'ith_req_contact@example.com') RETURNING id",
              u=requester_id, t=bed_id, q=qty, p=prov, d=dist)
    run("UPDATE needs SET status = 'published', public_text = 'Public text for the handover tests' WHERE id = :i", i=nid)
    return nid


def make_claim(nid, donor_id, tag, release=True):
    cid = run("INSERT INTO item_claims (need_id, donor_id, units, ack, contact_method, contact) "
              "VALUES (:n, :d, 1, true, 'email', :c) RETURNING id", n=nid, d=donor_id, c="ith_%s@example.com" % tag)
    if release:
        run("UPDATE item_claims SET released_at = now(), released_by = :v WHERE id = :c", v=VER, c=cid)
    return cid


def claim_row(cid):
    return one("SELECT status, sent_at FROM item_claims WHERE id = :c", c=cid)


def need_status(nid):
    return one("SELECT status FROM needs WHERE id = :i", i=nid)[0]


def ev(name, cid):
    return one("SELECT count(*) FROM need_events WHERE event = :e AND (note = :a OR starts_with(note, :b))",
               e=name, a="claim %d" % cid, b="claim %d: " % cid)[0]


def url_h(cid):
    return "/support/claims/%d/handover" % cid


def hand(cl, cid, token, ack=True):
    data = {"csrf": token}
    if ack:
        data["ack"] = "on"
    return cl.post(url_h(cid), data=data, follow_redirects=False)


cleanup()
try:
    VER = make_user("ith_ver", "verifier")
    req = make_user("ith_req", "requester")
    req2 = make_user("ith_req2", "requester")
    d = {i: make_user("ith_d%d" % i, "supporter") for i in (1, 2, 3)}
    N, F, H = new_need(req, 3), new_need(req, 1), new_need(req, 1)
    C1, C2 = make_claim(N, d[1], "c1"), make_claim(N, d[2], "c2")
    C3 = make_claim(N, d[3], "c3", release=False)
    CF, CH = make_claim(F, d[1], "cf"), make_claim(H, d[2], "ch")
    v, r, r2 = client_for("ith_ver"), client_for("ith_req"), client_for("ith_req2")
    c = {i: client_for("ith_d%d" % i) for i in (1, 2, 3)}
    anon = TestClient(app, base_url=BASE)
    tv, tr, tr2 = (csrf_from(x.get(p).text) for x, p in ((v, "/verify"), (r, "/needs/mine"), (r2, "/needs/mine")))
    td = {i: csrf_from(c[i].get("/support").text) for i in c}

    print("-- the handover page")
    page = c[1].get(url_h(C1))
    check("the page has a confirmation box, no file field, and says the site keeps no proof",
          page.status_code == 200 and 'name="ack"' in page.text and 'type="file"' not in page.text and "keeps no proof" in page.text, page.status_code)
    check("another donor gets 404, a requester 403, a visitor 401",
          c[2].get(url_h(C1)).status_code == 404 and r.get(url_h(C1)).status_code == 403 and anon.get(url_h(C1)).status_code == 401)
    check("an offer whose contact was not released yet gives 409 and stays pledged",
          c[3].get(url_h(C3)).status_code == 409 and hand(c[3], C3, td[3]).status_code == 409 and claim_row(C3).status == "pledged")
    check("a forged csrf gives 403 and nothing changes", hand(c[1], C1, "forged").status_code == 403 and claim_row(C1).status == "pledged")
    check("without the tick it gives 400 and the offer stays pledged", hand(c[1], C1, td[1], ack=False).status_code == 400 and claim_row(C1).status == "pledged")

    print("-- the donor says it was handed over")
    resp = hand(c[1], C1, td[1])
    row = claim_row(C1)
    check("303 to /support?done=claim_sent, claim sent, sent_at set, event recorded",
          resp.status_code == 303 and resp.headers.get("location") == "/support?done=claim_sent" and row.status == "sent"
          and row.sent_at is not None and ev("claim_sent", C1) == 1, resp.status_code)
    check("a repeat gives 409 and another donor gets 404", hand(c[1], C1, td[1]).status_code == 409 and hand(c[2], C1, td[2]).status_code == 404)
    sp = c[1].get("/support?done=claim_sent").text
    check("the donor's page shows the banner and the waiting-for-requester status", "Marked as handed over" in sp and "Waiting for the requester to confirm" in sp)

    print("-- the requester answers")
    mine = r.get("/needs/mine").text
    check("the requester sees the donor's contact and both answer forms, and no photo link",
          "ith_c1@example.com" in mine and "/needs/offers/%d/confirm" % C1 in mine and "/needs/offers/%d/dispute" % C1 in mine and "/photo" not in mine)
    check("another requester cannot confirm (404) and a donor cannot (403)",
          r2.post("/needs/offers/%d/confirm" % C1, data={"csrf": tr2}, follow_redirects=False).status_code == 404
          and c[1].post("/needs/offers/%d/confirm" % C1, data={"csrf": td[1]}, follow_redirects=False).status_code == 403)
    check("a forged csrf gives 403 and the offer stays sent",
          r.post("/needs/offers/%d/confirm" % C1, data={"csrf": "forged"}, follow_redirects=False).status_code == 403 and claim_row(C1).status == "sent")
    resp = r.post("/needs/offers/%d/confirm" % C1, data={"csrf": tr}, follow_redirects=False)
    mine = r.get("/needs/mine").text
    check("confirm: 303, confirmed, event recorded, the donor's contact is hidden",
          resp.status_code == 303 and claim_row(C1).status == "confirmed" and ev("claim_confirmed", C1) == 1 and "ith_c1@example.com" not in mine, resp.status_code)
    check("confirming again gives 409", r.post("/needs/offers/%d/confirm" % C1, data={"csrf": tr}, follow_redirects=False).status_code == 409)
    check("the need stays published while only part of it is confirmed", need_status(N) == "published")
    check("a second donor says it was handed over", hand(c[2], C2, td[2]).status_code == 303 and claim_row(C2).status == "sent")
    check("a short dispute reason gives 400 and the offer stays sent",
          r.post("/needs/offers/%d/dispute" % C2, data={"csrf": tr, "reason": "no"}, follow_redirects=False).status_code == 400 and claim_row(C2).status == "sent")
    resp = r.post("/needs/offers/%d/dispute" % C2, data={"csrf": tr, "reason": "Nothing arrived at the agreed place"}, follow_redirects=False)
    check("a dispute: 303, disputed, event with the reason",
          resp.status_code == 303 and claim_row(C2).status == "disputed" and ev("claim_disputed", C2) == 1, resp.status_code)
    check("the donor sees that it is disputed", "Disputed" in c[2].get("/support").text)

    print("-- the verifier settles")
    check("the queue lists the disputed handover", 'data-dispute="%d"' % C2 in v.get("/verify").text)
    kp = v.get("/verify/claims/%d" % C2)
    check("the claim page shows the reason and the settle form, with no photo or waiver controls",
          kp.status_code == 200 and "Nothing arrived at the agreed place" in kp.text and "/verify/claims/%d/settle" % C2 in kp.text
          and "/photo" not in kp.text and "/waive" not in kp.text)
    check("a short reason gives 400, an unknown outcome gives 400, a requester gets 403, a forged csrf 403",
          v.post("/verify/claims/%d/settle" % C2, data={"csrf": tv, "outcome": "cancel", "reason": "no"}, follow_redirects=False).status_code == 400
          and v.post("/verify/claims/%d/settle" % C2, data={"csrf": tv, "outcome": "maybe", "reason": "A good enough reason"}, follow_redirects=False).status_code == 400
          and r.post("/verify/claims/%d/settle" % C2, data={"csrf": tr, "outcome": "cancel", "reason": "A good enough reason"}, follow_redirects=False).status_code == 403
          and v.post("/verify/claims/%d/settle" % C2, data={"csrf": "forged", "outcome": "cancel", "reason": "A good enough reason"}, follow_redirects=False).status_code == 403
          and claim_row(C2).status == "disputed")
    resp = v.post("/verify/claims/%d/settle" % C2, data={"csrf": tv, "outcome": "cancel", "reason": "The donor could not show the handover"}, follow_redirects=False)
    check("settle against the donor: 303, cancelled, event recorded",
          resp.status_code == 303 and resp.headers.get("location") == "/verify?done=settled" and claim_row(C2).status == "cancelled" and ev("claim_settled", C2) == 1, resp.status_code)
    check("settling again gives 409",
          v.post("/verify/claims/%d/settle" % C2, data={"csrf": tv, "outcome": "cancel", "reason": "The donor could not show the handover"}, follow_redirects=False).status_code == 409)
    check("the donor sees it as cancelled", "Cancelled" in c[2].get("/support").text)

    print("-- fulfilment")
    hand(c[1], CF, td[1])
    r.post("/needs/offers/%d/confirm" % CF, data={"csrf": tr}, follow_redirects=False)
    check("confirming the last unit of a need fulfils the need", claim_row(CF).status == "confirmed" and need_status(F) == "fulfilled", need_status(F))
    check("a donor says it was handed over, the requester disputes, the verifier settles for the donor: the need is fulfilled",
          hand(c[2], CH, td[2]).status_code == 303
          and r.post("/needs/offers/%d/dispute" % CH, data={"csrf": tr, "reason": "I think it did not arrive"}, follow_redirects=False).status_code == 303
          and v.post("/verify/claims/%d/settle" % CH, data={"csrf": tv, "outcome": "confirm", "reason": "Both of them told me it happened"}, follow_redirects=False).status_code == 303
          and claim_row(CH).status == "confirmed" and need_status(H) == "fulfilled")

    print("-- the photo machinery is gone")
    check("no proofs table and no retention setting",
          one("SELECT to_regclass('handover_proofs') IS NULL")[0] is True
          and one("SELECT count(*) FROM information_schema.columns WHERE table_name = 'settings' AND column_name = 'photo_retention_days'")[0] == 0)
    check("the photo and waiver routes no longer exist",
          v.get("/verify/claims/%d/photo" % C1).status_code == 404 and r.get("/needs/offers/%d/photo" % C1).status_code == 404
          and v.post("/verify/claims/%d/waive" % C1, data={"csrf": tv, "reason": "A reason long enough"}, follow_redirects=False).status_code == 404)
finally:
    cleanup()

print("\n%d of %d checks passed" % (total - fails, total))
sys.exit(1 if fails else 0)
