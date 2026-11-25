"""Notifications: written only by the database on status changes, never forgeable by the app role, fixed text
without names or contacts, shown as a banner for the right person, marked read by opening the history page.

Run from the project root as a module: python -m dev.test_notifications
Needs the database owner's password (hidden prompt). Everything it creates has the username prefix ntf_ and is
removed at the end.
"""
import getpass
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
USERS = {"ntf_req": "requester", "ntf_s1": "supporter", "ntf_s2": "supporter", "ntf_v": "verifier"}
IBAN = "TR330006100519786457841326"
S1_CONTACT = "ntf-s1-contact@example.com"
REQ_CONTACT = "ntf private requester contact"
fails, total = [], 0

with admin.connect() as c:
    if not c.execute(text("SELECT count(*) FROM information_schema.tables WHERE table_name = 'notifications'")).scalar_one():
        raise SystemExit("This database has no notifications table; run the migration first.")


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
        return c.execute(text(sql), p).scalar()


def run(sql, **p):
    with admin.begin() as c:
        c.execute(text(sql), p)


def cleanup():
    sub = "(SELECT id FROM users WHERE left(username, 4) = 'ntf_')"
    mine = f"(SELECT id FROM needs WHERE requester_id IN {sub})"
    with admin.begin() as c:
        c.execute(text(f"DELETE FROM notifications WHERE user_id IN {sub}"))
        c.execute(text(f"DELETE FROM need_events WHERE actor_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM item_claims WHERE donor_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM pledges WHERE supporter_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM needs WHERE requester_id IN {sub}"))
        c.execute(text("DELETE FROM users WHERE left(username, 4) = 'ntf_'"))


def client_for(u):
    cl = TestClient(app, base_url="http://127.0.0.1:8000")
    tok = csrf_from(cl.get("/login").text)
    code = cl.post("/login", data={"username": u, "password": PW, "csrf": tok}, follow_redirects=False).status_code
    assert code == 303, f"login {u} failed: {code}"
    return cl, tok


cleanup()
try:
    with admin.begin() as c:
        for u, role in USERS.items():
            c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r)"),
                      {"u": u, "h": hash_password(PW), "r": role})
    uid = {u: one("SELECT id FROM users WHERE username = :u", u=u) for u in USERS}
    floor = one("SELECT pledge_floor FROM settings")
    district = DISTRICTS_BY_PROVINCE["Istanbul"][0]

    def notes(user, like="%"):
        return one("SELECT count(*) FROM notifications WHERE user_id = :u AND message ILIKE :l", u=uid[user], l=like)

    def last(user):
        return one("SELECT message FROM notifications WHERE user_id = :u ORDER BY id DESC LIMIT 1", u=uid[user]) or ""

    def unread(user):
        return one("SELECT count(*) FROM notifications WHERE user_id = :u AND read_at IS NULL", u=uid[user])

    def clear():
        run("DELETE FROM notifications WHERE user_id IN (SELECT id FROM users WHERE left(username, 4) = 'ntf_')")

    def money_need():
        with admin.begin() as c:
            return c.execute(text(
                "INSERT INTO needs (requester_id, purpose, currency, province, district, household_size, goal, description, "
                "contact_method, contact, payee_name, payee_details) VALUES (:r, 'rent_housing', 'TRY', 'Istanbul', :di, 4, 48000, "
                "'private description', 'other', :co, 'Ayse Demir', :ib) RETURNING id"),
                {"r": uid["ntf_req"], "di": district, "co": REQ_CONTACT, "ib": IBAN}).scalar_one()

    def publish(nid):
        run("UPDATE needs SET status = 'published', public_text = 'Public text.', payee_verified = true WHERE id = :i", i=nid)

    # privileges: the app role can read and mark read, nothing else
    check("app role can read notifications", one("SELECT has_table_privilege('dp_app', 'notifications', 'SELECT')"))
    check("app role cannot insert notifications", not one("SELECT has_table_privilege('dp_app', 'notifications', 'INSERT')"))
    check("app role cannot delete notifications", not one("SELECT has_table_privilege('dp_app', 'notifications', 'DELETE')"))
    check("app role can set read_at", one("SELECT has_column_privilege('dp_app', 'notifications', 'read_at', 'UPDATE')"))
    check("app role cannot rewrite the message", not one("SELECT has_column_privilege('dp_app', 'notifications', 'message', 'UPDATE')"))
    check("app role cannot call add_notification",
          not one("SELECT has_function_privilege('dp_app', 'add_notification(bigint, text, text)', 'EXECUTE')"))

    # need review
    nid = money_need()
    check("no notice while a need is pending", notes("ntf_req") == 0)
    publish(nid)
    check("requester is told the need was approved", last("ntf_req").startswith(f"Your request #{nid} was approved"))
    rej = money_need()
    run("UPDATE needs SET status = 'rejected', review_note = 'not enough detail' WHERE id = :i", i=rej)
    check("requester is told the need was not approved", f"#{rej} was not approved" in last("ntf_req"))
    clear()

    c1, t1 = client_for("ntf_s1")
    c2, t2 = client_for("ntf_s2")
    cr, tr = client_for("ntf_req")
    cv, tv = client_for("ntf_v")

    def pledge(cl, tok, amount, contact=S1_CONTACT):
        return cl.post(f"/support/pledge/{nid}", data={"amount": str(amount), "contact_method": "email",
                                                       "contact": contact, "csrf": tok, "ack": "on"}, follow_redirects=False)

    def newest(user):
        return one("SELECT max(id) FROM pledges WHERE supporter_id = :s", s=uid[user])

    # pledge in progress, sent, confirmed
    check("pledge accepted", pledge(c1, t1, floor).status_code == 303)
    p1 = newest("ntf_s1")
    code1 = one("SELECT ref_code FROM pledges WHERE id = :p", p=p1)
    check("donor is told the pledge is in progress", "now in progress" in last("ntf_s1") and code1 in last("ntf_s1"))
    check("requester is not told about an unsent pledge", notes("ntf_req") == 0)
    page = c1.get("/support?done=pledged").text
    check("banner on the donor's page", 'class="alerts"' in page and "now in progress" in page)
    check("another person sees no banner", 'class="alerts"' not in c2.get("/support").text)

    c1.post(f"/support/{p1}/sent", data={"csrf": t1}, follow_redirects=False)
    check("requester is told a pledge was sent", "says they sent" in last("ntf_req") and code1 in last("ntf_req"))
    check("requester banner on their page", "says they sent" in cr.get("/needs/mine").text)
    check("donor gets no extra notice for their own action", notes("ntf_s1") == 1)

    cr.post(f"/needs/pledges/{p1}/confirm", data={"csrf": tr}, follow_redirects=False)
    check("donor is told it was confirmed", "confirmed as received" in last("ntf_s1"))

    # no names, contacts or account details in any notice
    every = " ".join(r[0] for r in admin.connect().execute(text(
        "SELECT message FROM notifications WHERE user_id IN (SELECT id FROM users WHERE left(username, 4) = 'ntf_')")))
    check("no notice contains a username", not any(u in every for u in USERS))
    check("no notice contains a contact or the account number", not any(x in every for x in (S1_CONTACT, REQ_CONTACT, IBAN, "Ayse Demir")))

    # history page
    check("donor has unread notices", unread("ntf_s1") == 2)
    h = c1.get("/notifications")
    check("history page loads", h.status_code == 200)
    check("history lists both notices", "now in progress" in h.text and "confirmed as received" in h.text)
    check("opening the history marks all read", unread("ntf_s1") == 0)
    check("the banner is gone afterwards", 'class="alerts"' not in c1.get("/support").text)
    check("someone with no notices sees an empty page", "No notifications yet." in c2.get("/notifications").text)
    check("a visitor cannot open the history", TestClient(app, base_url="http://127.0.0.1:8000").get("/notifications").status_code == 401)

    # dispute and settlement
    pledge(c1, t1, floor + 10)
    p2 = newest("ntf_s1")
    c1.post(f"/support/{p2}/sent", data={"csrf": t1}, follow_redirects=False)
    cr.post(f"/needs/pledges/{p2}/dispute", data={"csrf": tr, "reason": "I checked the account and found nothing."}, follow_redirects=False)
    check("donor is told about the dispute", "did not arrive" in last("ntf_s1"))
    cv.post(f"/verify/pledges/{p2}/resolve", data={"csrf": tv, "outcome": "not_arrived", "note": "Checked the receipt, nothing found."},
            follow_redirects=False)
    check("donor is told how the dispute was settled", "not received" in last("ntf_s1"))
    check("requester is told how the dispute was settled", "not received" in last("ntf_req"))

    # a donor cancelling their own pledge is not a notice
    pledge(c2, t2, floor, contact="other@example.com")
    p3 = newest("ntf_s2")
    before = notes("ntf_s2")
    c2.post(f"/support/{p3}/cancel", data={"csrf": t2}, follow_redirects=False)
    check("self-cancel is not announced", one("SELECT status FROM pledges WHERE id = :p", p=p3) == "cancelled" and notes("ntf_s2") == before)

    # expiry
    pledge(c2, t2, floor, contact="other@example.com")
    p4 = newest("ntf_s2")
    run("UPDATE pledges SET expires_at = now() - interval '1 day' WHERE id = :p", p=p4)
    run("SELECT expire_pledges()")
    check("expired pledge is announced", "expired" in last("ntf_s2"))

    # withdrawal of the need cancels open pledges
    pledge(c2, t2, floor, contact="other@example.com")
    p5 = newest("ntf_s2")
    run("UPDATE needs SET status = 'withdrawn', withdrawn_reason = 'Not needed any more at all.' WHERE id = :i", i=nid)
    check("withdrawal is announced to the donor", one("SELECT status FROM pledges WHERE id = :p", p=p5) == "cancelled" and "withdrew" in last("ntf_s2"))

    # item offers
    itype = one("SELECT id FROM item_types WHERE enabled ORDER BY id LIMIT 1")
    check("an item type is enabled", itype is not None)
    with admin.begin() as c:
        inid = c.execute(text(
            "INSERT INTO needs (requester_id, need_kind, item_type_id, quantity_needed, used_ok, handover_method, province, district, "
            "household_size, description, contact_method, contact) VALUES (:r, 'item', :t, 3, true, 'public_place', 'Istanbul', :di, 3, "
            "'private description text', 'other', :co) RETURNING id"),
            {"r": uid["ntf_req"], "t": itype, "di": district, "co": REQ_CONTACT}).scalar_one()
    run("UPDATE needs SET status = 'published', public_text = 'Public text.' WHERE id = :i", i=inid)
    clear()
    with admin.begin() as c:
        k1 = c.execute(text("INSERT INTO item_claims (need_id, donor_id, units, ack, contact_method, contact) "
                            "VALUES (:n, :d, 2, true, 'email', :co) RETURNING id"), {"n": inid, "d": uid["ntf_s2"], "co": S1_CONTACT}).scalar_one()
    check("item donor is told the offer is recorded", "offer of 2 units of" in last("ntf_s2"))
    check("requester hears nothing before release", notes("ntf_req") == 0)
    run("UPDATE item_claims SET released_at = now(), released_by = :v WHERE id = :k", v=uid["ntf_v"], k=k1)
    check("requester is told about the released offer", "A donor offered 2 units of" in last("ntf_req"))
    check("item donor is told the contact was passed on", "passed your contact" in last("ntf_s2"))
    run("UPDATE item_claims SET status = 'sent' WHERE id = :k", k=k1)
    check("requester is told about the handover", "handed over" in last("ntf_req"))
    run("UPDATE item_claims SET status = 'confirmed' WHERE id = :k", k=k1)
    check("item donor is told it was confirmed", "confirmed that they received" in last("ntf_s2"))
    with admin.begin() as c:
        c.execute(text("INSERT INTO item_claims (need_id, donor_id, units, ack, contact_method, contact) "
                       "VALUES (:n, :d, 1, true, 'email', 'x@example.com')"), {"n": inid, "d": uid["ntf_s1"]})
    run("UPDATE needs SET status = 'withdrawn', withdrawn_reason = 'Not needed any more at all.' WHERE id = :i", i=inid)
    check("withdrawal is announced to an item donor", "withdrew" in last("ntf_s1"))
    kd = one("SELECT max(id) FROM item_claims WHERE donor_id = :d", d=uid["ntf_s1"])
    run("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'claim_declined', :t)",
        n=inid, a=uid["ntf_v"], t=f"claim {kd}: Not a good fit for this request.")
    check("a declined offer is announced without the reason", "declined your item offer" in last("ntf_s1")
          and "good fit" not in last("ntf_s1"))
finally:
    cleanup()

print(f"\n{total - len(fails)} of {total} checks passed")
if fails:
    print("FAILED:")
    for f in fails:
        print("  " + f)
    raise SystemExit(1)
