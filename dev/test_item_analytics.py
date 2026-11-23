"""Part 7a: item needs in the needs analytics. Item groupings exist on the live dataset only."""
import getpass
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import needs_engine as ne
from app.auth import hash_password
from app.config import settings
from app.db import engine as app_engine
from app.districts import DISTRICTS_BY_PROVINCE
from app.engine import Spec
from app.routes.analytics import GROUPINGS, _spec

admin = create_engine(make_url(settings.database_url).set(username="postgres", password=getpass.getpass("postgres password: ")))
PW = "Ita-test-pass-1234"
PREFIX = "ita_"
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
heat_id = one("SELECT id FROM item_types WHERE enabled AND category = 'heating' ORDER BY id LIMIT 1")[0]


def new_need(requester_id, type_id, qty, publish=True):
    nid = run("INSERT INTO needs (requester_id, need_kind, item_type_id, quantity_needed, used_ok, handover_method, province, "
              "district, household_size, description, contact_method, contact) VALUES (:u, 'item', :t, :q, true, 'public_place', "
              ":p, :d, 3, 'ITA test need for review', 'email', 'ita_req_contact@example.com') RETURNING id",
              u=requester_id, t=type_id, q=qty, p=prov, d=dist)
    if publish:
        run("UPDATE needs SET status = 'published', public_text = 'Public text for the analytics tests' WHERE id = :i", i=nid)
    return nid


def confirm_one_unit(nid, donor_id, releaser_id):
    cid = run("INSERT INTO item_claims (need_id, donor_id, units, ack, contact_method, contact) "
              "VALUES (:n, :d, 1, true, 'email', 'ita_donor@example.com') RETURNING id", n=nid, d=donor_id)
    run("UPDATE item_claims SET released_at = now(), released_by = :v WHERE id = :c", v=releaser_id, c=cid)
    run("UPDATE item_claims SET status = 'sent' WHERE id = :c", c=cid)
    run("UPDATE item_claims SET status = 'confirmed' WHERE id = :c", c=cid)


def counts(dims, via=None):
    with (via or admin).connect() as c:
        return ne.true_counts(c, "needs_live", Spec("count", dims))


def moved(a, b):
    return {k: a[k] - b[k] for k in a if a[k] != b[k]}


def refused(dataset, dims):
    try:
        ne.validate_needs(Spec("count", dims), dataset)
        return False
    except ValueError:
        return True


cleanup()
try:
    check("the new groupings are in the analyst form list", {"kind", "item_category", "item_category_status"} <= set(GROUPINGS))
    check("the live dataset accepts the item groupings",
          all(not refused("needs_live", d) for d in (("kind",), ("item_category",), ("item_category", "status"))))
    check("the synthetic dataset refuses kind and item category (nothing to read there)",
          refused("needs_synthetic", ("kind",)) and refused("needs_synthetic", ("item_category",))
          and refused("needs_synthetic", ("item_category", "status")))
    check("other mixes are still refused",
          refused("needs_live", ("kind", "status")) and refused("needs_live", ("item_category", "province")))
    try:
        _spec("needs_synthetic", "kind")
        route_ok = False
    except ValueError:
        route_ok = True
    check("the route helper refuses an item grouping on the synthetic dataset", route_ok and _spec("needs_live", "item_category").group_by == ("item_category",))
    ncat = one("SELECT count(DISTINCT category) FROM item_types")[0]
    check("fixed cell counts: kind 2, categories %d, categories x status %d" % (ncat, 2 * ncat),
          ne.cell_count(Spec("count", ("kind",))) == 2 and ne.cell_count(Spec("count", ("item_category",))) == ncat
          and ne.cell_count(Spec("count", ("item_category", "status"))) == 2 * ncat)

    check("no money row has a category and no item row lacks one",
          one("SELECT count(*) FROM needs_analytics WHERE kind = 'money' AND item_category IS NOT NULL")[0] == 0
          and one("SELECT count(*) FROM needs_analytics WHERE kind = 'item' AND item_category IS NULL")[0] == 0)
    check("every verified money need is still in the view",
          one("SELECT count(*) FROM needs_analytics WHERE kind = 'money'")[0]
          == one("SELECT count(*) FROM needs WHERE need_kind = 'money' AND status IN ('published', 'fulfilled')")[0])

    b_kind, b_cat, b_cs = counts(("kind",)), counts(("item_category",)), counts(("item_category", "status"))
    b_prov, b_status, b_type, b_band = counts(("province",)), counts(("status",)), counts(("need_type",)), counts(("band",))

    VER = make_user("ita_ver", "verifier")
    ra, rb, rc = make_user("ita_ra", "requester"), make_user("ita_rb", "requester"), make_user("ita_rc", "requester")
    donor = make_user("ita_donor", "supporter")
    for _ in range(5):
        new_need(ra, bed_id, 1)                    # five published bedding needs from one person
    fulfilled = new_need(rb, heat_id, 1)           # one heating need, fulfilled by a confirmed unit
    confirm_one_unit(fulfilled, donor, VER)
    new_need(rc, bed_id, 1, publish=False)         # pending: not in the view

    check("the heating need really is fulfilled", one("SELECT status FROM needs WHERE id = :i", i=fulfilled)[0] == "fulfilled")
    a_kind, a_cat, a_cs = counts(("kind",)), counts(("item_category",)), counts(("item_category", "status"))
    check("kind: one person's five item needs count as 3 (the cap), plus the fulfilled one; pending ignored; money unchanged",
          moved(a_kind, b_kind) == {("item",): 4}, moved(a_kind, b_kind))
    check("item category: bedding +3, heating +1, nothing else",
          moved(a_cat, b_cat) == {("bedding",): 3, ("heating",): 1}, moved(a_cat, b_cat))
    check("category x status: bedding published +3, heating fulfilled +1",
          moved(a_cs, b_cs) == {("bedding", "published"): 3, ("heating", "fulfilled"): 1}, moved(a_cs, b_cs))
    check("status counts now include item needs", moved(counts(("status",)), b_status) == {("published",): 3, ("fulfilled",): 1})
    if prov in ne.dim_values("province"):
        check("province counts now include item needs", sum(moved(counts(("province",)), b_prov).values()) == 4)
    else:
        check("the test province is in the province list", False, prov)
    check("need type counts ignore item needs", moved(counts(("need_type",)), b_type) == {})
    check("goal band counts ignore item needs", moved(counts(("band",)), b_band) == {})
    check("the app role reads the new columns and gets the same counts", counts(("kind",), via=app_engine) == a_kind)
    with open("app/templates/analyst.html", encoding="utf-8") as f:
        note = f.read()
    check("the analyst note explains the item groupings and the shared cap",
          "item needs" in note and "both kinds" in note and "cells below 30 are suppressed" in note)
finally:
    cleanup()

print("\n%d of %d checks passed" % (total - fails, total))
sys.exit(1 if fails else 0)
