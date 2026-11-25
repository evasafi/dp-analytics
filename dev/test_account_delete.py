"""Account deletion: the app role cannot edit users, only requesters and supporters can delete, the page lists what
is still open and the database refuses on the same list, a wrong password or an unticked box deletes nothing, and a
deleted account is anonymized in place (private text overwritten, rows kept, login and old cookie dead).

Run from the project root as a module: python -m dev.test_account_delete
Needs the database owner's password (hidden prompt). Everything it creates has the username prefix del_ (renamed
accounts are tracked by id) and is removed at the end.
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
USERS = {"del_req": "requester", "del_s1": "supporter", "del_s2": "supporter", "del_idle": "supporter", "del_v": "verifier"}
tracked = []
fails, total = [], 0

with admin.connect() as c:
    if not c.execute(text("SELECT count(*) FROM information_schema.columns "
                          "WHERE table_name = 'users' AND column_name = 'deleted_at'")).scalar_one():
        raise SystemExit("This database has no users.deleted_at column; run the migration first.")


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


def delete_fn(uid):
    with admin.begin() as c:
        return c.execute(text("SELECT delete_account(:u)"), {"u": uid}).scalar()


def cleanup():
    ids = list(tracked) or [0]
    sub = "(SELECT id FROM users WHERE left(username, 4) = 'del_' OR id = ANY(:ids))"
    mine = f"(SELECT id FROM needs WHERE requester_id IN {sub})"
    with admin.begin() as c:
        for sql in (f"DELETE FROM notifications WHERE user_id IN {sub}",
                    f"DELETE FROM need_events WHERE actor_id IN {sub} OR need_id IN {mine}",
                    f"DELETE FROM item_claims WHERE donor_id IN {sub} OR need_id IN {mine}",
                    f"DELETE FROM pledges WHERE supporter_id IN {sub} OR need_id IN {mine}",
                    f"DELETE FROM needs WHERE requester_id IN {sub}",
                    f"DELETE FROM users WHERE id IN {sub}"):
            c.execute(text(sql), {"ids": ids})


def client_for(u):
    cl = TestClient(app, base_url="http://127.0.0.1:8000")
    tok = csrf_from(cl.get("/login").text)
    code = cl.post("/login", data={"username": u, "password": PW, "csrf": tok}, follow_redirects=False).status_code
    assert code == 303, f"login {u} failed: {code}"
    return cl, tok


def post_delete(cl, tok, password=PW, confirm=True):
    d = {"password": password, "csrf": tok}
    if confirm:
        d["confirm"] = "on"
    return cl.post("/account/delete", data=d, follow_redirects=False)


def login_status(username):
    cl = TestClient(app, base_url="http://127.0.0.1:8000")
    tok = csrf_from(cl.get("/login").text)
    return cl.post("/login", data={"username": username, "password": PW, "csrf": tok}, follow_redirects=False).status_code


cleanup()
try:
    with admin.begin() as c:
        for u, role in USERS.items():
            c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r)"),
                      {"u": u, "h": hash_password(PW), "r": role})
    uid = {u: one("SELECT id FROM users WHERE username = :u", u=u) for u in USERS}
    tracked.extend(uid.values())
    floor = one("SELECT pledge_floor FROM settings")
    district = DISTRICTS_BY_PROVINCE["Istanbul"][0]

    # privileges and access
    check("app role cannot update users", not one("SELECT has_table_privilege('dp_app', 'users', 'UPDATE')"))
    check("app role can call delete_account", one("SELECT has_function_privilege('dp_app', 'delete_account(bigint)', 'EXECUTE')"))
    check("app role can call account_blockers", one("SELECT has_function_privilege('dp_app', 'account_blockers(bigint)', 'EXECUTE')"))
    check("a visitor cannot open the page", TestClient(app, base_url="http://127.0.0.1:8000").get("/account/delete").status_code == 401)
    cv, tv = client_for("del_v")
    check("a verifier cannot open the page", cv.get("/account/delete").status_code == 403)
    check("the database refuses to delete a verifier", delete_fn(uid["del_v"]) is False)

    # an account with nothing open
    ci, ti = client_for("del_idle")
    page = ci.get("/account/delete")
    check("an idle supporter sees the password form", page.status_code == 200 and 'name="password"' in page.text)
    old_cookie = ci.cookies.get("session")
    check("a wrong password deletes nothing", post_delete(ci, ti, password="wrong-password-123").status_code == 400
          and one("SELECT deleted_at FROM users WHERE id = :i", i=uid["del_idle"]) is None)
    check("an unticked box deletes nothing", post_delete(ci, ti, confirm=False).status_code == 400
          and one("SELECT deleted_at FROM users WHERE id = :i", i=uid["del_idle"]) is None)
    done = post_delete(ci, ti)
    check("deleting with the password and the box works", done.status_code == 200 and "was deleted" in done.text)
    check("the username is replaced", one("SELECT username FROM users WHERE id = :i", i=uid["del_idle"]) == f"deleted user #{uid['del_idle']}")
    check("the password hash is unusable", one("SELECT password_hash FROM users WHERE id = :i", i=uid["del_idle"]) == "!")
    check("deleted_at is set", one("SELECT deleted_at FROM users WHERE id = :i", i=uid["del_idle"]) is not None)
    stale = TestClient(app, base_url="http://127.0.0.1:8000")
    stale.cookies.set("session", old_cookie)
    check("the old session cookie stops working", stale.get("/support").status_code == 401)
    check("the old username cannot sign in", login_status("del_idle") == 401)
    check("the new username cannot sign in", login_status(f"deleted user #{uid['del_idle']}") == 401)
    check("deleting twice is refused", delete_fn(uid["del_idle"]) is False)

    # a need with private text, a pledge in flight
    with admin.begin() as c:
        nid = c.execute(text(
            "INSERT INTO needs (requester_id, purpose, currency, province, district, household_size, goal, description, "
            "contact_method, contact, payee_name, payee_details) VALUES (:r, 'rent_housing', 'TRY', 'Istanbul', :di, 4, 48000, "
            "'DEL-PRIVATE-DESC about the family', 'other', 'DEL-PRIVATE-CONTACT', 'DEL Payee Name', 'TR330006100519786457841326') RETURNING id"),
            {"r": uid["del_req"], "di": district}).scalar_one()
    run("UPDATE needs SET status = 'published', public_text = 'Public text.', payee_verified = true WHERE id = :i", i=nid)
    cr, tr = client_for("del_req")
    c1, t1 = client_for("del_s1")
    c2, t2 = client_for("del_s2")
    r = c1.post(f"/support/pledge/{nid}", data={"amount": str(floor), "contact_method": "email", "contact": "DEL-DONOR-EMAIL@example.com",
                                                "csrf": t1, "ack": "on"}, follow_redirects=False)
    check("pledge accepted", r.status_code == 303)
    p1 = one("SELECT max(id) FROM pledges WHERE supporter_id = :s", s=uid["del_s1"])
    check("a donor with an open pledge sees what is open", "still open" in c1.get("/account/delete").text
          and 'name="password"' not in c1.get("/account/delete").text)
    check("the donor cannot delete with an open pledge", post_delete(c1, t1).status_code == 409
          and one("SELECT deleted_at FROM users WHERE id = :i", i=uid["del_s1"]) is None)
    c1.post(f"/support/{p1}/sent", data={"csrf": t1}, follow_redirects=False)
    page = cr.get("/account/delete").text
    check("the requester sees the public request and the unanswered pledge",
          "public on the board" in page and "still need an answer" in page)
    check("the database refuses the requester too", delete_fn(uid["del_req"]) is False)
    cr.post(f"/needs/pledges/{p1}/confirm", data={"csrf": tr}, follow_redirects=False)
    check("once confirmed the donor has nothing open", 'name="password"' in c1.get("/account/delete").text)
    check("the requester is still blocked by the public request", "public on the board" in cr.get("/account/delete").text)

    with admin.begin() as c:
        c.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'receipt_disputed', :t)"),
                  {"n": nid, "a": uid["del_req"], "t": f"pledge {p1}: DEL-PRIVATE-WORDS about the transfer"})
        c.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'withdrawn', 'DEL-FREE-TEXT-NOTE')"),
                  {"n": nid, "a": uid["del_req"]})

    # the donor deletes: the pledge stays, the contact goes
    check("the donor has notifications before deleting", one("SELECT count(*) FROM notifications WHERE user_id = :u", u=uid["del_s1"]) > 0)
    check("the donor deletes", post_delete(c1, t1).status_code == 200)
    check("the pledge row stays and is still confirmed", one("SELECT status FROM pledges WHERE id = :p", p=p1) == "confirmed")
    check("the donor contact on the pledge is gone", one("SELECT contact FROM pledges WHERE id = :p", p=p1) is None)
    check("the donor's notifications are gone", one("SELECT count(*) FROM notifications WHERE user_id = :u", u=uid["del_s1"]) == 0)
    check("the donor is renamed", one("SELECT username FROM users WHERE id = :i", i=uid["del_s1"]) == f"deleted user #{uid['del_s1']}")

    # an item need and offer
    itype = one("SELECT id FROM item_types WHERE enabled ORDER BY id LIMIT 1")
    check("an item type is enabled", itype is not None)
    with admin.begin() as c:
        inid = c.execute(text(
            "INSERT INTO needs (requester_id, need_kind, item_type_id, quantity_needed, specification, used_ok, handover_method, "
            "province, district, household_size, description, contact_method, contact) VALUES (:r, 'item', :t, 3, 'DEL-PRIVATE-SPEC', "
            "true, 'public_place', 'Istanbul', :di, 3, 'DEL-PRIVATE-DESC item need', 'other', 'DEL-PRIVATE-CONTACT') RETURNING id"),
            {"r": uid["del_req"], "t": itype, "di": district}).scalar_one()
    run("UPDATE needs SET status = 'published', public_text = 'Public text.' WHERE id = :i", i=inid)
    with admin.begin() as c:
        k1 = c.execute(text("INSERT INTO item_claims (need_id, donor_id, units, ack, contact_method, contact) "
                            "VALUES (:n, :d, 2, true, 'email', 'DEL-ITEM-DONOR-CONTACT') RETURNING id"),
                       {"n": inid, "d": uid["del_s2"]}).scalar_one()
    run("UPDATE item_claims SET released_at = now(), released_by = :v WHERE id = :k", v=uid["del_v"], k=k1)
    run("UPDATE item_claims SET status = 'sent' WHERE id = :k", k=k1)
    check("an item donor with a handover in flight is blocked", post_delete(c2, t2).status_code == 409)
    run("UPDATE item_claims SET status = 'confirmed' WHERE id = :k", k=k1)
    check("the item donor has notifications before deleting", one("SELECT count(*) FROM notifications WHERE user_id = :u", u=uid["del_s2"]) > 0)
    check("the item donor deletes", post_delete(c2, t2).status_code == 200)
    check("the offer stays and keeps its units", one("SELECT units FROM item_claims WHERE id = :k", k=k1) == 2)
    check("the item donor contact is overwritten", one("SELECT contact FROM item_claims WHERE id = :k", k=k1) == "removed")
    check("the item donor's notifications are gone", one("SELECT count(*) FROM notifications WHERE user_id = :u", u=uid["del_s2"]) == 0)

    # the requester finishes everything, then deletes
    run("UPDATE needs SET status = 'withdrawn', withdrawn_reason = 'DEL-REASON-MARKER no longer needed.' WHERE id IN (:a, :b)", a=nid, b=inid)
    check("the requester has notifications before deleting", one("SELECT count(*) FROM notifications WHERE user_id = :u", u=uid["del_req"]) > 0)
    page = cr.get("/account/delete").text
    check("with everything finished the requester sees the form", 'name="password"' in page)
    check("the requester deletes", post_delete(cr, tr).status_code == 200)

    check("both needs are kept", one("SELECT count(*) FROM needs WHERE requester_id = :u", u=uid["del_req"]) == 2)
    check("no private marker is left in the needs", one(
        "SELECT count(*) FROM needs WHERE requester_id = :u AND (description LIKE '%DEL-PRIVATE%' OR contact LIKE '%DEL-PRIVATE%' "
        "OR payee_name LIKE '%DEL Payee%' OR withdrawn_reason LIKE '%DEL-REASON%' OR public_text = 'Public text.' "
        "OR specification IS NOT NULL OR payee_details LIKE 'TR33%')", u=uid["del_req"]) == 0)
    check("the money need says it was removed", one("SELECT description FROM needs WHERE id = :i", i=nid) == "Removed with the account."
          and one("SELECT payee_name FROM needs WHERE id = :i", i=nid) == "Removed")
    check("the item need has no payee", one("SELECT payee_name FROM needs WHERE id = :i", i=inid) is None)
    check("goal, place and status are unchanged", one("SELECT goal FROM needs WHERE id = :i", i=nid) == 48000
          and one("SELECT province FROM needs WHERE id = :i", i=nid) == "Istanbul"
          and one("SELECT status FROM needs WHERE id = :i", i=nid) == "withdrawn")
    notes = [r[0] for r in admin.connect().execute(text("SELECT note FROM need_events WHERE actor_id = :a AND event IN ('receipt_disputed', 'withdrawn')"),
                                                   {"a": uid["del_req"]})]
    check("a dispute note keeps its pledge tag and loses the words", f"pledge {p1}: removed" in notes
          and not any("DEL-PRIVATE-WORDS" in (n or "") for n in notes))
    check("a free-text note is blanked", not any("DEL-FREE-TEXT-NOTE" in (n or "") for n in notes))
    check("the requester's notifications are gone", one("SELECT count(*) FROM notifications WHERE user_id = :u", u=uid["del_req"]) == 0)
    check("the requester cannot sign in again", login_status("del_req") == 401)

    vp = cv.get(f"/verify/pledges/{p1}")
    check("the verifier's pledge page still loads", vp.status_code == 200)
    check("it shows the deleted accounts by their new names", "deleted user #" in vp.text)
    check("no private marker reaches the verifier page", "DEL-PRIVATE" not in vp.text and "DEL-DONOR-EMAIL" not in vp.text)
finally:
    cleanup()

print(f"\n{total - len(fails)} of {total} checks passed")
if fails:
    print("FAILED:")
    for f in fails:
        print("  " + f)
    raise SystemExit(1)
