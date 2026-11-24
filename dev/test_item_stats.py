"""Part 7b: the front-page stats count fulfilled item needs, confirmed units and item donors. Only confirmed claims count."""
import getpass
import re
import sys

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import items
from app.auth import hash_password
from app.config import settings
from app.districts import DISTRICTS_BY_PROVINCE
from app.main import app

admin = create_engine(make_url(settings.database_url).set(username="postgres", password=getpass.getpass("postgres password: ")))
PW = "Its-test-pass-1234"
PREFIX = "its_"
BASE = "http://127.0.0.1:8000"
fails = 0
total = 0


def check(label, ok, info=""):
    global fails, total
    total += 1
    if not ok:
        fails += 1
    print("%s%02d. %s%s" % ("ok   " if ok else "FAIL ", total, label, "" if ok or info == "" else "  [" + str(info) + "]"))


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
        c.execute(text("DELETE FROM users WHERE username LIKE :p"), {"p": PREFIX + "%"})


def make_user(u, role):
    return run("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r) RETURNING id", u=u, h=hash_password(PW), r=role)


prov = next(iter(DISTRICTS_BY_PROVINCE))
dist = DISTRICTS_BY_PROVINCE[prov][0]
bed_id = one("SELECT id FROM item_types WHERE enabled AND category = 'bedding' ORDER BY id LIMIT 1")[0]


def new_need(requester_id, qty):
    nid = run("INSERT INTO needs (requester_id, need_kind, item_type_id, quantity_needed, used_ok, handover_method, province, "
              "district, household_size, description, contact_method, contact) VALUES (:u, 'item', :t, :q, true, 'public_place', "
              ":p, :d, 3, 'ITS test need for review', 'email', 'its_req_contact@example.com') RETURNING id",
              u=requester_id, t=bed_id, q=qty, p=prov, d=dist)
    run("UPDATE needs SET status = 'published', public_text = 'Public text for the stats tests' WHERE id = :i", i=nid)
    return nid


def make_claim(nid, donor_id, upto):
    cid = run("INSERT INTO item_claims (need_id, donor_id, units, ack, contact_method, contact) "
              "VALUES (:n, :d, 1, true, 'email', 'its_donor@example.com') RETURNING id", n=nid, d=donor_id)
    if upto != "pledged":
        run("UPDATE item_claims SET released_at = now(), released_by = :v WHERE id = :c", v=VER, c=cid)
        run("UPDATE item_claims SET status = 'sent' WHERE id = :c", c=cid)
    if upto == "confirmed":
        run("UPDATE item_claims SET status = 'confirmed' WHERE id = :c", c=cid)
    return cid


def stats():
    return one("SELECT fulfilled_needs, confirmed_try, donors, fulfilled_items, confirmed_units, claimants FROM public_stats")


def delta(a, b):
    return tuple(x - y for x, y in zip(a, b))


cleanup()
try:
    before = stats()
    anon = TestClient(app, base_url=BASE)
    page0 = anon.get("/board").text
    check("the existing money sentence is still there", 'id="stats"' in page0 and "confirmed by requesters" in page0)
    original_flag = items.ITEM_NEEDS_OPEN
    if not (before.fulfilled_items or before.confirmed_units or before.claimants):
        items.ITEM_NEEDS_OPEN = False
        page0 = anon.get("/board").text
        items.ITEM_NEEDS_OPEN = True
        on_page = anon.get("/board").text
        items.ITEM_NEEDS_OPEN = original_flag
        check("with item posting open, the board shows the item line even before any activity", 'id="item-stats"' in on_page)
        check("with item posting off and no item activity, the board shows no item line", 'id="item-stats"' not in page0)
    VER = make_user("its_ver", "verifier")
    req = make_user("its_req", "requester")
    d1, d2, d3 = (make_user("its_d%d" % i, "supporter") for i in (1, 2, 3))
    N, M, P = new_need(req, 2), new_need(req, 1), new_need(req, 1)

    k3 = make_claim(M, d3, "pledged")
    check("a pledged claim changes nothing", tuple(stats()) == tuple(before))
    k1 = make_claim(N, d1, "sent")
    check("a handed-over claim changes nothing", tuple(stats()) == tuple(before))
    run("UPDATE item_claims SET status = 'confirmed' WHERE id = :c", c=k1)
    check("one confirmed claim: +1 unit and +1 donor, no need fulfilled yet", delta(stats(), before) == (0, 0, 0, 0, 1, 1), delta(stats(), before))
    make_claim(N, d2, "confirmed")
    check("the second confirmed claim fulfils the need: +1 fulfilled (item), +2 units, +2 donors",
          delta(stats(), before) == (1, 0, 0, 1, 2, 2), delta(stats(), before))
    make_claim(P, d1, "confirmed")
    check("a donor who confirms a second claim is still one donor: +2 fulfilled, +3 units, +2 donors",
          delta(stats(), before) == (2, 0, 0, 2, 3, 2), delta(stats(), before))
    run("UPDATE item_claims SET status = 'cancelled' WHERE id = :c", c=k3)
    check("a cancelled claim changes nothing", delta(stats(), before) == (2, 0, 0, 2, 3, 2))
    check("the money figures did not move", stats().confirmed_try == before.confirmed_try and stats().donors == before.donors)
    after = stats()
    page = anon.get("/board").text
    check("the board now shows the item line with the three numbers",
          'id="item-stats"' in page and "<strong>%d</strong>" % after.fulfilled_items in page
          and "<strong>%d</strong>" % after.confirmed_units in page and "<strong>%d</strong>" % after.claimants in page)
    check("the money sentence is unchanged by item activity", 'id="stats"' in page and "confirmed by requesters" in page)
finally:
    cleanup()

print("\n%d of %d checks passed" % (total - fails, total))
sys.exit(1 if fails else 0)
