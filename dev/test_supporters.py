import getpass
import re
import threading
from urllib.parse import quote

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.auth import hash_password
from app.main import app

owner = getpass.getpass("postgres password: ")
admin = create_engine(f"postgresql+psycopg://postgres:{quote(owner, safe='')}@localhost:5432/dp_analytics")
PW = "throwaway-password-1"
USERS = {"sp_a": "supporter", "sp_b": "supporter", "sp_c": "supporter", "sp_d": "supporter",
         "sp_e": "supporter", "sp_req": "requester", "sp_ver": "verifier"}
CONTACT = "0555 111 22 33"
PRIVATE = ["PRIVATE-DESC", "PRIVATE-CONTACT"]


def cleanup(c):
    u = "(SELECT id FROM users WHERE starts_with(username, 'sp_'))"
    n = f"(SELECT id FROM needs WHERE requester_id IN {u})"
    c.execute(text(f"DELETE FROM need_events WHERE need_id IN {n} OR actor_id IN {u}"))
    c.execute(text(f"DELETE FROM pledges WHERE need_id IN {n} OR supporter_id IN {u}"))
    c.execute(text(f"DELETE FROM needs WHERE requester_id IN {u}"))
    c.execute(text("DELETE FROM users WHERE starts_with(username, 'sp_')"))


def q(sql, **p):
    with admin.connect() as c:
        return c.execute(text(sql), p).all()


def one(sql, **p):
    return q(sql, **p)[0][0]


def nstat(n):
    return one("SELECT status FROM needs WHERE id = :i", i=n)


def pledges_on(n):
    return one("SELECT count(*) FROM pledges WHERE need_id = :i", i=n)


def active_for(user):
    return one("SELECT count(*) FROM pledges p JOIN needs n ON n.id = p.need_id WHERE p.supporter_id = "
               "(SELECT id FROM users WHERE username = :u) AND p.status IN ('pledged', 'handed_over') AND n.status <> 'fulfilled'", u=user)


def pid(n, user):
    return one("SELECT id FROM pledges WHERE need_id = :n AND supporter_id = (SELECT id FROM users WHERE username = :u) "
               "ORDER BY id DESC LIMIT 1", n=n, u=user)


def tok(cl, path="/support"):
    return re.search(r'name="csrf" value="([^"]+)"', cl.get(path).text).group(1)


def login(user):
    cl = TestClient(app, base_url="http://127.0.0.1:8000")
    t = re.search(r'name="csrf" value="([^"]+)"', cl.get("/login").text).group(1)
    code = cl.post("/login", data={"username": user, "password": PW, "csrf": t}, follow_redirects=False).status_code
    assert code == 303, f"login {user}: {code}"
    return cl


def pledge(cl, n, method="phone", contact=CONTACT, csrf=None):
    return cl.post(f"/support/pledge/{n}", data={"contact_method": method, "contact": contact, "csrf": csrf or tok(cl)},
                   follow_redirects=False)


def cancel(cl, p, csrf=None):
    return cl.post(f"/support/cancel/{p}", data={"csrf": csrf or tok(cl)}, follow_redirects=False)


def parallel(calls):
    out, barrier = [None] * len(calls), threading.Barrier(len(calls))

    def run(i, f):
        barrier.wait()
        out[i] = f()
    ts = [threading.Thread(target=run, args=(i, f)) for i, f in enumerate(calls)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


fails = []


def check(label, ok, extra=""):
    print(("OK   " if ok else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        fails.append(label)


with admin.begin() as c:
    cleanup(c)
    for u, role in USERS.items():
        c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r)"), {"u": u, "h": hash_password(PW), "r": role})
    rq = c.execute(text("SELECT id FROM users WHERE username = 'sp_req'")).scalar_one()

    def add(i, published=True):
        return c.execute(text(
            "INSERT INTO needs (requester_id, category, province, district, description, contact, contact_method, public_text, status) "
            "VALUES (:u, 'household', 'Istanbul', 'Uskudar', :d, :k, 'phone', :pt, :s) RETURNING id"),
            {"u": rq, "d": f"PRIVATE-DESC-{i} family details", "k": f"PRIVATE-CONTACT-{i}",
             "pt": f"Pledge test need {i}: a household item is needed." if published else None,
             "s": "published" if published else "pending"}).scalar_one()
    N = [add(i) for i in range(11)]
    PEND, REJ = add(100, False), add(101, False)
    c.execute(text("UPDATE needs SET status = 'rejected' WHERE id = :i"), {"i": REJ})

try:
    A, B = login("sp_a"), login("sp_b")
    RQ, VR = login("sp_req"), login("sp_ver")
    anon = TestClient(app, base_url="http://127.0.0.1:8000")

    check("a supporter lands on /support after sign-in", A.get("/", follow_redirects=False).headers.get("location") == "/support")
    page = A.get("/support")
    check("support page: 200 and shows published need text", page.status_code == 200 and "Pledge test need 1:" in page.text)
    check("support page shows no private description or contact", not any(s in page.text for s in PRIVATE))

    rqt, vrt = tok(RQ, "/needs/new"), tok(VR, "/verify")
    codes = [RQ.get("/support").status_code, VR.get("/support").status_code, RQ.get(f"/support/pledge/{N[0]}").status_code,
             pledge(RQ, N[0], csrf=rqt).status_code, pledge(VR, N[0], csrf=vrt).status_code, cancel(RQ, 1, csrf=rqt).status_code]
    check("requester and verifier refused on support pages and actions (all 403): " + str(codes),
          all(c == 403 for c in codes) and nstat(N[0]) == "published" and pledges_on(N[0]) == 0)
    check("anonymous refused (401)", anon.get("/support").status_code == 401 and anon.get(f"/support/pledge/{N[0]}").status_code == 401)

    form = A.get(f"/support/pledge/{N[0]}")
    check("pledge form shows the public text and no private data", form.status_code == 200 and "Pledge test need 0:" in form.text
          and not any(s in form.text for s in PRIVATE))
    codes = [A.get(f"/support/pledge/{PEND}").status_code, A.get(f"/support/pledge/{REJ}").status_code, A.get("/support/pledge/999999999").status_code,
             pledge(A, PEND).status_code, pledge(A, REJ).status_code, pledge(A, 999999999).status_code]
    check("pending, rejected and unknown needs cannot be pledged (all 404): " + str(codes), all(c == 404 for c in codes))

    bad = [("phone", "abc"), ("phone", "12345"), ("email", "not-an-email"), ("whatsapp", "call me"), ("pigeon", CONTACT), ("other", "ab")]
    codes = [pledge(A, N[0], m, c).status_code for m, c in bad]
    check("bad contact details refused (all 400), need left published, nothing saved: " + str(codes),
          all(c == 400 for c in codes) and nstat(N[0]) == "published" and pledges_on(N[0]) == 0)

    r = pledge(A, N[0])
    check("valid pledge: 303 to /support?done=pledged", r.status_code == 303 and r.headers["location"] == "/support?done=pledged")
    row = q("SELECT p.status, p.contact_method, p.contact, u.username FROM pledges p JOIN users u ON u.id = p.supporter_id WHERE p.need_id = :n", n=N[0])
    check("need pledged, pledge row stored with the supporter's contact, event written",
          nstat(N[0]) == "pledged" and [tuple(x) for x in row] == [("pledged", "phone", CONTACT, "sp_a")]
          and one("SELECT count(*) FROM need_events WHERE need_id = :n AND event = 'pledged'", n=N[0]) == 1, str(row))

    board = anon.get("/board").text
    check("a pledged need leaves the public board and other supporters' list",
          "Pledge test need 0:" not in board and "Pledge test need 0:" not in B.get("/support").text and "Pledge test need 1:" in board)
    check("the supporter's contact appears on no page", all(CONTACT not in t for t in (board, A.get("/support").text, B.get("/support").text)))
    check("a second supporter cannot pledge a pledged need (404), one pledge row", pledge(B, N[0]).status_code == 404 and pledges_on(N[0]) == 1)
    check("forged CSRF on pledge: 403, need unchanged", pledge(A, N[1], csrf="forged").status_code == 403 and nstat(N[1]) == "published")

    codes = [pledge(A, N[1]).status_code, pledge(A, N[2]).status_code]
    r4 = pledge(A, N[3])
    check("cap: two more pledges allowed, the fourth refused (429), its need untouched: " + str(codes + [r4.status_code]),
          codes == [303, 303] and r4.status_code == 429 and nstat(N[3]) == "published" and pledges_on(N[3]) == 0 and active_for("sp_a") == 3)

    p0 = pid(N[0], "sp_a")
    check("another supporter cannot cancel someone else's pledge (404), unchanged",
          cancel(B, p0).status_code == 404 and one("SELECT status FROM pledges WHERE id = :p", p=p0) == "pledged" and nstat(N[0]) == "pledged")
    r = cancel(A, p0)
    check("own cancel: 303, pledge cancelled, need back on the board, event written",
          r.status_code == 303 and r.headers["location"] == "/support?done=cancelled" and one("SELECT status FROM pledges WHERE id = :p", p=p0) == "cancelled"
          and nstat(N[0]) == "published" and one("SELECT count(*) FROM need_events WHERE need_id = :n AND event = 'pledge_cancelled'", n=N[0]) == 1)
    p1 = pid(N[1], "sp_a")
    check("cancelling twice (404) and forged CSRF on cancel (403) change nothing",
          cancel(A, p0).status_code == 404 and cancel(A, p1, csrf="forged").status_code == 403
          and one("SELECT status FROM pledges WHERE id = :p", p=p1) == "pledged")
    check("the freed need can be pledged by another supporter (303)", pledge(B, N[0]).status_code == 303 and nstat(N[0]) == "pledged")

    with admin.begin() as c:
        c.execute(text("UPDATE pledges SET status = 'handed_over' WHERE id = :p"), {"p": p1})
    check("a pledge with the handover arranged cannot be cancelled by the supporter (404), unchanged",
          cancel(A, p1).status_code == 404 and one("SELECT status FROM pledges WHERE id = :p", p=p1) == "handed_over" and nstat(N[1]) == "pledged")

    check("after a cancel the cap has room again: a new pledge is accepted (303)", pledge(A, N[3]).status_code == 303 and active_for("sp_a") == 3)
    check("at the cap a new pledge is refused (429)", pledge(A, N[5]).status_code == 429 and nstat(N[5]) == "published")
    p2 = pid(N[2], "sp_a")
    with admin.begin() as c:
        c.execute(text("UPDATE pledges SET status = 'handed_over' WHERE id = :p"), {"p": p2})
        c.execute(text("UPDATE needs SET status = 'fulfilled' WHERE id = :i"), {"i": N[2]})
    check("a fulfilled need no longer counts toward the cap: a new pledge is accepted (303)",
          active_for("sp_a") == 2 and pledge(A, N[5]).status_code == 303 and active_for("sp_a") == 3)
    pg = A.get("/support").text
    check("status labels show handover arranged, fulfilled and waiting", "Handover arranged" in pg and "Fulfilled" in pg and "Waiting for the verifier" in pg)

    C, D = login("sp_c"), login("sp_d")
    ct, dt = tok(C), tok(D)
    res = parallel([lambda: pledge(C, N[4], csrf=ct).status_code, lambda: pledge(D, N[4], csrf=dt).status_code])
    check("two supporters pledging the same need at once: exactly one wins: " + str(res),
          sorted(res)[0] == 303 and sorted(res)[1] in (404, 409) and pledges_on(N[4]) == 1 and nstat(N[4]) == "pledged")

    Es = [login("sp_e") for _ in range(5)]
    toks = [tok(e) for e in Es]
    res = parallel([(lambda e=e, n=n, t=t: pledge(e, n, csrf=t).status_code) for e, n, t in zip(Es, N[6:11], toks)])
    check("five simultaneous pledges by one supporter: exactly three accepted, two refused (429): " + str(sorted(res)),
          sorted(res) == [303, 303, 303, 429, 429] and active_for("sp_e") == 3)
finally:
    with admin.begin() as c:
        cleanup(c)
    print("throwaway accounts, needs, pledges and events removed")

assert not fails, f"failed: {fails}"
print("PASS")
