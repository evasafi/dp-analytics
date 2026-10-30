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
    u = "(SELECT id FROM users WHERE starts_with(username, 'ct_'))"
    n = f"(SELECT id FROM needs WHERE requester_id IN {u})"
    c.execute(text(f"DELETE FROM need_events WHERE need_id IN {n} OR actor_id IN {u}"))
    c.execute(text(f"DELETE FROM needs WHERE requester_id IN {u}"))
    c.execute(text("DELETE FROM users WHERE starts_with(username, 'ct_')"))
    c.execute(text("DELETE FROM login_failures WHERE username = '__register__'"))


def q(sql, **p):
    with admin.connect() as c:
        return c.execute(text(sql), p).all()


def stored(user):
    return [(r[0], r[1]) for r in q("SELECT contact_method, contact FROM needs WHERE requester_id = "
                                     "(SELECT id FROM users WHERE username = :u) ORDER BY id", u=user)]


def token(cl, path):
    return re.search(r'name="csrf" value="([^"]+)"', cl.get(path).text).group(1)


def new_client():
    return TestClient(app, base_url="http://127.0.0.1:8000")


def register(user):
    cl = new_client()
    r = cl.post("/register", data={"username": user, "password": PW, "role": "requester", "csrf": token(cl, "/register")},
                follow_redirects=False)
    assert r.status_code == 303, f"register {user}: {r.status_code}"
    return cl


def post(cl, method, contact):
    data = {"category": "health", "province": "Istanbul", "district": "Uskudar",
            "description": "needs a wheelchair for a family member", "contact": contact, "csrf": token(cl, "/needs/new")}
    if method is not None:
        data["contact_method"] = method
    return cl.post("/needs/new", data=data, follow_redirects=False).status_code


fails = []


def check(label, ok, extra=""):
    print(("OK   " if ok else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        fails.append(label)


with admin.begin() as c:
    cleanup(c)
    c.execute(text("INSERT INTO users (username, password_hash, role) VALUES ('ct_ver', :h, 'verifier')"), {"h": hash_password(PW)})

try:
    R1, R2 = register("ct_req1"), register("ct_req2")
    page = R1.get("/needs/new").text
    check("form offers the four methods", all(f'<option value="{m}">' in page for m in ("phone", "email", "whatsapp", "other"))
          and 'name="contact_method"' in page)

    good = [post(R1, "phone", "0555 123 45 67"), post(R1, "email", "ayse@example.com"), post(R1, "whatsapp", "+90 555 123 45 67")]
    check("valid phone, email and WhatsApp accepted: " + str(good), good == [303, 303, 303])
    check("method and contact are stored as given",
          stored("ct_req1") == [("phone", "0555 123 45 67"), ("email", "ayse@example.com"), ("whatsapp", "+90 555 123 45 67")], str(stored("ct_req1")))

    bad = [("phone", "call me maybe"), ("phone", "12345"), ("phone", "1" * 20), ("email", "ayse.example.com"),
           ("email", "a b@example.com"), ("whatsapp", "not a number"), ("carrier-pigeon", "0555 123 45 67"),
           ("phone; DROP TABLE needs", "0555 123 45 67"), ("other", "ab")]
    codes = [post(R1, m, c) for m, c in bad]
    check("malformed contacts and unknown methods refused (all 400): " + str(codes), all(c == 400 for c in codes))
    check("refused posts saved nothing", len(stored("ct_req1")) == 3)

    other = post(R2, "other", "ask for Ayse at the clinic")
    default = post(R2, None, "via verifier")
    check("'other' accepted, and a missing method defaults to other: " + str([other, default]), [other, default] == [303, 303])
    check("both stored as other", stored("ct_req2") == [("other", "ask for Ayse at the clinic"), ("other", "via verifier")], str(stored("ct_req2")))
    check("the pending limit still applies (429)", post(R1, "phone", "0555 999 88 77") == 429)

    V = new_client()
    r = V.post("/login", data={"username": "ct_ver", "password": PW, "csrf": token(V, "/login")}, follow_redirects=False)
    assert r.status_code == 303
    nid = q("SELECT id FROM needs WHERE contact = '0555 123 45 67'")[0][0]
    eid = q("SELECT id FROM needs WHERE contact = 'ayse@example.com'")[0][0]
    check("verifier sees the method and the phone number", "phone: 0555 123 45 67" in V.get(f"/verify/{nid}").text)
    check("verifier sees the method and the email", "email: ayse@example.com" in V.get(f"/verify/{eid}").text)
    mine = R1.get("/needs/mine").text
    check("the requester's own list does not repeat the contact", "0555 123 45 67" not in mine and "ayse@example.com" not in mine)
    check("anonymous visitors cannot read the review page (401)", new_client().get(f"/verify/{nid}").status_code == 401)
finally:
    with admin.begin() as c:
        cleanup(c)
    print("throwaway accounts and needs removed")

assert not fails, f"failed: {fails}"
print("PASS")
