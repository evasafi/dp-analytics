"""Transfer reference code on money pledges: format, set by the database (not by the app role), unique,
shown to the donor while the pledge is open, to the requester only once it is sent (never with the donor's name or
contact), and to the verifier.

Run from the project root as a module: python -m dev.test_pledge_code
Needs the database owner's password (hidden prompt). Everything it creates has the username prefix pcd_ and is
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
USERS = {"pcd_req": "requester", "pcd_s1": "supporter", "pcd_s2": "supporter", "pcd_v": "verifier"}
CODE = re.compile(r"^AB-[A-HJKMNP-Z2-9]{4}-[A-HJKMNP-Z2-9]{4}$")
DONOR_CONTACT = "pcd-donor-contact@example.com"
fails, total = [], 0

with admin.connect() as c:
    if not c.execute(text("SELECT count(*) FROM information_schema.columns "
                          "WHERE table_name = 'pledges' AND column_name = 'ref_code'")).scalar_one():
        raise SystemExit("This database has no pledges.ref_code column; run the migration first.")


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
    sub = "(SELECT id FROM users WHERE left(username, 4) = 'pcd_')"
    mine = f"(SELECT id FROM needs WHERE requester_id IN {sub})"
    with admin.begin() as c:
        c.execute(text(f"DELETE FROM need_events WHERE actor_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM pledges WHERE supporter_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM needs WHERE requester_id IN {sub}"))
        c.execute(text("DELETE FROM users WHERE left(username, 4) = 'pcd_'"))


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
    with admin.begin() as c:
        nid = c.execute(text(
            "INSERT INTO needs (requester_id, purpose, currency, province, district, household_size, goal, description, "
            "contact_method, contact, payee_name, payee_details) VALUES (:r, 'rent_housing', 'TRY', 'Istanbul', :di, 4, 48000, "
            "'private description', 'other', 'private requester contact', 'Ayse Demir', 'TR330006100519786457841326') RETURNING id"),
            {"r": uid["pcd_req"], "di": district}).scalar_one()
        c.execute(text("UPDATE needs SET status = 'published', public_text = 'Public text.', payee_verified = true WHERE id = :i"), {"i": nid})

    c1, t1 = client_for("pcd_s1")
    c2, t2 = client_for("pcd_s2")
    cr, tr = client_for("pcd_req")
    cv, tv = client_for("pcd_v")

    def pledge(cl, tok, amount, contact=DONOR_CONTACT):
        return cl.post(f"/support/pledge/{nid}", data={"amount": str(amount), "contact_method": "email",
                                                       "contact": contact, "csrf": tok, "ack": "on"}, follow_redirects=False)

    # schema facts
    check("ref_code is NOT NULL", one("SELECT is_nullable FROM information_schema.columns "
                                      "WHERE table_name = 'pledges' AND column_name = 'ref_code'") == "NO")
    check("ref_code has a unique constraint", one("SELECT count(*) FROM pg_constraint WHERE conrelid = 'pledges'::regclass "
                                                  "AND contype = 'u' AND conname = 'pledges_ref_code_key'") == 1)
    check("app role cannot insert ref_code", not one("SELECT has_column_privilege('dp_app', 'pledges', 'ref_code', 'INSERT')"))
    check("app role cannot update ref_code", not one("SELECT has_column_privilege('dp_app', 'pledges', 'ref_code', 'UPDATE')"))
    check("app role can read ref_code", one("SELECT has_column_privilege('dp_app', 'pledges', 'ref_code', 'SELECT')"))
    check("no existing pledge lacks a code", one("SELECT count(*) FROM pledges WHERE ref_code IS NULL") == 0)

    # the pledge form tells the donor about the code
    form = c1.get(f"/support/pledge/{nid}")
    check("pledge form: 200", form.status_code == 200)
    check("pledge form mentions the reference code", "reference code" in form.text.lower())
    check("pledge form names the bank description field", "a\u00e7\u0131klama" in form.text)

    # first pledge
    r = pledge(c1, t1, floor)
    check("pledge accepted", r.status_code == 303 and r.headers["location"].endswith("/support?done=pledged"))
    pid = one("SELECT max(id) FROM pledges WHERE supporter_id = :s", s=uid["pcd_s1"])
    code1 = one("SELECT ref_code FROM pledges WHERE id = :p", p=pid)
    check("code has the AB-XXXX-XXXX format", bool(CODE.match(code1)))

    # a code forced in by the owner connection is replaced by the trigger
    with admin.begin() as c:
        forced = c.execute(text(
            "INSERT INTO pledges (need_id, supporter_id, amount_try, ack, contact_method, contact, ref_code) "
            "VALUES (:n, :s, :a, true, 'email', 'x@example.com', 'FORCED-CODE') RETURNING id"),
            {"n": nid, "s": uid["pcd_s1"], "a": floor}).scalar_one()
    check("a code supplied on insert is ignored", one("SELECT ref_code FROM pledges WHERE id = :p", p=forced) != "FORCED-CODE")
    with admin.begin() as c:
        c.execute(text("DELETE FROM pledges WHERE id = :p"), {"p": forced})

    # donor view
    banner = c1.get("/support?done=pledged").text
    check("thank-you banner mentions the reference code", "reference code" in banner.lower())
    page1 = c1.get("/support").text
    check("donor sees the code while pledged", code1 in page1)
    check("donor is told where to put it", "transfer description" in page1)
    page2 = c2.get("/support").text
    check("another donor does not see the code", code1 not in page2)

    # requester view
    mine = cr.get("/needs/mine")
    check("requester page loads", mine.status_code == 200)
    check("requester does not see the code before it is sent", code1 not in mine.text)

    sent = c1.post(f"/support/{pid}/sent", data={"csrf": t1}, follow_redirects=False)
    check("pledge marked as sent", sent.status_code == 303 and one("SELECT status FROM pledges WHERE id = :p", p=pid) == "sent")
    mine = cr.get("/needs/mine").text
    check("requester sees the code once sent", code1 in mine)
    check("requester never sees the donor's name", "pcd_s1" not in mine)
    check("requester never sees the donor's contact", DONOR_CONTACT not in mine)
    check("donor still sees the code after marking sent", code1 in c1.get("/support").text)

    # verifier view
    vp = cv.get(f"/verify/pledges/{pid}")
    check("verifier pledge page loads", vp.status_code == 200)
    check("verifier sees the code", code1 in vp.text)

    # a second pledge gets a different code, and a cancelled one stops being shown
    r2 = pledge(c2, t2, floor + 10, contact="other@example.com")
    check("second pledge accepted", r2.status_code == 303)
    pid2 = one("SELECT max(id) FROM pledges WHERE supporter_id = :s", s=uid["pcd_s2"])
    code2 = one("SELECT ref_code FROM pledges WHERE id = :p", p=pid2)
    check("second code has the right format", bool(CODE.match(code2)))
    check("codes differ between pledges", code1 != code2)
    check("second donor sees their own code", code2 in c2.get("/support").text)
    check("first donor does not see the second code", code2 not in c1.get("/support").text)
    c2.post(f"/support/{pid2}/cancel", data={"csrf": t2}, follow_redirects=False)
    check("pledge cancelled", one("SELECT status FROM pledges WHERE id = :p", p=pid2) == "cancelled")
    c2.get("/notifications")  # opening the history marks the banner read, so only the table is left
    check("a cancelled pledge no longer shows its code", code2 not in c2.get("/support").text)
finally:
    cleanup()

print(f"\n{total - len(fails)} of {total} checks passed")
if fails:
    print("FAILED:")
    for f in fails:
        print("  " + f)
    raise SystemExit(1)
