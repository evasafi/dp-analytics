"""Part 3: verifier review of item needs."""
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
PW = "Itv-test-pass-1234"
PREFIX = "itv_"
BASE = "http://127.0.0.1:8000"
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


def cleanup():
    with admin.begin() as c:
        c.execute(text("DELETE FROM need_events WHERE need_id IN (SELECT id FROM needs WHERE requester_id IN "
                       "(SELECT id FROM users WHERE username LIKE :p))"), {"p": PREFIX + "%"})
        c.execute(text("DELETE FROM needs WHERE requester_id IN (SELECT id FROM users WHERE username LIKE :p)"),
                  {"p": PREFIX + "%"})
        c.execute(text("DELETE FROM login_failures WHERE username LIKE :p"), {"p": PREFIX + "%"})
        c.execute(text("DELETE FROM users WHERE username LIKE :p"), {"p": PREFIX + "%"})


def make_user(u, role):
    with admin.begin() as c:
        return c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r) RETURNING id"),
                         {"u": u, "h": hash_password(PW), "r": role}).scalar_one()


def client_for(u):
    cl = TestClient(app, base_url=BASE)
    tok = csrf_from(cl.get("/login").text)
    code = cl.post("/login", data={"username": u, "password": PW, "csrf": tok}, follow_redirects=False).status_code
    assert code == 303, "login for %s gave %s" % (u, code)
    return cl


prov = next(iter(DISTRICTS_BY_PROVINCE))
dist = DISTRICTS_BY_PROVINCE[prov][0]


def new_need(requester_id, type_id):
    with admin.begin() as c:
        return c.execute(text(
            "INSERT INTO needs (requester_id, need_kind, item_type_id, quantity_needed, specification, used_ok, "
            "handover_method, province, district, household_size, description, contact_method, contact) VALUES "
            "(:u, 'item', :t, 1, 'size M', true, 'public_place', :p, :d, 3, 'ITV test need for review', 'email', "
            "'itv_contact@example.com') RETURNING id"), {"u": requester_id, "t": type_id, "p": prov, "d": dist}).scalar_one()


def events(nid, name):
    return one("SELECT count(*) FROM need_events WHERE need_id = :n AND event = :e", n=nid, e=name)[0]


def state(nid):
    return one("SELECT status, public_text, payee_verified, reviewed_by, review_note FROM needs WHERE id = :i", i=nid)


rep_id, rep_name = one("SELECT id, name FROM item_types WHERE enabled AND category IN ('mobility','medical_equipment') ORDER BY id LIMIT 1")
bed_id, bed_name = one("SELECT id, name FROM item_types WHERE enabled AND category = 'bedding' ORDER BY id LIMIT 1")
print("report-type item:", rep_name, "| plain item:", bed_name)
GOODTEXT = "A household in %s, %s needs help with a basic item." % (dist, prov)

cleanup()
try:
    ver_id = make_user("itv_ver", "verifier")
    req_id = make_user("itv_req", "requester")
    n_rep = new_need(req_id, rep_id)
    n_bed = new_need(req_id, bed_id)
    n_rej = new_need(req_id, bed_id)
    n_csrf = new_need(req_id, bed_id)
    try:
        n_self = new_need(ver_id, bed_id)
    except Exception as e:
        n_self = None
        print("note: could not create a need owned by a verifier:", str(e).splitlines()[0])
    v = client_for("itv_ver")
    r = client_for("itv_req")
    anon = TestClient(app, base_url=BASE)
    anon_tok = csrf_from(anon.get("/login").text)
    tok = csrf_from(v.get("/verify/%d" % n_csrf).text)

    def approve(cl, nid, token=tok, **over):
        return cl.post("/verify/%d/approve" % nid, data={"csrf": token, "public_text": GOODTEXT, **over}, follow_redirects=False)

    print("-- queue and detail")
    q = v.get("/verify")
    check("1. the queue renders with item needs in it", q.status_code == 200)
    check("2. the queue shows the item name", "Item: " + str(escape(rep_name)) in q.text and "1 unit" in q.text)
    d_rep = v.get("/verify/%d" % n_rep)
    d_bed = v.get("/verify/%d" % n_bed)
    check("3. detail renders for an item need", d_rep.status_code == 200 and str(escape(rep_name)) in d_rep.text)
    check("4. detail shows no IBAN, account holder or goal row",
          all(x not in d_rep.text for x in ("IBAN", "Account holder", "<th>Goal</th>")))
    check("5. detail shows condition and handover", "a used item is fine" in d_rep.text and "Meet in a public place" in d_rep.text)
    check("6. contact tick is on both forms", 'name="check_contact"' in d_rep.text and 'name="check_contact"' in d_bed.text)
    check("7. report tick only for mobility or medical items", 'name="check_report"' in d_rep.text and 'name="check_report"' not in d_bed.text)

    print("-- approving a report-type item")
    resp = approve(v, n_rep)
    check("8. no ticks: 400, still pending, no events", resp.status_code == 400 and state(n_rep).status == "pending"
          and events(n_rep, "approved") == 0)
    resp = approve(v, n_rep, check_contact="on")
    check("9. contact tick only: 400 and still pending", resp.status_code == 400 and state(n_rep).status == "pending")
    resp = approve(v, n_rep, check_contact="on", check_report="on", public_text=GOODTEXT + " Write to itv_contact@example.com")
    check("10. public text with the requester's contact is refused", resp.status_code == 400 and state(n_rep).status == "pending")
    resp = approve(v, n_rep, check_contact="on", check_report="on")
    check("11. both ticks and good text: 303 to the queue",
          resp.status_code == 303 and resp.headers.get("location") == "/verify?done=approved&need=%d" % n_rep)
    s = state(n_rep)
    check("12. published with the text, reviewer set, payee_verified still false",
          s.status == "published" and s.public_text == GOODTEXT and s.payee_verified is False and s.reviewed_by == ver_id)
    check("13. one 'approved' and one 'report_checked' event", events(n_rep, "approved") == 1 and events(n_rep, "report_checked") == 1)
    resp = approve(v, n_rep, check_contact="on", check_report="on")
    check("14. approving it again gives 409 and adds no event", resp.status_code == 409 and events(n_rep, "approved") == 1)

    print("-- approving a plain item")
    resp = approve(v, n_bed, check_contact="on")
    check("15. contact tick alone is enough: 303", resp.status_code == 303)
    check("16. 'approved' recorded, no 'report_checked'", events(n_bed, "approved") == 1 and events(n_bed, "report_checked") == 0
          and state(n_bed).status == "published")
    check("17. a decided need says it is already decided", "already been decided" in v.get("/verify/%d" % n_bed).text)
    if n_self is not None:
        resp = approve(v, n_self, check_contact="on")
        check("18. a verifier cannot approve their own need", resp.status_code == 409 and state(n_self).status == "pending")

    print("-- rejecting")
    resp = v.post("/verify/%d/reject" % n_rej, data={"csrf": tok, "note": "short"}, follow_redirects=False)
    check("19. a short reason gives 400 and the need stays pending", resp.status_code == 400 and state(n_rej).status == "pending")
    resp = v.post("/verify/%d/reject" % n_rej, data={"csrf": tok, "note": "Could not reach the requester by phone."}, follow_redirects=False)
    check("20. a good reason: 303, rejected, note stored",
          resp.status_code == 303 and state(n_rej).status == "rejected" and "Could not reach" in state(n_rej).review_note)
    check("21. a 'rejected' event was recorded", events(n_rej, "rejected") == 1)

    print("-- access")
    check("22. a requester gets 403 on the detail page", r.get("/verify/%d" % n_csrf).status_code == 403)
    rt = csrf_from(r.get("/needs/mine").text)
    check("23. a requester gets 403 on approve", approve(r, n_csrf, token=rt, check_contact="on").status_code == 403)
    check("24. a visitor gets 401 on the detail page", anon.get("/verify/%d" % n_csrf).status_code == 401)
    check("25. a visitor gets 401 on approve", approve(anon, n_csrf, token=anon_tok, check_contact="on").status_code == 401)
    resp = approve(v, n_csrf, token="forged", check_contact="on")
    check("26. a forged csrf gives 403 and the need stays pending", resp.status_code == 403 and state(n_csrf).status == "pending")
finally:
    cleanup()

print("\n%d of %d checks passed" % (total - fails, total))
sys.exit(1 if fails else 0)
