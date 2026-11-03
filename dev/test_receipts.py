"""Receipts and disputes: the requester confirms or disputes a pledge marked sent, a verifier settles disputes, pledges
left unanswered are listed for follow-up, and confirmed money completes a need.

Run from the project root as a module: python -m dev.test_receipts
Needs the database owner's password (hidden prompt). The owner connection is built from the app's own
DP_DATABASE_URL and the test refuses to run against a database without the financial-needs schema.
Everything it creates has the username prefix rc_ and is removed at the end.
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
USERS = {"rc_req": "requester", "rc_req2": "requester", "rc_s": "supporter", "rc_v": "verifier", "rc_v2": "verifier", "rc_an": "analyst"}
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
    sub = "(SELECT id FROM users WHERE left(username, 3) = 'rc_')"
    mine = f"(SELECT id FROM needs WHERE requester_id IN {sub})"
    with admin.begin() as c:
        c.execute(text(f"DELETE FROM need_events WHERE actor_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM pledges WHERE supporter_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM needs WHERE requester_id IN {sub}"))
        c.execute(text("DELETE FROM users WHERE left(username, 3) = 'rc_'"))


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
IBAN, IBAN_SHOWN = "TR330006100519786457841326", "TR33 0006 1005 1978 6457 8413 26"
DONOR_CONTACT, REQ_CONTACT = "donor-rc@example.com", "requester-rc@example.com"


def make(goal, requester="rc_req"):
    with admin.begin() as c:
        nid = c.execute(text(
            "INSERT INTO needs (requester_id, purpose, currency, province, district, household_size, goal, description, "
            "contact_method, contact, payee_name, payee_details) VALUES (:r, 'rent_housing', 'TRY', 'Istanbul', :di, 3, :g, "
            "'Rent is overdue and the landlord wants it this month.', 'email', :co, 'Ayse Demir', :pd) RETURNING id"),
            {"r": uid[requester], "di": ISTANBUL, "g": goal, "co": REQ_CONTACT, "pd": IBAN}).scalar_one()
        c.execute(text("UPDATE needs SET status = 'published', public_text = 'Rent help.', payee_verified = true WHERE id = :i"), {"i": nid})
    return nid


def pledge(nid, amount, final="sent"):
    with admin.begin() as c:
        pid = c.execute(text("INSERT INTO pledges (need_id, supporter_id, amount_try, ack, contact_method, contact) "
                             "VALUES (:n, :s, :a, true, 'email', :d) RETURNING id"),
                        {"n": nid, "s": uid["rc_s"], "a": amount, "d": DONOR_CONTACT}).scalar_one()
        if final == "sent":
            c.execute(text("UPDATE pledges SET status = 'sent' WHERE id = :i"), {"i": pid})
    return pid


def pstat(pid):
    return one("SELECT status FROM pledges WHERE id = :p", p=pid)


def nstat(nid):
    return one("SELECT status FROM needs WHERE id = :n", n=nid)


def events(nid, kind):
    return one("SELECT count(*) FROM need_events WHERE need_id = :n AND event = :k", n=nid, k=kind)


def row_of(page, pid):
    m = re.search(rf'<tr data-pledge="{pid}">(.*?)</tr>', page, re.S)
    return m.group(1) if m else ""


def section(page, start, end):
    return page.split(start, 1)[1].split(end, 1)[0] if start in page else ""


def confirm(cl, tok, pid):
    return cl.post(f"/needs/pledges/{pid}/confirm", data={"csrf": tok}, follow_redirects=False)


def dispute(cl, tok, pid, reason="I checked my bank app twice and nothing came in."):
    return cl.post(f"/needs/pledges/{pid}/dispute", data={"reason": reason, "csrf": tok}, follow_redirects=False)


def resolve(cl, tok, pid, outcome="not_arrived", note="The donor could not show a transfer receipt."):
    return cl.post(f"/verify/pledges/{pid}/resolve", data={"outcome": outcome, "note": note, "csrf": tok}, follow_redirects=False)


try:
    board_routes._RATE.clear()
    rq, tq = client_for("rc_req")
    rq2, tq2 = client_for("rc_req2")
    sp, ts = client_for("rc_s")
    vf, tv = client_for("rc_v")
    vf2, tv2 = client_for("rc_v2")
    an, ta = client_for("rc_an")
    anon = TestClient(app, base_url="http://127.0.0.1:8000")
    n1 = make(1000)
    pA, pB, pC = pledge(n1, 400), pledge(n1, 300, "pledged"), pledge(n1, 300)

    # ---- access
    codes = [cl.post(f"/needs/pledges/{pA}/{k}", data={"csrf": t, "reason": "a long enough reason"}).status_code
             for cl, t in ((sp, ts), (vf, tv), (an, ta)) for k in ("confirm", "dispute")]
    codes += [cl.get(f"/verify/pledges/{pA}").status_code for cl in (rq, sp, an)]
    codes += [resolve(cl, t, pA).status_code for cl, t in ((rq, tq), (sp, ts), (an, ta))]
    check("other roles are refused (403) on the receipt and dispute routes: " + str(sorted(set(codes))), set(codes) == {403})
    codes = [anon.post(f"/needs/pledges/{pA}/confirm", data={"csrf": "x"}).status_code, anon.get(f"/verify/pledges/{pA}").status_code,
             anon.post(f"/verify/pledges/{pA}/resolve", data={"csrf": "x", "outcome": "arrived", "note": "x" * 20}).status_code]
    check("anonymous visitors get 401: " + str(codes), set(codes) == {401})
    r1, r2 = confirm(rq2, tq2, pA), dispute(rq2, tq2, pA)
    check("another requester cannot confirm or dispute my pledges (404, unchanged)",
          r1.status_code == 404 and r2.status_code == 404 and pstat(pA) == "sent")

    # ---- the requester's page
    page = rq.get("/needs/mine").text
    check("my needs lists pledges marked sent, with the amount and both answers",
          "400 TRY" in row_of(page, pA) and "It arrived" in row_of(page, pA) and "It did not arrive" in row_of(page, pA) and row_of(page, pC))
    check("a pledge not yet marked sent is not listed for an answer", row_of(page, pB) == "")
    check("the donor's name and contact are not shown to the requester", DONOR_CONTACT not in page and "rc_s<" not in page and ">rc_s" not in page)

    # ---- confirming
    r = rq.post(f"/needs/pledges/{pA}/confirm", data={"csrf": "forged"}, follow_redirects=False)
    check("confirming with a forged CSRF token is refused (403)", r.status_code == 403 and pstat(pA) == "sent")
    r = confirm(rq, tq, pA)
    check("confirming: 303, status confirmed, time recorded, event written",
          r.status_code == 303 and r.headers["location"] == "/needs/mine?received=1" and pstat(pA) == "confirmed"
          and one("SELECT confirmed_at IS NOT NULL FROM pledges WHERE id = :p", p=pA) and events(n1, "receipt_confirmed") == 1)
    check("it shows as confirmed, without buttons", "You confirmed it arrived" in row_of(rq.get("/needs/mine").text, pA)
          and "<button" not in row_of(rq.get("/needs/mine").text, pA))
    r1, r2 = confirm(rq, tq, pA), dispute(rq, tq, pA)
    check("a confirmed pledge cannot be confirmed or disputed again (409)", r1.status_code == 409 and r2.status_code == 409 and pstat(pA) == "confirmed")
    r1, r2 = confirm(rq, tq, pB), dispute(rq, tq, pB)
    check("a pledge not marked sent cannot be confirmed or disputed (409)", r1.status_code == 409 and r2.status_code == 409 and pstat(pB) == "pledged")
    check("the donor sees it as confirmed", "Confirmed by the requester" in row_of(sp.get("/support").text, pA))

    # ---- disputing
    r = dispute(rq, tq, pC, reason="short")
    check("a dispute without a real reason is refused (400), the reason is kept, nothing changes",
          r.status_code == 400 and ">short</textarea>" in r.text and pstat(pC) == "sent")
    r = dispute(rq, tq, pC, reason="x" * 451)
    check("a reason over 450 characters is refused (400)", r.status_code == 400 and pstat(pC) == "sent")
    HOSTILE = "<script>alert(1)</script> nothing arrived on the account"
    r = dispute(rq, tq, pC, reason=HOSTILE)
    check("disputing: 303, status disputed, the reason stored for verifiers",
          r.status_code == 303 and r.headers["location"] == "/needs/mine?disputed=1" and pstat(pC) == "disputed"
          and one("SELECT note FROM need_events WHERE need_id = :n AND event = 'receipt_disputed'", n=n1) == f"pledge {pC}: {HOSTILE}")
    check("the requester sees it as under review, without buttons",
          "A verifier is looking at it" in row_of(rq.get("/needs/mine").text, pC) and "<button" not in row_of(rq.get("/needs/mine").text, pC))
    sup = sp.get("/support").text
    check("the donor sees it as disputed, but not the requester's reason", "Disputed" in row_of(sup, pC) and "nothing arrived on the account" not in sup)
    board_routes._RATE.clear()
    check("a disputed pledge still counts as promised on the board", "Confirmed 400 TRY, promised 600 TRY more" in anon.get(f"/board/{n1}").text)

    # ---- the verifier's lists and page
    q = vf.get("/verify").text
    disputed_part = section(q, "Disputed pledges", "Sent but not answered")
    check("the verifier's queue lists the disputed pledge", f'data-pledge="{pC}"' in disputed_part and f'data-pledge="{pA}"' not in q)
    vp = vf.get(f"/verify/pledges/{pC}")
    check("its page shows the amount, both contacts, the account and the requester's reason (escaped)",
          vp.status_code == 200 and "300 TRY" in vp.text and DONOR_CONTACT in vp.text and REQ_CONTACT in vp.text and IBAN_SHOWN in vp.text
          and "&lt;script&gt;alert(1)&lt;/script&gt; nothing arrived" in vp.text and "<script>alert(1)" not in vp.text and "Settle the dispute" in vp.text)
    check("an unknown pledge gives 404 and a malformed id 422",
          vf.get("/verify/pledges/99999999").status_code == 404 and vf.get("/verify/pledges/abc").status_code == 422)
    check("a pledge that is not disputed has no settle form", "Settle the dispute" not in vf.get(f"/verify/pledges/{pA}").text)

    # ---- settling
    r = resolve(vf, tv, pC, outcome="")
    check("settling without an outcome is refused (400), nothing changes", r.status_code == 400 and pstat(pC) == "disputed")
    r = resolve(vf, tv, pC, outcome="refund")
    check("an unknown outcome is refused (400)", r.status_code == 400 and pstat(pC) == "disputed")
    r = resolve(vf, tv, pC, note="too short")
    check("settling without a real note is refused (400), the note is kept", r.status_code == 400 and ">too short</textarea>" in r.text and pstat(pC) == "disputed")
    r = vf.post(f"/verify/pledges/{pC}/resolve", data={"outcome": "arrived", "note": "A long enough note here.", "csrf": "forged"})
    check("settling with a forged CSRF token is refused (403)", r.status_code == 403 and pstat(pC) == "disputed")
    r = resolve(vf, tv, pC, outcome="not_arrived")
    check("'did not arrive': 303, the pledge is cancelled, event written with the note",
          r.status_code == 303 and r.headers["location"] == "/verify?done=resolved" and pstat(pC) == "cancelled"
          and one("SELECT note FROM need_events WHERE need_id = :n AND event = 'dispute_not_arrived'", n=n1)
          == f"pledge {pC}: The donor could not show a transfer receipt.")
    board_routes._RATE.clear()
    check("the amount is open again on the board", "Confirmed 400 TRY, promised 300 TRY more" in anon.get(f"/board/{n1}").text)
    check("settling it again is refused (409)", resolve(vf, tv, pC, outcome="arrived").status_code == 409 and pstat(pC) == "cancelled")
    hist = vf.get(f"/verify/pledges/{pC}").text
    check("the pledge page shows its history: disputed by the requester, settled by the verifier",
          "receipt_disputed" in hist and "dispute_not_arrived" in hist and "rc_req" in hist and "rc_v" in hist)

    # ---- confirmed money completes a need
    n2 = make(500)
    p2 = pledge(n2, 495)
    confirm(rq, tq, p2)
    check("confirming 495 of 500 leaves less than one minimum pledge open, so the need is fulfilled",
          pstat(p2) == "confirmed" and nstat(n2) == "fulfilled")
    board_routes._RATE.clear()
    check("a fulfilled need leaves the board", anon.get(f"/board/{n2}").status_code == 404 and "Fully funded" in rq.get("/needs/mine").text)
    n3 = make(1000)
    p3 = pledge(n3, 1000)
    dispute(rq, tq, p3)
    r = resolve(vf, tv, p3, outcome="arrived", note="The donor showed the bank receipt; the requester found it.")
    check("'it arrived' after a dispute confirms the pledge and can complete the need",
          r.status_code == 303 and pstat(p3) == "confirmed" and nstat(n3) == "fulfilled" and events(n3, "dispute_arrived") == 1)

    # ---- two verifiers settling the same dispute at once
    n4 = make(2000)
    p4 = pledge(n4, 700)
    dispute(rq, tq, p4)
    barrier, lock, out = threading.Barrier(2), threading.Lock(), []

    def worker(cl, tok, outcome):
        barrier.wait(timeout=30)
        r_ = resolve(cl, tok, p4, outcome=outcome)
        with lock:
            out.append(r_.status_code)

    ts_ = [threading.Thread(target=worker, args=a) for a in ((vf, tv, "arrived"), (vf2, tv2, "not_arrived"))]
    [t.start() for t in ts_]
    [t.join() for t in ts_]
    settled = events(n4, "dispute_arrived") + events(n4, "dispute_not_arrived")
    check(f"two verifiers settling one dispute at once: {sorted(out)}, {settled} event", sorted(out) == [303, 409] and settled == 1
          and pstat(p4) in ("confirmed", "cancelled"))

    # ---- unanswered pledges
    n5 = make(5000)
    old, recent = pledge(n5, 1000), pledge(n5, 1000)
    with admin.begin() as c:
        c.execute(text("UPDATE pledges SET sent_at = now() - interval '15 days' WHERE id = :p"), {"p": old})
        c.execute(text("UPDATE pledges SET sent_at = now() - interval '13 days' WHERE id = :p"), {"p": recent})
    q = vf.get("/verify").text
    unanswered = section(q, "Sent but not answered", "Look again")
    check("a pledge sent 15 days ago and not answered is listed for follow-up; one sent 13 days ago is not",
          f'data-pledge="{old}"' in unanswered and f'data-pledge="{recent}"' not in q)
    check("listing it changes nothing", pstat(old) == "sent" and "Settle the dispute" not in vf.get(f"/verify/pledges/{old}").text)
    confirm(rq, tq, old)
    check("once the requester answers, it leaves the list", f'data-pledge="{old}"' not in vf.get("/verify").text)

    # ---- a withdrawn need
    n6 = make(3000)
    p6 = pledge(n6, 800)
    with admin.begin() as c:
        c.execute(text("UPDATE needs SET status = 'withdrawn', withdrawn_reason = 'We found the money elsewhere.' WHERE id = :n"), {"n": n6})
    check("after a withdrawal the sent pledge is still open and listed", pstat(p6) == "sent" and "It arrived" in row_of(rq.get("/needs/mine").text, p6))
    r = confirm(rq, tq, p6)
    check("the requester can still confirm it (303, confirmed, the need stays withdrawn)", r.status_code == 303 and pstat(p6) == "confirmed" and nstat(n6) == "withdrawn")
finally:
    board_routes._RATE.clear()
    cleanup()
    left = one("SELECT count(*) FROM users WHERE left(username, 3) = 'rc_'")
    print(f"test rows removed (users left with the rc_ prefix: {left})")

assert not fails, f"failed: {fails}"
print(f"PASS: {total} checks")
