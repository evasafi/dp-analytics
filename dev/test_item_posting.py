"""Part 2: the requester item posting form. Checks the switch, access rules, validation, saving, and the mine page."""
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
PW = "Itp-test-pass-1234"
PREFIX = "itp_"
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
        c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r)"),
                  {"u": u, "h": hash_password(PW), "r": role})


def client_for(u):
    cl = TestClient(app, base_url=BASE)
    tok = csrf_from(cl.get("/login").text)
    code = cl.post("/login", data={"username": u, "password": PW, "csrf": tok}, follow_redirects=False).status_code
    assert code == 303, "login for %s gave %s" % (u, code)
    return cl


def tok_of(cl, path):
    return csrf_from(cl.get(path).text)


def n_needs(u):
    return one("SELECT count(*) FROM needs WHERE requester_id = (SELECT id FROM users WHERE username = :u)", u=u)[0]


prov = next(iter(DISTRICTS_BY_PROVINCE))
dist = DISTRICTS_BY_PROVINCE[prov][0]
walker = one("SELECT id FROM item_types WHERE name = 'Walker' AND enabled")[0]
disabled, disabled_name = one("SELECT id, name FROM item_types WHERE NOT enabled ORDER BY id LIMIT 1")
print("using disabled type:", disabled_name)
GOOD = {"item_type_id": str(walker), "quantity_needed": "2", "specification": "Folding, adult size",
        "used_ok": "used", "handover_method": "public_place", "province": prov, "district": dist,
        "household_size": "3", "description": "ITPKEEP my grandmother needs a walker after surgery.",
        "contact_method": "email", "contact": "itp_req@example.com"}

original = items.ITEM_NEEDS_OPEN
cleanup()
try:
    make_user("itp_req", "requester")
    make_user("itp_req2", "requester")
    make_user("itp_sup", "supporter")
    r1 = client_for("itp_req")
    r2 = client_for("itp_req2")
    sup = client_for("itp_sup")
    anon = TestClient(app, base_url=BASE)
    anon_tok = csrf_from(anon.get("/login").text)

    print("-- switch off")
    items.ITEM_NEEDS_OPEN = False
    t = tok_of(r1, "/needs/new")
    check("1. flag off: GET item form is 404", r1.get("/needs/new/item").status_code == 404)
    check("2. flag off: POST item form is 404 and saves nothing",
          r1.post("/needs/new/item", data={**GOOD, "csrf": t}, follow_redirects=False).status_code == 404
          and n_needs("itp_req") == 0)
    check("3. flag off: money page has no link to the item form", "/needs/new/item" not in r1.get("/needs/new").text)

    print("-- switch on")
    items.ITEM_NEEDS_OPEN = True
    check("4. flag on: money page links to the item form", 'href="/needs/new/item"' in r1.get("/needs/new").text)
    page = r1.get("/needs/new/item")
    check("5. item form loads for a requester", page.status_code == 200)
    check("6. form lists an enabled type (Walker)", "Walker" in page.text)
    check("7. form does not list a disabled type", disabled_name not in page.text)
    check("8. form has no IBAN field", "iban" not in page.text.lower())
    check("9. form has the safety note", "cannot guarantee" in page.text)
    check("10. supporter gets 403 on GET", sup.get("/needs/new/item").status_code == 403)
    ts = tok_of(sup, "/pledges") if False else csrf_from(sup.get("/").text) if 'name="csrf"' in sup.get("/").text else "x"
    check("11. supporter gets 403 on POST", sup.post("/needs/new/item", data={**GOOD, "csrf": ts}).status_code == 403)
    check("12. visitor gets 401 on GET", anon.get("/needs/new/item").status_code == 401)
    check("13. visitor gets 401 on POST", anon.post("/needs/new/item", data={**GOOD, "csrf": anon_tok}).status_code == 401)

    print("-- validation")
    t = csrf_from(r1.get("/needs/new/item").text)
    bad = {
        "14. no item type": {"item_type_id": ""},
        "15. item type not a number": {"item_type_id": "abc"},
        "16. disabled item type": {"item_type_id": str(disabled)},
        "17. nonexistent item type": {"item_type_id": "999999"},
        "18. quantity 0": {"quantity_needed": "0"},
        "19. quantity 51": {"quantity_needed": "51"},
        "20. quantity text": {"quantity_needed": "two"},
        "21. quantity negative": {"quantity_needed": "-1"},
        "22. specification over 300": {"specification": "x" * 301},
        "23. condition missing": {"used_ok": ""},
        "24. condition invalid": {"used_ok": "maybe"},
        "25. handover missing": {"handover_method": ""},
        "26. handover invalid": {"handover_method": "teleport"},
        "27. province invalid": {"province": "Atlantis"},
        "28. district not in province": {"district": "Nowhere"},
        "29. household 0": {"household_size": "0"},
        "30. household 13": {"household_size": "13"},
        "31. description too short": {"description": "short"},
        "32. description over 1000": {"description": "d" * 1001},
        "33. contact invalid email": {"contact_method": "email", "contact": "not-an-email"},
    }
    for label, over in bad.items():
        before = n_needs("itp_req")
        resp = r1.post("/needs/new/item", data={**GOOD, **over, "csrf": t}, follow_redirects=False)
        check(label + ": 400 and nothing saved", resp.status_code == 400 and n_needs("itp_req") == before
              and "Nothing was saved" in resp.text)
    resp = r1.post("/needs/new/item", data={**GOOD, "quantity_needed": "0", "csrf": t}, follow_redirects=False)
    check("34. typed values are kept after an error", "ITPKEEP" in resp.text and "Folding, adult size" in resp.text)
    check("35. forged csrf gives 403 and saves nothing",
          r1.post("/needs/new/item", data={**GOOD, "csrf": "forged"}, follow_redirects=False).status_code == 403
          and n_needs("itp_req") == 0)

    print("-- saving")
    t = csrf_from(r1.get("/needs/new/item").text)
    resp = r1.post("/needs/new/item", data={**GOOD, "csrf": t}, follow_redirects=False)
    check("36. valid post redirects to /needs/mine?done=1",
          resp.status_code == 303 and resp.headers.get("location") == "/needs/mine?done=1")
    row = one("SELECT n.id, n.need_kind, n.goal, n.payee_name, n.status, n.item_type_id, n.quantity_needed, n.used_ok, "
              "n.handover_method, n.specification FROM needs n JOIN users u ON u.id = n.requester_id WHERE u.username = 'itp_req'")
    check("37. row saved as an item need, pending, no goal and no payee",
          row is not None and row.need_kind == "item" and row.goal is None and row.payee_name is None
          and row.status == "pending")
    check("38. row keeps the chosen values",
          row is not None and row.item_type_id == walker and row.quantity_needed == 2 and row.used_ok is True
          and row.handover_method == "public_place" and row.specification == "Folding, adult size")
    ev = one("SELECT count(*) FROM need_events WHERE need_id = :n AND event = 'submitted'", n=row.id)[0] if row else 0
    check("39. a 'submitted' event was recorded", ev == 1)
    mine = r1.get("/needs/mine?done=1")
    check("40. /needs/mine renders the item need", mine.status_code == 200 and "Item: Walker" in mine.text
          and "2 units" in mine.text)

    print("-- pending limit")
    t2 = csrf_from(r2.get("/needs/new/item").text)
    codes = [r2.post("/needs/new/item", data={**GOOD, "csrf": t2}, follow_redirects=False).status_code for _ in range(4)]
    check("41. three pending allowed, the fourth gives 429", codes == [303, 303, 303, 429])
    check("42. only three rows exist for that requester", n_needs("itp_req2") == 3)
finally:
    items.ITEM_NEEDS_OPEN = original
    cleanup()

print("\n%d of %d checks passed" % (total - fails, total))
sys.exit(1 if fails else 0)
