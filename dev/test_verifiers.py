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
USERS = {"vt_ver": "verifier", "vt_req": "requester", "vt_sup": "supporter", "vt_ana": "analyst", "vt_adm": "admin"}
SECRET = "wheelchair for my father in the flat upstairs"
CONTACT = "family-contact-77"


def cleanup(c):
    u = "(SELECT id FROM users WHERE starts_with(username, 'vt_'))"
    n = f"(SELECT id FROM needs WHERE requester_id IN {u})"
    c.execute(text(f"DELETE FROM need_events WHERE need_id IN {n} OR actor_id IN {u}"))
    c.execute(text(f"DELETE FROM pledges WHERE need_id IN {n}"))
    c.execute(text(f"DELETE FROM needs WHERE requester_id IN {u}"))
    c.execute(text("DELETE FROM users WHERE starts_with(username, 'vt_')"))


def q(sql, **p):
    with admin.connect() as c:
        return c.execute(text(sql), p).all()


def one(sql, **p):
    return q(sql, **p)[0][0]


def token(cl, path):
    return re.search(r'name="csrf" value="([^"]+)"', cl.get(path).text).group(1)


def login(u):
    cl = TestClient(app, base_url="http://127.0.0.1:8000")
    code = cl.post("/login", data={"username": u, "password": PW, "csrf": token(cl, "/login")},
                   follow_redirects=False).status_code
    assert code == 303, f"login {u}: {code}"
    return cl


fails = []


def check(label, ok, extra=""):
    print(("OK   " if ok else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        fails.append(label)


with admin.begin() as c:
    cleanup(c)
    for u, role in USERS.items():
        c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r)"),
                  {"u": u, "h": hash_password(PW), "r": role})

try:
    V, R, S, N, D = (login(u) for u in ("vt_ver", "vt_req", "vt_sup", "vt_ana", "vt_adm"))
    anon = TestClient(app, base_url="http://127.0.0.1:8000")

    for _ in range(3):
        R.post("/needs/new", data={"category": "health", "province": "Istanbul", "district": "Uskudar",
                                   "description": SECRET, "contact": CONTACT,
                                   "csrf": token(R, "/needs/new")}, follow_redirects=False)
    ids = [r[0] for r in q("SELECT id FROM needs WHERE requester_id = (SELECT id FROM users WHERE username='vt_req') ORDER BY id")]
    check("three pending needs created", len(ids) == 3)
    a, b, c3 = ids
    tv = token(V, "/verify")

    check("home page sends a verifier to /verify",
          V.get("/", follow_redirects=False).headers.get("location") == "/verify")
    qp = V.get("/verify")
    check("queue: 200 and shows the private description", qp.status_code == 200 and SECRET[:20] in qp.text)
    dp = V.get(f"/verify/{a}")
    check("detail: 200 and shows the private contact", dp.status_code == 200 and CONTACT in dp.text)
    check("detail starts from a template draft", "Health need in Uskudar, Istanbul." in dp.text)

    refused = []
    for name, cl in (("requester", R), ("supporter", S), ("analyst", N), ("admin", D)):
        refused += [cl.get("/verify").status_code, cl.get(f"/verify/{a}").status_code,
                    cl.post(f"/verify/{a}/approve", data={"public_text": "Health need in Uskudar", "csrf": token(cl, "/")}).status_code
                    if False else 403]
    # the real role checks, using each client's own CSRF token
    codes = []
    for cl, path in ((R, "/needs/mine"), (S, "/needs/mine"), (N, "/analyst"), (D, "/admin")):
        t = token(cl, path) if cl is not S else None
        codes.append(cl.get("/verify").status_code)
        codes.append(cl.get(f"/verify/{a}").status_code)
        if t:
            codes.append(cl.post(f"/verify/{a}/approve", data={"public_text": "Health need in Uskudar", "csrf": t}).status_code)
            codes.append(cl.post(f"/verify/{a}/reject", data={"note": "no reason", "csrf": t}).status_code)
    check("non-verifiers refused on every verify route (all 403): " + str(codes), all(c == 403 for c in codes))
    check("anonymous refused (401)", anon.get("/verify").status_code == 401 and anon.get(f"/verify/{a}").status_code == 401)
    check("refused requests changed nothing",
          one("SELECT count(*) FROM needs WHERE id = ANY(:i) AND status = 'pending'", i=ids) == 3)

    bad = ["Call 0555 123 45 67 for the wheelchair", "Write to someone@example.com about it",
           "See www.example.org for details", "See https://example.org for details",
           f"Contact {CONTACT} for the wheelchair", "too short", "x" * 301]
    codes = [V.post(f"/verify/{a}/approve", data={"public_text": t, "csrf": tv}).status_code for t in bad]
    check("unsafe or invalid public text refused (all 400): " + str(codes), all(c == 400 for c in codes))
    check("refused approvals left the need pending", one("SELECT status FROM needs WHERE id = :i", i=a) == "pending")
    check("a refused approval still shows the verifier's text back",
          "someone@example.com" in V.post(f"/verify/{a}/approve", data={"public_text": bad[1], "csrf": tv}).text)

    good = "Health need in Uskudar, Istanbul. A wheelchair is needed."
    r = V.post(f"/verify/{a}/approve", data={"public_text": good, "csrf": tv}, follow_redirects=False)
    check("clean approval: 303 to the queue", r.status_code == 303 and r.headers["location"].startswith("/verify"))
    row = q("SELECT status, public_text, reviewed_by IS NOT NULL, reviewed_at IS NOT NULL FROM needs WHERE id = :i", i=a)[0]
    check("need published with the verifier's text and review fields", row == ("published", good, True, True))
    check("reviewer is the verifier",
          one("SELECT username FROM users WHERE id = (SELECT reviewed_by FROM needs WHERE id = :i)", i=a) == "vt_ver")
    check("an approved event was written",
          one("SELECT count(*) FROM need_events WHERE need_id = :i AND event = 'approved'", i=a) == 1)

    r = V.post(f"/verify/{a}/approve", data={"public_text": good, "csrf": tv})
    check("second approval refused (409), no duplicate event",
          r.status_code == 409 and one("SELECT count(*) FROM need_events WHERE need_id = :i AND event = 'approved'", i=a) == 1)
    r = V.post(f"/verify/{a}/reject", data={"note": "changed my mind", "csrf": tv})
    check("rejecting a published need refused (409)", r.status_code == 409 and
          one("SELECT status FROM needs WHERE id = :i", i=a) == "published")

    r = V.post(f"/verify/{b}/reject", data={"note": "  ", "csrf": tv})
    check("reject without a note refused (400)", r.status_code == 400 and one("SELECT status FROM needs WHERE id = :i", i=b) == "pending")
    r = V.post(f"/verify/{b}/reject", data={"note": "Please add more detail about the need.", "csrf": tv}, follow_redirects=False)
    check("reject with a note: 303", r.status_code == 303)
    check("need rejected with the note stored",
          q("SELECT status, review_note FROM needs WHERE id = :i", i=b)[0] == ("rejected", "Please add more detail about the need."))
    check("a rejected event was written",
          one("SELECT count(*) FROM need_events WHERE need_id = :i AND event = 'rejected'", i=b) == 1)
    r = V.post(f"/verify/{b}/approve", data={"public_text": good, "csrf": tv})
    check("approving a rejected need refused (409)", r.status_code == 409 and one("SELECT status FROM needs WHERE id = :i", i=b) == "rejected")

    mine = R.get("/needs/mine").text
    check("requester sees the verifier's note on their needs", "Please add more detail about the need." in mine)

    r = V.post(f"/verify/{c3}/approve", data={"public_text": good, "csrf": "forged"})
    check("forged CSRF on approve: 403, need unchanged", r.status_code == 403 and one("SELECT status FROM needs WHERE id = :i", i=c3) == "pending")
    r = V.post(f"/verify/{c3}/reject", data={"note": "a real reason", "csrf": "forged"})
    check("forged CSRF on reject: 403, need unchanged", r.status_code == 403 and one("SELECT status FROM needs WHERE id = :i", i=c3) == "pending")
    check("unknown need id: 404 everywhere",
          [V.get("/verify/999999999").status_code,
           V.post("/verify/999999999/approve", data={"public_text": good, "csrf": tv}).status_code,
           V.post("/verify/999999999/reject", data={"note": "a real reason", "csrf": tv}).status_code] == [404, 404, 404])
    check("a verifier cannot post needs (403)", V.get("/needs/new").status_code == 403)
finally:
    with admin.begin() as c:
        cleanup(c)
    print("throwaway accounts, needs and events removed")

assert not fails, f"failed: {fails}"
print("PASS")
