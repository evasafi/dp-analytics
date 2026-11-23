import sys
from getpass import getpass

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.auth import hash_password
from app.config import settings
from app.db import engine as app_engine

OWNER_PW = getpass("postgres password (hidden): ")
owner = create_engine(make_url(settings.database_url).set(username="postgres", password=OWNER_PW))
results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok or not detail else "  -> " + detail))


def first_line(e):
    return str(getattr(e, "orig", e)).strip().splitlines()[0]


def run(eng, sql, params=None):
    with eng.begin() as c:
        r = c.execute(text(sql), params or {})
        return r.scalar() if r.returns_rows else None


def row(eng, sql, params=None):
    with eng.connect() as c:
        return c.execute(text(sql), params or {}).one()


def must_fail(label, eng, sql, params, contains):
    try:
        with eng.begin() as c:
            c.execute(text(sql), params)
    except DBAPIError as e:
        check(label, contains in str(getattr(e, "orig", e)), "wrong error: " + first_line(e))
        return
    check(label, False, "no error was raised")


def must_ok(label, eng, sql, params=None):
    try:
        v = run(eng, sql, params)
    except DBAPIError as e:
        check(label, False, first_line(e))
        return None
    check(label, True)
    return v


MINE = "SELECT id FROM users WHERE left(username, 9) = 'itemtest_'"
NEEDS = "SELECT id FROM needs WHERE requester_id IN (" + MINE + ")"
CLAIMS = "SELECT id FROM item_claims WHERE need_id IN (" + NEEDS + ")"


def cleanup():
    with owner.begin() as c:
        c.execute(text("DELETE FROM item_claims WHERE need_id IN (" + NEEDS + ")"))
        c.execute(text("DELETE FROM need_events WHERE need_id IN (" + NEEDS + ")"))
        c.execute(text("DELETE FROM pledges WHERE need_id IN (" + NEEDS + ")"))
        c.execute(text("DELETE FROM needs WHERE requester_id IN (" + MINE + ")"))
        c.execute(text("DELETE FROM users WHERE left(username, 9) = 'itemtest_'"))


def insert_sql(vals):
    return ("INSERT INTO needs (" + ", ".join(vals) + ") VALUES ("
            + ", ".join(":" + k for k in vals) + ") RETURNING id")


try:
    cleanup()
    pw = hash_password("itemtest-password-123")
    ins_user = "INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r) RETURNING id"
    req = run(owner, ins_user, {"u": "itemtest_req", "h": pw, "r": "requester"})
    donor = run(owner, ins_user, {"u": "itemtest_donor", "h": pw, "r": "supporter"})
    donor2 = run(owner, ins_user, {"u": "itemtest_donor2", "h": pw, "r": "supporter"})
    ver = run(owner, ins_user, {"u": "itemtest_ver", "h": pw, "r": "verifier"})

    walker = run(owner, "SELECT id FROM item_types WHERE name = 'Walker'")
    electric = run(owner, "SELECT id FROM item_types WHERE name = 'Wheelchair, electric'")

    def money(**kw):
        v = dict(requester_id=req, purpose="food", currency="TRY", province="Istanbul",
                 district="Uskudar", household_size=3, goal=1000,
                 description="Test description for a money need", contact_method="phone",
                 contact="5550000", payee_name="Test Payee", payee_details="TR00 TEST 0000")
        v.update(kw)
        return v

    def item(**kw):
        v = dict(requester_id=req, need_kind="item", item_type_id=walker, quantity_needed=2,
                 used_ok=True, handover_method="public_place", province="Istanbul",
                 district="Uskudar", household_size=3,
                 description="Test description for an item need", contact_method="phone",
                 contact="5550000")
        v.update(kw)
        return v

    print("-- settings and item types")
    s = row(owner, "SELECT claim_expiry_days FROM settings")
    check("claim expiry 7 days", s[0] == 7, str(tuple(s)))
    check("walker is enabled",
          run(owner, "SELECT enabled FROM item_types WHERE name = 'Walker'") is True)
    check("electric wheelchair is disabled",
          run(owner, "SELECT enabled FROM item_types WHERE name = 'Wheelchair, electric'") is False)

    print("-- need shapes")
    m1 = must_ok("a money need still inserts", owner, insert_sql(money()), money())
    needA = must_ok("a valid item need inserts", owner, insert_sql(item()), item())
    IC = "item_needs_are_complete"
    must_fail("item need with a purpose is refused", owner, insert_sql(item(purpose="food")), item(purpose="food"), IC)
    must_fail("item need with a goal is refused", owner, insert_sql(item(goal=1000)), item(goal=1000), IC)
    must_fail("item need with a payee is refused", owner, insert_sql(item(payee_name="Test Payee")), item(payee_name="Test Payee"), IC)
    must_fail("item need with a stray event is refused", owner, insert_sql(item(event="flood")), item(event="flood"), IC)
    must_fail("item need without quantity is refused", owner, insert_sql(item(quantity_needed=None)), item(quantity_needed=None), IC)
    must_fail("item need without used_ok is refused", owner, insert_sql(item(used_ok=None)), item(used_ok=None), IC)
    must_fail("item need without handover method is refused", owner, insert_sql(item(handover_method=None)), item(handover_method=None), IC)
    must_fail("quantity 0 is refused", owner, insert_sql(item(quantity_needed=0)), item(quantity_needed=0), "quantity_needed")
    must_fail("quantity 51 is refused", owner, insert_sql(item(quantity_needed=51)), item(quantity_needed=51), "quantity_needed")
    must_fail("unknown handover method is refused", owner, insert_sql(item(handover_method="teleport")), item(handover_method="teleport"), "handover_method")
    must_fail("disabled item type is refused", owner, insert_sql(item(item_type_id=electric)), item(item_type_id=electric), "not available yet")
    must_fail("money need with an item type is refused", owner, insert_sql(money(item_type_id=walker)), money(item_type_id=walker), "money_needs_are_complete")
    must_fail("money need without a payee is refused", owner, insert_sql(money(payee_name=None)), money(payee_name=None), "money_needs_are_complete")

    print("-- publishing")
    must_fail("item need cannot publish without public text", owner,
              "UPDATE needs SET status = 'published' WHERE id = :n", {"n": needA}, "public_needs_are_complete")
    must_fail("money need still cannot publish without a verified payee", owner,
              "UPDATE needs SET status = 'published', public_text = 'x' WHERE id = :n", {"n": m1}, "public_needs_are_complete")
    must_ok("item need publishes with public text and no payee", owner,
            "UPDATE needs SET status = 'published', public_text = 'Test public text' WHERE id = :n", {"n": needA})

    print("-- money pledges and item needs")
    must_fail("a money pledge on an item need is refused", owner,
              "INSERT INTO pledges (need_id, supporter_id, amount_try, ack) VALUES (:n, :d, 100, true)",
              {"n": needA, "d": donor}, "money pledges are for money needs only")

    print("-- claims")
    needD = must_ok("a pending item need", owner, insert_sql(item()), item())
    CLAIM = ("INSERT INTO item_claims (need_id, donor_id, units, ack, contact_method, contact) "
             "VALUES (:n, :d, :u, true, 'phone', '5550001') RETURNING id")
    must_fail("claim on a pending need is refused", owner, CLAIM, {"n": needD, "d": donor, "u": 1}, "not open for item claims")
    must_fail("claim above the quantity is refused", owner, CLAIM, {"n": needA, "d": donor, "u": 3}, "exceed")
    must_fail("claim on your own need is refused", owner, CLAIM, {"n": needA, "d": req, "u": 1}, "own need")
    c1 = must_ok("donor claims 1 unit", owner, CLAIM, {"n": needA, "d": donor, "u": 1})
    c2 = must_ok("second donor claims 1 unit", owner, CLAIM, {"n": needA, "d": donor2, "u": 1})
    must_fail("a third unit is refused (quantity 2)", owner, CLAIM, {"n": needA, "d": donor, "u": 1}, "exceed")
    check("claim starts as pledged with an expiry",
          tuple(row(owner, "SELECT status, expires_at IS NOT NULL FROM item_claims WHERE id = :c", {"c": c1})) == ("pledged", True))
    must_fail("released_at without released_by is refused", owner,
              "UPDATE item_claims SET released_at = now() WHERE id = :c", {"c": c1}, "release_has_both")
    must_fail("handover cannot start before the contact is released", owner,
              "UPDATE item_claims SET status = 'sent' WHERE id = :c", {"c": c1}, "not released")
    for c in (c1, c2):
        run(owner, "UPDATE item_claims SET released_at = now(), released_by = :v WHERE id = :c", {"v": ver, "c": c})
        must_ok("claim " + str(c) + " moves to sent after release", owner,
                "UPDATE item_claims SET status = 'sent' WHERE id = :c", {"c": c})


    print("-- confirming and fulfilling")
    run(owner, "UPDATE item_claims SET status = 'confirmed' WHERE id = :c", {"c": c1})
    check("one of two units confirmed: need stays published",
          run(owner, "SELECT status FROM needs WHERE id = :n", {"n": needA}) == "published")
    run(owner, "UPDATE item_claims SET status = 'confirmed' WHERE id = :c", {"c": c2})
    check("both units confirmed: need is fulfilled",
          run(owner, "SELECT status FROM needs WHERE id = :n", {"n": needA}) == "fulfilled")
    must_fail("a confirmed claim cannot go back", owner,
              "UPDATE item_claims SET status = 'pledged' WHERE id = :c", {"c": c1}, "invalid claim status change")

    print("-- expiry")
    needB = run(owner, insert_sql(item(quantity_needed=3)), item(quantity_needed=3))
    run(owner, "UPDATE needs SET status = 'published', public_text = 'Test public text' WHERE id = :n", {"n": needB})
    cE = run(owner, CLAIM, {"n": needB, "d": donor, "u": 1})
    run(owner, "UPDATE item_claims SET expires_at = now() - interval '1 day' WHERE id = :c", {"c": cE})
    k = run(owner, "SELECT expire_item_claims()")
    check("expire_item_claims expired at least one claim", k >= 1, str(k))
    check("the claim is expired", run(owner, "SELECT status FROM item_claims WHERE id = :c", {"c": cE}) == "expired")
    must_ok("the freed units can be claimed again", owner, CLAIM, {"n": needB, "d": donor2, "u": 3})

    print("-- withdrawal")
    needC = run(owner, insert_sql(item()), item())
    run(owner, "UPDATE needs SET status = 'published', public_text = 'Test public text' WHERE id = :n", {"n": needC})
    cW = run(owner, CLAIM, {"n": needC, "d": donor, "u": 1})
    run(owner, "UPDATE needs SET status = 'withdrawn', withdrawn_reason = 'test withdrawal reason' WHERE id = :n", {"n": needC})
    check("withdrawing a need cancels its unsent claims",
          run(owner, "SELECT status FROM item_claims WHERE id = :c", {"c": cW}) == "cancelled")

    print("-- views")
    r = row(owner, "SELECT need_kind, quantity_needed, promised_units, confirmed_units FROM public_needs WHERE id = :n", {"n": needB})
    check("public_needs shows the item need with its units", tuple(r) == ("item", 3, 3, 0), str(tuple(r)))
    check("needs_analytics shows the verified item need as kind item",
          run(owner, "SELECT kind FROM needs_analytics WHERE id = :n", {"n": needB}) == "item")

    print("-- grants for the app role")
    check("the app role can read item_types", run(app_engine, "SELECT count(*) FROM item_types") > 0)
    must_fail("the app role cannot delete a claim", app_engine, "DELETE FROM item_claims WHERE id = :c", {"c": c1}, "permission denied")
    must_fail("the app role cannot change claim units", app_engine, "UPDATE item_claims SET units = 9 WHERE id = :c", {"c": c1}, "permission denied")
    must_fail("the app role cannot insert a published need", app_engine,
              insert_sql(item(status="published")), item(status="published"), "permission denied")
    must_ok("the app role can insert a pending item need", app_engine, insert_sql(item()), item())
finally:
    cleanup()
    print()
    print(sum(results), "of", len(results), "checks passed")
    if not all(results):
        sys.exit(1)
