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
    u = "(SELECT id FROM users WHERE starts_with(username, 'fe_'))"
    n = f"(SELECT id FROM needs WHERE requester_id IN {u})"
    c.execute(text(f"DELETE FROM need_events WHERE need_id IN {n} OR actor_id IN {u}"))
    c.execute(text(f"DELETE FROM pledges WHERE need_id IN {n} OR supporter_id IN {u}"))
    c.execute(text(f"DELETE FROM needs WHERE requester_id IN {u}"))
    c.execute(text("DELETE FROM users WHERE starts_with(username, 'fe_')"))


def one(sql, **p):
    with admin.connect() as c:
        return c.execute(text(sql), p).scalar_one()


def tok(cl, path):
    return re.search(r'name="csrf" value="([^"]+)"', cl.get(path).text).group(1)


def login(user):
    cl = TestClient(app, base_url="http://127.0.0.1:8000")
    t = re.search(r'name="csrf" value="([^"]+)"', cl.get("/login").text).group(1)
    code = cl.post("/login", data={"username": user, "password": PW, "csrf": t}, follow_redirects=False).status_code
    assert code == 303, f"login {user}: {code}"
    return cl


fails = []


def check(label, ok, extra=""):
    print(("OK   " if ok else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        fails.append(label)


with admin.begin() as c:
    cleanup(c)
    for u, role in (("fe_req", "requester"), ("fe_sup", "supporter")):
        c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r)"),
                  {"u": u, "h": hash_password(PW), "r": role})
    rq = c.execute(text("SELECT id FROM users WHERE username = 'fe_req'")).scalar_one()
    NEED = c.execute(text(
        "INSERT INTO needs (requester_id, category, province, district, description, contact, contact_method, public_text, status) "
        "VALUES (:u, 'household', 'Istanbul', 'Uskudar', 'PRIVATE-DESC fe test', 'PRIVATE-CONTACT-fe', 'phone', "
        "'Form test need: a household item is needed.', 'published') RETURNING id"), {"u": rq}).scalar_one()


def saved():
    return one("SELECT count(*) FROM needs WHERE requester_id = (SELECT id FROM users WHERE username = 'fe_req') "
               "AND status = 'pending'")


def kept(html, d):
    return all(s in html for s in (
        f'<option value="{d["category"]}" selected>', f'<option value="{d["province"]}" selected>',
        f'<option value="{d["district"]}" selected>', f'<option value="{d["contact_method"]}" selected>',
        f'value="{d["contact"]}"'))


try:
    R = login("fe_req")
    BASE = {"category": "culture_art", "province": "Bilecik", "district": "Pazaryeri",
            "description": "I need traditional clothing for an event", "contact_method": "email", "contact": "demo@example.com"}

    def post_need(**over):
        d = {**BASE, **over}
        return R.post("/needs/new", data={**d, "csrf": tok(R, "/needs/new")}, follow_redirects=False), d

    r, d = post_need(description="short")
    check("a too-short description is refused (400) and the message gives its length",
          r.status_code == 400 and "The description must be 10 to 1000 characters (it is 5)." in r.text)
    check("the refused form keeps the category, province, district, contact method, contact and description",
          kept(r.text, d) and ">short</textarea>" in r.text)
    check("a stale error message is not shown on a fresh form", "Nothing was saved" not in R.get("/needs/new").text)
    check("nothing was saved by the refusal", saved() == 0)

    cases = [("email", "not-an-email", "does not look like an email"), ("phone", "call me", "7 to 15 digits"),
             ("whatsapp", "12", "7 to 15 digits"), ("other", "ab", "3 to 200 characters")]
    for method, contact, expect in cases:
        r, d = post_need(contact_method=method, contact=contact)
        check(f"bad {method} contact: 400, the message says what is wrong, the form keeps the values, nothing saved",
              r.status_code == 400 and expect in r.text and kept(r.text, d) and saved() == 0)
    r, _ = post_need(contact_method="carrier-pigeon", contact="x1y2z3")
    check("an unknown contact method: 400 and 'Choose how a verifier can reach you'",
          r.status_code == 400 and "Choose how a verifier can reach you" in r.text and saved() == 0)

    r, _ = post_need(district="Aladag")
    check("a district from another province: 400 with a specific message", r.status_code == 400 and "belongs to the province" in r.text and saved() == 0)
    r, _ = post_need(category="weapons")
    check("an unknown category: 400 with a specific message", r.status_code == 400 and "Choose a category." in r.text and saved() == 0)

    r, _ = post_need(description="<script>alert(1)</script> padding text", contact="\"><b>x</b>")
    check("hostile text typed into the form is escaped when it comes back",
          r.status_code == 400 and "<script>alert(1)</script>" not in r.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
          and "<b>x</b>" not in r.text)
    check("the description box has a minlength of 10", 'minlength="10"' in R.get("/needs/new").text)

    r, _ = post_need()
    check("a valid post is still accepted (303) and saved", r.status_code == 303 and saved() == 1)
    a, b = post_need()[0].status_code, post_need()[0].status_code
    r, d = post_need(description="Fourth need keeps its text")
    check("at the pending limit: 429, the message says why, and the form keeps the typed text: " + str([a, b, r.status_code]),
          [a, b] == [303, 303] and r.status_code == 429 and "3 needs waiting" in r.text and ">Fourth need keeps its text</textarea>" in r.text
          and saved() == 3)

    S = login("fe_sup")

    def post_pledge(method, contact):
        return S.post(f"/support/pledge/{NEED}", data={"contact_method": method, "contact": contact,
                                                       "csrf": tok(S, f"/support/pledge/{NEED}")}, follow_redirects=False)

    g = S.get(f"/support/pledge/{NEED}")
    check("the pledge form loads with no error shown", g.status_code == 200 and "Nothing was saved" not in g.text)
    for method, contact, expect in [("phone", "call me", "7 to 15 digits"), ("email", "nope", "does not look like an email"),
                                    ("whatsapp", "12", "7 to 15 digits"), ("other", "ab", "3 to 200 characters")]:
        r = post_pledge(method, contact)
        check(f"pledge with a bad {method} contact: 400, specific message, form keeps the values, need left published",
              r.status_code == 400 and expect in r.text and "Nothing was saved." in r.text
              and f'<option value="{method}" selected>' in r.text and f'value="{contact}"' in r.text
              and one("SELECT status FROM needs WHERE id = :i", i=NEED) == "published"
              and one("SELECT count(*) FROM pledges WHERE need_id = :i", i=NEED) == 0)
    r = post_pledge("phone", "<i>x</i>")
    check("hostile text in the pledge form is escaped when it comes back", r.status_code == 400 and "<i>x</i>" not in r.text and "&lt;i&gt;x&lt;/i&gt;" in r.text)
    check("a refused pledge page shows the public text and no private detail",
          "Form test need:" in r.text and "PRIVATE" not in r.text)
    r = post_pledge("phone", "0555 111 22 33")
    check("a valid pledge is still accepted (303) and the need is pledged",
          r.status_code == 303 and one("SELECT status FROM needs WHERE id = :i", i=NEED) == "pledged")
finally:
    with admin.begin() as c:
        cleanup(c)
    print("throwaway accounts, needs and pledges removed")

assert not fails, f"failed: {fails}"
print("PASS")
