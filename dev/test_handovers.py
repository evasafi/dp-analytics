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
USERS = {"hv_ver": "verifier", "hv_ver2": "verifier", "hv_req": "requester", "hv_supa": "supporter",
         "hv_supb": "supporter", "hv_ana": "analyst", "hv_adm": "admin"}
SUP_A_CONTACT = "0555 111 22 33"
SUP_B_CONTACT = "supporter.b@example.com"


def cleanup(c):
    u = "(SELECT id FROM users WHERE starts_with(username, 'hv_'))"
    n = f"(SELECT id FROM needs WHERE requester_id IN {u})"
    c.execute(text(f"DELETE FROM need_events WHERE need_id IN {n} OR actor_id IN {u}"))
    c.execute(text(f"DELETE FROM pledges WHERE need_id IN {n} OR supporter_id IN {u}"))
    c.execute(text(f"DELETE FROM needs WHERE requester_id IN {u}"))
    c.execute(text("DELETE FROM users WHERE starts_with(username, 'hv_')"))


def q(sql, **p):
    with admin.connect() as c:
        return c.execute(text(sql), p).all()


def one(sql, **p):
    return q(sql, **p)[0][0]


def nstat(n):
    return one("SELECT status FROM needs WHERE id = :i", i=n)


def pstat(p):
    return one("SELECT status FROM pledges WHERE id = :i", i=p)


def events(n, ev):
    return one("SELECT count(*) FROM need_events WHERE need_id = :n AND event = :e", n=n, e=ev)


def active_for(user):
    return one("SELECT count(*) FROM pledges p JOIN needs n ON n.id = p.need_id WHERE p.supporter_id = "
               "(SELECT id FROM users WHERE username = :u) AND p.status IN ('pledged', 'handed_over') AND n.status <> 'fulfilled'", u=user)


def pid(n, user):
    return one("SELECT id FROM pledges WHERE need_id = :n AND supporter_id = (SELECT id FROM users WHERE username = :u) "
               "ORDER BY id DESC LIMIT 1", n=n, u=user)


def tok(cl, path):
    return re.search(r'name="csrf" value="([^"]+)"', cl.get(path).text).group(1)


def login(user):
    cl = TestClient(app, base_url="http://127.0.0.1:8000")
    t = re.search(r'name="csrf" value="([^"]+)"', cl.get("/login").text).group(1)
    code = cl.post("/login", data={"username": user, "password": PW, "csrf": t}, follow_redirects=False).status_code
    assert code == 303, f"login {user}: {code}"
    return cl


def pledge(cl, n, method="phone", contact=SUP_A_CONTACT):
    return cl.post(f"/support/pledge/{n}", data={"contact_method": method, "contact": contact, "csrf": tok(cl, "/support")},
                   follow_redirects=False)


def act(cl, kind, p, csrf=None, note=None):
    data = {"csrf": csrf or tok(cl, "/handovers")}
    if note is not None:
        data["note"] = note
    return cl.post(f"/handovers/{p}/{kind}", data=data, follow_redirects=False)


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
    rq = c.execute(text("SELECT id FROM users WHERE username = 'hv_req'")).scalar_one()
    NN = [c.execute(text(
        "INSERT INTO needs (requester_id, category, province, district, description, contact, contact_method, public_text, status) "
        "VALUES (:u, 'household', 'Istanbul', 'Uskudar', :d, :k, 'phone', :pt, 'published') RETURNING id"),
        {"u": rq, "d": f"PRIVATE-DESC-{i} family details", "k": f"PRIVATE-CONTACT-{i}",
         "pt": f"Handover test need {i}: a household item is needed."}).scalar_one() for i in range(6)]

try:
    SA, SB = login("hv_supa"), login("hv_supb")
    V1, V2 = login("hv_ver"), login("hv_ver2")
    R, AN, AD = login("hv_req"), login("hv_ana"), login("hv_adm")
    anon = TestClient(app, base_url="http://127.0.0.1:8000")

    ok = [pledge(SA, NN[i]).status_code for i in range(3)] + [pledge(SB, NN[3], "email", SUP_B_CONTACT).status_code]
    check("four pledges made through the supporter route: " + str(ok), ok == [303, 303, 303, 303])
    P = [pid(NN[0], "hv_supa"), pid(NN[1], "hv_supa"), pid(NN[2], "hv_supa"), pid(NN[3], "hv_supb")]

    check("home page still sends a verifier to /verify, and the queue links to /handovers",
          V1.get("/", follow_redirects=False).headers.get("location") == "/verify" and "/handovers" in V1.get("/verify").text)

    codes = []
    for cl, path in ((SA, "/support"), (R, "/needs/mine"), (AN, "/analyst"), (AD, "/admin")):
        t = tok(cl, path)
        codes += [cl.get("/handovers").status_code,
                  cl.post(f"/handovers/{P[3]}/arrange", data={"csrf": t}).status_code,
                  cl.post(f"/handovers/{P[3]}/fulfil", data={"csrf": t}).status_code,
                  cl.post(f"/handovers/{P[3]}/release", data={"csrf": t, "note": "some reason"}).status_code]
    check("supporter, requester, analyst and admin refused on every handover route (all 403): " + str(codes), all(c == 403 for c in codes))
    check("anonymous refused (401)", anon.get("/handovers").status_code == 401
          and anon.post(f"/handovers/{P[3]}/arrange", data={"csrf": "x"}).status_code == 401)
    check("refused requests changed nothing", all(pstat(p) == "pledged" for p in P) and all(nstat(NN[i]) == "pledged" for i in range(4)))

    hp = V1.get("/handovers")
    need = ["Handover test need 0:", "Handover test need 1:", "Handover test need 2:", "Handover test need 3:"]
    check("verifier sees all four pledges with the public text", hp.status_code == 200 and all(s in hp.text for s in need))
    check("verifier sees the private description, both contacts and the supporter",
          all(s in hp.text for s in ("PRIVATE-DESC-0", "PRIVATE-CONTACT-0", SUP_A_CONTACT, "hv_supa", "hv_supb", "email: " + SUP_B_CONTACT)))
    pubs = [SA.get("/support").text, SB.get("/support").text, anon.get("/board").text]
    check("no supporter page and no public page shows any private detail or either contact",
          not any(s in t for t in pubs for s in ("PRIVATE-DESC", "PRIVATE-CONTACT", SUP_B_CONTACT)) and not any(SUP_A_CONTACT in t for t in pubs))

    r = act(V1, "arrange", P[0])
    check("arrange: 303 to /handovers?done=arranged", r.status_code == 303 and r.headers["location"] == "/handovers?done=arranged")
    check("pledge handed over, need still pledged, one event written",
          pstat(P[0]) == "handed_over" and nstat(NN[0]) == "pledged" and events(NN[0], "handover_arranged") == 1)
    check("page now offers 'Mark as fulfilled', and the supporter sees 'Handover arranged'",
          "Mark as fulfilled" in V1.get("/handovers").text and "Handover arranged" in SA.get("/support").text)
    r = act(V1, "arrange", P[0])
    check("arranging twice: 409, no duplicate event", r.status_code == 409 and events(NN[0], "handover_arranged") == 1)
    r = act(V1, "fulfil", P[1])
    check("fulfilling before the handover is arranged: 409, need unchanged", r.status_code == 409 and nstat(NN[1]) == "pledged" and events(NN[1], "fulfilled") == 0)

    r = act(V1, "fulfil", P[0])
    check("fulfil: 303, need fulfilled, pledge stays handed over, one event",
          r.status_code == 303 and r.headers["location"] == "/handovers?done=fulfilled" and nstat(NN[0]) == "fulfilled"
          and pstat(P[0]) == "handed_over" and events(NN[0], "fulfilled") == 1)
    check("a fulfilled need leaves the verifier's list, shows as Fulfilled to the supporter and frees a slot",
          "Handover test need 0:" not in V1.get("/handovers").text and "Fulfilled" in SA.get("/support").text and active_for("hv_supa") == 2)
    a, b = act(V1, "fulfil", P[0]), act(V1, "release", P[0], note="trying to undo a fulfilled need")
    check("fulfilling again and releasing a fulfilled need are refused (409), nothing changes: " + str([a.status_code, b.status_code]),
          a.status_code == 409 and b.status_code == 409 and nstat(NN[0]) == "fulfilled" and pstat(P[0]) == "handed_over"
          and events(NN[0], "pledge_released") == 0)

    bad = [act(V1, "release", P[1], note="  ").status_code, act(V1, "release", P[1], note="ab").status_code]
    check("release without a real reason refused (400), nothing changed: " + str(bad),
          bad == [400, 400] and pstat(P[1]) == "pledged" and nstat(NN[1]) == "pledged")
    NOTE = "The requester withdrew the request."
    r = act(V1, "release", P[1], note=NOTE)
    check("release: 303, pledge cancelled, need published, event stored with the note",
          r.status_code == 303 and r.headers["location"] == "/handovers?done=released" and pstat(P[1]) == "cancelled"
          and nstat(NN[1]) == "published" and q("SELECT note FROM need_events WHERE need_id = :n AND event = 'pledge_released'", n=NN[1])[0][0] == NOTE)
    check("the released need is back on the public board, and its supporter sees Cancelled",
          "Handover test need 1:" in anon.get("/board").text and "Cancelled" in SA.get("/support").text)
    check("a different supporter can now pledge the released need (303)", pledge(SB, NN[1], "email", SUP_B_CONTACT).status_code == 303)

    act(V1, "arrange", P[2])
    r = act(V1, "release", P[2], note="The supporter can no longer help.")
    check("a pledge with the handover arranged can also be released (303 to published)",
          r.status_code == 303 and pstat(P[2]) == "cancelled" and nstat(NN[2]) == "published")

    v1t = tok(V1, "/handovers")
    codes = [act(V1, "arrange", P[3], csrf="forged").status_code, act(V1, "fulfil", P[3], csrf="forged").status_code,
             act(V1, "release", P[3], csrf="forged", note="a real reason").status_code]
    check("forged CSRF on all three actions: 403, pledge unchanged: " + str(codes), all(c == 403 for c in codes) and pstat(P[3]) == "pledged")
    codes = [act(V1, "arrange", 999999999).status_code, act(V1, "fulfil", 999999999).status_code,
             act(V1, "release", 999999999, note="a real reason").status_code]
    check("unknown pledge: 404 on all three actions: " + str(codes), all(c == 404 for c in codes))
    check("a second verifier can act on any pledge (303)", act(V2, "arrange", P[3]).status_code == 303 and pstat(P[3]) == "handed_over")

    pB = pid(NN[1], "hv_supb")
    t1, t2 = tok(V1, "/handovers"), tok(V2, "/handovers")
    res = parallel([lambda: act(V1, "arrange", pB, csrf=t1).status_code, lambda: act(V2, "arrange", pB, csrf=t2).status_code])
    check("two verifiers arranging the same pledge at once: exactly one wins: " + str(res),
          sorted(res) == [303, 409] and pstat(pB) == "handed_over" and events(NN[1], "handover_arranged") == 1)

    assert pledge(SA, NN[4]).status_code == 303
    p4 = pid(NN[4], "hv_supa")
    vt, st = tok(V1, "/handovers"), tok(SA, "/support")
    res = parallel([lambda: act(V1, "release", p4, csrf=vt, note="Released while the supporter cancels.").status_code,
                    lambda: SA.post(f"/support/cancel/{p4}", data={"csrf": st}, follow_redirects=False).status_code])
    total = events(NN[4], "pledge_released") + events(NN[4], "pledge_cancelled")
    check("a verifier's release and the supporter's cancel at once: exactly one wins: " + str(res),
          sorted(res) in ([303, 409], [303, 404]) and pstat(p4) == "cancelled" and nstat(NN[4]) == "published" and total == 1)
finally:
    with admin.begin() as c:
        cleanup(c)
    print("throwaway accounts, needs, pledges and events removed")

assert not fails, f"failed: {fails}"
print("PASS")
