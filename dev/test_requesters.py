import getpass
import re
from urllib.parse import quote

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import app

owner = getpass.getpass("postgres password: ")
admin = create_engine(f"postgresql+psycopg://postgres:{quote(owner, safe='')}@localhost:5432/dp_analytics")
PW = "throwaway-password-1"
SECRET = "wheelchair for my father"


def cleanup(c):
    u = "(SELECT id FROM users WHERE starts_with(username, 'wt_'))"
    n = f"(SELECT id FROM needs WHERE requester_id IN {u})"
    c.execute(text(f"DELETE FROM need_events WHERE need_id IN {n}"))
    c.execute(text(f"DELETE FROM pledges WHERE need_id IN {n}"))
    c.execute(text(f"DELETE FROM needs WHERE requester_id IN {u}"))
    c.execute(text("DELETE FROM users WHERE starts_with(username, 'wt_')"))
    c.execute(text("DELETE FROM login_failures WHERE username = '__register__'"))


def q(sql, **p):
    with admin.connect() as c:
        return c.execute(text(sql), p).scalar_one()


def new_client():
    return TestClient(app, base_url="http://127.0.0.1:8000")


def token(cl, path):
    return re.search(r'name="csrf" value="([^"]+)"', cl.get(path).text).group(1)


def register(cl, username, password=PW, role="requester", csrf=None):
    return cl.post("/register", data={"username": username, "password": password, "role": role,
                                      "csrf": csrf or token(cl, "/register")}, follow_redirects=False)


def need(cl, **over):
    d = {"category": "health", "province": "Istanbul", "district": "Uskudar",
         "description": SECRET, "contact": "via verifier"}
    d.update(over)
    return cl.post("/needs/new", data={**d, "csrf": token(cl, "/needs/new")}, follow_redirects=False)


fails = []


def check(label, ok, extra=""):
    print(("OK   " if ok else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        fails.append(label)


with admin.begin() as c:
    cleanup(c)

try:
    A = new_client()
    r = register(A, "wt_req1")
    check("register requester: 303 to /", r.status_code == 303 and r.headers["location"] == "/")
    check("role stored as requester", q("SELECT role FROM users WHERE username = 'wt_req1'") == "requester")
    check("home page sends a requester to /needs/mine",
          A.get("/", follow_redirects=False).headers.get("location") == "/needs/mine")

    codes = []
    for bad in ("admin", "verifier", "analyst", "field_agent"):
        codes.append(register(new_client(), f"wt_bad_{bad}", role=bad).status_code)
    check("registering as a privileged role refused (all 400): " + str(codes), all(c == 400 for c in codes))
    check("no privileged account was created",
          q("SELECT count(*) FROM users WHERE starts_with(username, 'wt_bad')") == 0)

    codes = [register(new_client(), u).status_code for u in ("ab", "__register__", "has space", "x" * 31)]
    codes.append(register(new_client(), "wt_shortpw", password="short").status_code)
    codes.append(register(new_client(), "wt_req1").status_code)
    check("bad usernames, short password, duplicate refused (all 400): " + str(codes), all(c == 400 for c in codes))
    check("duplicate did not create a second account", q("SELECT count(*) FROM users WHERE username = 'wt_req1'") == 1)

    check("forged CSRF on register: 403", register(new_client(), "wt_forged", csrf="forged").status_code == 403)

    rid = q("SELECT id FROM users WHERE username = 'wt_req1'")
    check("need form loads", A.get("/needs/new").status_code == 200)
    r = need(A)
    check("valid need: 303 to my needs", r.status_code == 303 and r.headers["location"].startswith("/needs/mine"))
    check("need stored as pending for this requester",
          q("SELECT count(*) FROM needs WHERE requester_id = :r AND status = 'pending'", r=rid) == 1)
    check("a submitted event was written",
          q("SELECT count(*) FROM need_events e JOIN needs n ON n.id = e.need_id WHERE n.requester_id = :r AND e.event = 'submitted'", r=rid) == 1)

    codes = [need(A).status_code, need(A).status_code, need(A).status_code]
    check("2 more allowed, 4th pending refused: " + str(codes), codes == [303, 303, 429])
    check("exactly 3 needs stored", q("SELECT count(*) FROM needs WHERE requester_id = :r", r=rid) == 3)

    codes = [need(A, category="weapons").status_code, need(A, description="short").status_code,
             need(A, province="I").status_code]
    check("invalid category, description, province refused (all 400): " + str(codes), all(c == 400 for c in codes))
    check("invalid posts saved nothing", q("SELECT count(*) FROM needs WHERE requester_id = :r", r=rid) == 3)

    B = new_client()
    check("register supporter: 303", register(B, "wt_sup1", role="supporter").status_code == 303)
    tb = token(B, "/register") if False else None
    refused = [B.get("/needs/new").status_code, B.get("/needs/mine").status_code,
               B.post("/needs/new", data={"category": "health", "province": "Istanbul", "district": "Uskudar",
                                          "description": SECRET, "contact": "x y z", "csrf": "forged"}).status_code]
    check("supporter refused on requester pages (all 403): " + str(refused), all(c == 403 for c in refused))
    anon = new_client()
    check("anonymous refused (401): ", anon.get("/needs/mine").status_code == 401 and anon.get("/needs/new").status_code == 401)

    C = new_client()
    check("register second requester: 303", register(C, "wt_req2").status_code == 303)
    page_c = C.get("/needs/mine")
    check("second requester sees none of the first one's needs", page_c.status_code == 200 and SECRET not in page_c.text)
    check("first requester sees their own needs", SECRET in A.get("/needs/mine").text)

    codes = [register(new_client(), f"wt_rate{i}").status_code for i in range(5)]
    check("registration throttled after 5 per hour from one address: " + str(codes),
          codes[:2] == [303, 303] and 429 in codes)
finally:
    with admin.begin() as c:
        cleanup(c)
    print("throwaway accounts and needs removed")

assert not fails, f"failed: {fails}"
print("PASS")
