"""Part 4: the public board shows item needs."""
import getpass
import json
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
PW = "Itb-test-pass-1234"
PREFIX = "itb_"
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


def run(sql, **p):
    with admin.begin() as c:
        r = c.execute(text(sql), p)
        return r.scalar_one() if r.returns_rows else None


def cleanup():
    sub = "(SELECT id FROM needs WHERE requester_id IN (SELECT id FROM users WHERE username LIKE :p))"
    with admin.begin() as c:
        c.execute(text("DELETE FROM item_claims WHERE need_id IN " + sub), {"p": PREFIX + "%"})
        c.execute(text("DELETE FROM need_events WHERE need_id IN " + sub), {"p": PREFIX + "%"})
        c.execute(text("DELETE FROM needs WHERE requester_id IN (SELECT id FROM users WHERE username LIKE :p)"),
                  {"p": PREFIX + "%"})
        c.execute(text("DELETE FROM login_failures WHERE username LIKE :p"), {"p": PREFIX + "%"})
        c.execute(text("DELETE FROM users WHERE username LIKE :p"), {"p": PREFIX + "%"})


def make_user(u, role):
    return run("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r) RETURNING id",
               u=u, h=hash_password(PW), r=role)


def client_for(u):
    cl = TestClient(app, base_url=BASE)
    tok = csrf_from(cl.get("/login").text)
    code = cl.post("/login", data={"username": u, "password": PW, "csrf": tok}, follow_redirects=False).status_code
    assert code == 303, "login for %s gave %s" % (u, code)
    return cl


prov = next(iter(DISTRICTS_BY_PROVINCE))
dist = DISTRICTS_BY_PROVINCE[prov][0]


def new_item_need(requester_id, type_id, qty, public_text, spec=None):
    nid = run("INSERT INTO needs (requester_id, need_kind, item_type_id, quantity_needed, specification, used_ok, "
              "handover_method, province, district, household_size, description, contact_method, contact) VALUES "
              "(:u, 'item', :t, :q, :s, true, 'public_place', :p, :d, 3, 'ITBDESC private description', 'email', "
              "'itb_contact@example.com') RETURNING id",
              u=requester_id, t=type_id, q=qty, s=spec, p=prov, d=dist)
    run("UPDATE needs SET status = 'published', public_text = :t WHERE id = :i", t=public_text, i=nid)
    return nid


def new_claim(need_id, donor_id, units, to_confirmed):
    cid = run("INSERT INTO item_claims (need_id, donor_id, units, ack, contact_method, contact) "
              "VALUES (:n, :d, :u, true, 'email', 'itb_donor@example.com') RETURNING id", n=need_id, d=donor_id, u=units)
    if to_confirmed:
        run("UPDATE item_claims SET released_at = now(), released_by = :v WHERE id = :c", v=VER, c=cid)
        run("UPDATE item_claims SET status = 'sent' WHERE id = :c", c=cid)
        run("UPDATE item_claims SET status = 'confirmed' WHERE id = :c", c=cid)
    return cid


def cards(html):
    return [int(x) for x in re.findall(r'data-need="(\d+)"', html)]


def count(html):
    return int(re.search(r"(\d+) open need", html).group(1))


rep_id, rep_name, rep_cat = one("SELECT id, name, category FROM item_types WHERE enabled AND category IN ('mobility','medical_equipment') ORDER BY id LIMIT 1")
bed_id, bed_name, bed_cat = one("SELECT id, name, category FROM item_types WHERE enabled AND category = 'bedding' ORDER BY id LIMIT 1")
print("items:", rep_name, "(%s) and" % rep_cat, bed_name, "(%s)" % bed_cat)

cleanup()
try:
    VER = make_user("itb_ver", "verifier")
    req = make_user("itb_req", "requester")
    sup1 = make_user("itb_sup1", "supporter")
    sup2 = make_user("itb_sup2", "supporter")
    A = new_item_need(req, rep_id, 5, "Public text for the first item need ITBPUBA", spec="TOPSECRETSPEC")
    B = new_item_need(req, bed_id, 2, "Public text for the second item need ITBPUBB")
    pending = run("INSERT INTO needs (requester_id, need_kind, item_type_id, quantity_needed, used_ok, handover_method, "
                  "province, district, household_size, description, contact_method, contact) VALUES (:u, 'item', :t, 1, "
                  "true, 'public_place', :p, :d, 2, 'ITBDESC pending', 'email', 'itb_contact@example.com') RETURNING id",
                  u=req, t=bed_id, p=prov, d=dist)
    try:
        c1 = new_claim(A, sup1, 1, True)
        c2 = new_claim(A, sup2, 1, False)
        claims_ok = True
    except Exception as e:
        claims_ok = False
        print("claim setup failed:", str(e).splitlines()[0])
    check("0. test claims could be created", claims_ok)
    anon = TestClient(app, base_url=BASE)
    sup = client_for("itb_sup1")

    print("-- the board")
    page = anon.get("/board")
    check("1. the board renders with item needs published", page.status_code == 200)
    check("2. the stats card still renders", 'id="stats"' in page.text)
    items_page = anon.get("/board?kind=item")
    t = items_page.text
    check("3. kind=item lists exactly the two item needs", items_page.status_code == 200 and sorted(cards(t)) == sorted([A, B])
          and count(t) == 2)
    check("4. item cards show the name and the number of units",
          str(escape(rep_name)) in t and "Needs 5 units" in t and "Needs 2 units" in t)
    check("5. item cards have no goal in money and no euro note", "Goal " not in t and "euro goal" not in t)
    check("6. funding line counts units, promised and donors",
          "1 of 5 units confirmed, 1 more promised, 2 donors." in t)
    check("7. a need with no claims says no pledges yet", "No pledges yet." in t)
    check("8. the bar uses units: 20 percent confirmed and 20 percent more promised",
          "20 percent confirmed, 20 percent more promised" in t)
    check("9. kind=money does not show the item needs",
          A not in cards(anon.get("/board?kind=money").text) and B not in cards(anon.get("/board?kind=money").text))
    check("10. category filter finds the matching item need only",
          cards(anon.get("/board?category=" + rep_cat).text).count(A) == 1 and B not in cards(anon.get("/board?category=" + rep_cat).text))
    check("11. the purpose filter leaves item needs out",
          A not in cards(anon.get("/board?purpose=medical").text))
    check("12. an unknown kind gives 400", anon.get("/board?kind=bogus").status_code == 400)
    check("13. an unknown category gives 400", anon.get("/board?category=bogus").status_code == 400)
    least = cards(anon.get("/board?kind=item&sort=least").text)
    near = cards(anon.get("/board?kind=item&sort=near").text)
    check("14. least funded puts the empty need first", least.index(B) < least.index(A))
    check("15. closest to the goal puts the funded need first", near.index(A) < near.index(B))
    check("16. mixed sorting with money needs still renders", anon.get("/board?sort=least").status_code == 200
          and anon.get("/board?sort=near").status_code == 200)
    check("17. no private detail reaches the board",
          all(x not in t for x in ("TOPSECRETSPEC", "ITBDESC", "itb_contact@example.com", "itb_donor@example.com")))
    md = anon.get("/board/map-data")
    check("18. the map data still works", md.status_code == 200 and isinstance(json.loads(md.text), list))

    print("-- the need page")
    pa = anon.get("/board/%d" % A)
    check("19. the item need page renders", pa.status_code == 200 and str(escape(rep_name)) in pa.text and "Needs 5 units" in pa.text)
    check("20. it explains how item giving works and tells a visitor to sign in to offer help",
          "How giving works" in pa.text and "to offer help" in pa.text)
    check("21. it shows the condition and the preferred handover",
          "A used item is fine" in pa.text and "Meet in a public place, in daylight" in pa.text)
    check("22. it has no money pledge steps, euro estimate or pledge link",
          all(x not in pa.text for x in ("Pledge an amount", "/support/pledge/", "lira", "account details")))
    ps = sup.get("/board/%d" % A)
    check("23. a signed-in supporter sees the offer link and no money pledge link", ps.status_code == 200 and "/support/pledge/" not in ps.text and "/support/item/%d" % A in ps.text)
    check("24. the need page leaks no private detail",
          all(x not in pa.text for x in ("TOPSECRETSPEC", "ITBDESC", "itb_contact@example.com", "itb_donor@example.com")))
    check("25. an unpublished item need gives 404", anon.get("/board/%d" % pending).status_code == 404)
    money = one("SELECT id FROM public_needs WHERE need_kind = 'money' ORDER BY id LIMIT 1")
    if money:
        pm = anon.get("/board/%d" % money.id)
        check("26. a money need page is unchanged", pm.status_code == 200 and "Pledge an amount" in pm.text)
    else:
        print("note: no published money need in this database, check 26 skipped (run seed_demo.py to get some)")
finally:
    cleanup()

print("\n%d of %d checks passed" % (total - fails, total))
sys.exit(1 if fails else 0)
