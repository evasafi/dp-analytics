import getpass
import re
from urllib.parse import quote

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.auth import hash_password
from app.main import app

owner = getpass.getpass("postgres password: ")
admin = create_engine(f"postgresql+psycopg://postgres:{quote(owner, safe='')}@localhost:5432/dp_analytics")
PW = "throwaway-password-1"


def cleanup(c):
    u = "(SELECT id FROM users WHERE starts_with(username, 'sl_'))"
    n = f"(SELECT id FROM needs WHERE requester_id IN {u})"
    c.execute(text(f"DELETE FROM need_events WHERE need_id IN {n} OR actor_id IN {u}"))
    c.execute(text(f"DELETE FROM pledges WHERE need_id IN {n} OR supporter_id IN {u}"))
    c.execute(text(f"DELETE FROM needs WHERE requester_id IN {u}"))
    c.execute(text("DELETE FROM users WHERE starts_with(username, 'sl_')"))


fails = []


def check(label, ok, extra=""):
    print(("OK   " if ok else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        fails.append(label)


with admin.begin() as c:
    cleanup(c)
    for u, role in (("sl_sup", "supporter"), ("sl_req", "requester")):
        c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r)"),
                  {"u": u, "h": hash_password(PW), "r": role})
    rq = c.execute(text("SELECT id FROM users WHERE username = 'sl_req'")).scalar_one()
    sp = c.execute(text("SELECT id FROM users WHERE username = 'sl_sup'")).scalar_one()
    nid = c.execute(text(
        "INSERT INTO needs (requester_id, category, province, district, description, contact, contact_method, public_text, status) "
        "VALUES (:u, 'household', 'Istanbul', 'Uskudar', 'PRIVATE-DESC label test', 'PRIVATE-CONTACT', 'phone', "
        "'Label test need: a household item is needed.', 'published') RETURNING id"), {"u": rq}).scalar_one()
    c.execute(text("UPDATE needs SET status = 'pledged' WHERE id = :i"), {"i": nid})
    c.execute(text("UPDATE needs SET status = 'fulfilled' WHERE id = :i"), {"i": nid})
    for st in ("cancelled", "cancelled", "handed_over"):
        c.execute(text("INSERT INTO pledges (need_id, supporter_id, status, contact_method, contact) "
                       "VALUES (:n, :s, :st, 'phone', '0555 111 22 33')"), {"n": nid, "s": sp, "st": st})

try:
    cl = TestClient(app, base_url="http://127.0.0.1:8000")
    t = re.search(r'name="csrf" value="([^"]+)"', cl.get("/login").text).group(1)
    assert cl.post("/login", data={"username": "sl_sup", "password": PW, "csrf": t}, follow_redirects=False).status_code == 303
    page = cl.get("/support").text
    check("a need with two cancelled pledges and one fulfilled pledge shows one Fulfilled and two Cancelled",
          page.count("<td>Fulfilled</td>") == 1 and page.count("<td>Cancelled</td>") == 2,
          f"fulfilled={page.count('<td>Fulfilled</td>')}, cancelled={page.count('<td>Cancelled</td>')}")
    check("no cancel button on any of them", "/support/cancel/" not in page)
    check("none of them counts toward the cap", "0 of 3 active" in page)
finally:
    with admin.begin() as c:
        cleanup(c)
    print("throwaway accounts, need and pledges removed")

assert not fails, f"failed: {fails}"
print("PASS")
