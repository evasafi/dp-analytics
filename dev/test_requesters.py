"""Requester flow: sign-up limits, posting a need, my needs, withdrawal with a reason.

Run from the project root as a module: python -m dev.test_requesters
Needs the database owner's password (hidden prompt). The owner connection is built from the app's own
DP_DATABASE_URL and the test refuses to run against a database without the financial-needs schema.
Everything it creates has the username prefix rq_ and is removed at the end.
"""
import getpass
import re
import threading
from collections import defaultdict

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.auth import hash_password
from app.config import settings
from app.db import engine
from app.districts import DISTRICTS_BY_PROVINCE
from app.main import app

PW = "throwaway-password-1"
admin = create_engine(make_url(settings.database_url).set(username="postgres", password=getpass.getpass("postgres password: ")))
USERS = {"rq_a": "requester", "rq_b": "requester", "rq_conc": "requester", "rq_s": "supporter", "rq_s2": "supporter", "rq_v": "verifier"}
VALID_IBAN = "TR33 0006 1005 1978 6457 8413 26"      # a published sample; the last digit changed fails the check
fails, total = [], 0

with admin.connect() as c:
    if not c.execute(text("SELECT count(*) FROM information_schema.columns "
                          "WHERE table_name = 'needs' AND column_name = 'payee_name'")).scalar_one():
        raise SystemExit("This database does not have the financial-needs schema; refusing to run.")


def check(label, ok):
    global total
    total += 1
    print(("OK   " if ok else "FAIL ") + label)
    if not ok:
        fails.append(label)


def csrf_from(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def one(sql, **p):
    with admin.connect() as c:
        return c.execute(text(sql), p).scalar_one()


def rows(sql, **p):
    with admin.connect() as c:
        return [tuple(r) for r in c.execute(text(sql), p).all()]


def cleanup():
    sub = "(SELECT id FROM users WHERE left(username, 3) = 'rq_')"
    mine = f"(SELECT id FROM needs WHERE requester_id IN {sub})"
    with admin.begin() as c:
        c.execute(text(f"DELETE FROM need_events WHERE actor_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM pledges WHERE supporter_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM needs WHERE requester_id IN {sub}"))
        c.execute(text("DELETE FROM users WHERE left(username, 3) = 'rq_'"))


def client_for(u):
    cl = TestClient(app, base_url="http://127.0.0.1:8000")
    tok = csrf_from(cl.get("/login").text)
    code = cl.post("/login", data={"username": u, "password": PW, "csrf": tok}, follow_redirects=False).status_code
    assert code == 303, f"login {u} failed: {code}"
    return cl, tok


def saved(u):
    return one("SELECT count(*) FROM needs n JOIN users x ON x.id = n.requester_id WHERE x.username = :u", u=u)


def clear_pending(u):
    """Move a requester's pending needs out of the way so the next post is not blocked by the limit."""
    with admin.begin() as c:
        c.execute(text("UPDATE needs SET status = 'rejected', review_note = 'cleared by the test' "
                       "WHERE status = 'pending' AND requester_id = (SELECT id FROM users WHERE username = :u)"), {"u": u})


cleanup()
with admin.begin() as c:
    for u, role in USERS.items():
        c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r)"),
                  {"u": u, "h": hash_password(PW), "r": role})
uid = {u: one("SELECT id FROM users WHERE username = :u", u=u) for u in USERS}

ISTANBUL = DISTRICTS_BY_PROVINCE["Istanbul"][0]
GOOD = {"purpose": "rent_housing", "medical_location": "turkey", "event": "", "province": "Istanbul", "district": ISTANBUL,
        "household_size": "4", "goal": "48000", "description": "The landlord is ending the lease and we need a deposit.",
        "contact_method": "phone", "contact": "+90 532 000 00 00", "payee_name": "Example Payee", "iban": VALID_IBAN}


def post(cl, tok, **over):
    return cl.post("/needs/new", data={**GOOD, **over, "csrf": tok}, follow_redirects=False)


try:
    a, ta = client_for("rq_a")
    b, tb = client_for("rq_b")
    s, ts_ = client_for("rq_s")
    v, tv = client_for("rq_v")
    anon = TestClient(app, base_url="http://127.0.0.1:8000")
    anon_tok = csrf_from(anon.get("/login").text)

    # ---- who may use these pages
    refused = [c.get(p).status_code for c in (s, v) for p in ("/needs/new", "/needs/mine")]
    check("supporters and verifiers are refused on the requester pages (all 403): " + str(refused), all(x == 403 for x in refused))
    check("anonymous visitors get 401 on both pages and on posting",
          anon.get("/needs/new").status_code == 401 and anon.get("/needs/mine").status_code == 401
          and anon.post("/needs/new", data={**GOOD, "csrf": anon_tok}).status_code == 401)
    all_before = one("SELECT count(*) FROM needs")
    check("a supporter and a verifier cannot post a need (403) and nothing is saved",
          s.post("/needs/new", data={**GOOD, "csrf": ts_}).status_code == 403
          and v.post("/needs/new", data={**GOOD, "csrf": tv}).status_code == 403
          and one("SELECT count(*) FROM needs") == all_before)

    # ---- sign-up
    reg = TestClient(app, base_url="http://127.0.0.1:8000")
    rt = csrf_from(reg.get("/register").text)
    privileged = [reg.post("/register", data={"username": "rq_priv_" + r, "password": PW, "role": r, "csrf": rt}).status_code
                  for r in ("verifier", "admin", "analyst")]
    check("registering as verifier, admin or analyst is refused (400) and creates nothing: " + str(privileged),
          all(x == 400 for x in privileged) and one("SELECT count(*) FROM users WHERE left(username, 8) = 'rq_priv_'") == 0)
    ok_signups = [reg.post("/register", data={"username": f"rq_new{i}", "password": PW, "role": "requester", "csrf": rt},
                           follow_redirects=False).status_code for i in range(1, 6)]
    sixth = reg.post("/register", data={"username": "rq_new6", "password": PW, "role": "requester", "csrf": rt},
                     follow_redirects=False).status_code
    check(f"five sign-ups from one address are accepted {ok_signups} and the sixth gets 429 ({sixth})",
          all(x == 303 for x in ok_signups) and sixth == 429 and one("SELECT count(*) FROM users WHERE username = 'rq_new6'") == 0)

    # ---- the form and a valid post
    page = a.get("/needs/new")
    check("the post form lists every purpose, the limits, and warns that payee details are final",
          page.status_code == 200 and "Rent and housing" in page.text and "Disaster recovery" in page.text
          and "limits-data" in page.text and "You cannot change it after posting" in page.text)
    r = post(a, ta)
    row = rows("SELECT purpose, currency, goal, payee_details, status, medical_location, event, household_size "
               "FROM needs WHERE requester_id = :u", u=uid["rq_a"])
    check("a valid post is accepted (303 to my needs) and stored as pending in lira with a normalised IBAN",
          r.status_code == 303 and r.headers["location"] == "/needs/mine?done=1" and len(row) == 1
          and row[0][:5] == ("rent_housing", "TRY", 48000, "TR330006100519786457841326", "pending")
          and row[0][5] is None and row[0][6] is None and row[0][7] == 4)
    check("a 'submitted' event was written",
          one("SELECT count(*) FROM need_events WHERE event = 'submitted' AND actor_id = :u", u=uid["rq_a"]) == 1)
    mine = a.get("/needs/mine")
    check("my needs shows it as waiting for review, with its goal, place and description",
          "Waiting for review" in mine.text and "48,000 TRY" in mine.text and ISTANBUL in mine.text and "landlord" in mine.text)
    check("another requester sees none of it", "landlord" not in b.get("/needs/mine").text and "You have not posted" in b.get("/needs/mine").text)

    # ---- purposes, currencies, events
    clear_pending("rq_a")
    r = post(a, ta, purpose="medical", medical_location="abroad", goal="85000")
    abroad = rows("SELECT currency, medical_location, goal FROM needs WHERE requester_id = :u AND purpose = 'medical'", u=uid["rq_a"])
    check("medical abroad is stored in euros", r.status_code == 303 and abroad == [("EUR", "abroad", 85000)])
    clear_pending("rq_a")
    r = post(a, ta, purpose="medical", medical_location="turkey", goal="620000")
    check("medical in Turkey is stored in lira up to its higher limit",
          r.status_code == 303 and ("TRY", "turkey", 620000) in rows("SELECT currency, medical_location, goal FROM needs WHERE requester_id = :u AND purpose = 'medical'", u=uid["rq_a"]))
    clear_pending("rq_a")
    r = post(a, ta, purpose="disaster_recovery", event="earthquake", goal="180000")
    check("disaster recovery with an event is accepted and the event is stored",
          r.status_code == 303 and one("SELECT event FROM needs WHERE requester_id = :u AND purpose = 'disaster_recovery'", u=uid["rq_a"]) == "earthquake")
    clear_pending("rq_a")
    r = post(a, ta, purpose="food", goal="3000", event="flood", medical_location="abroad")
    food = rows("SELECT currency, event, medical_location FROM needs WHERE requester_id = :u AND purpose = 'food'", u=uid["rq_a"])
    check("an event or a medical location sent with another purpose is ignored, not stored", r.status_code == 303 and food == [("TRY", None, None)])

    # ---- refused forms: 400, nothing saved, the problem is named, what was typed comes back
    def refused_post(label, expect, **over):
        before = saved("rq_a")
        clear_pending("rq_a")
        r = post(a, ta, **over)
        check(f"refused: {label}", r.status_code == 400 and expect in r.text and saved("rq_a") == before)
        return r

    refused_post("no purpose chosen", "Choose what the money is for.", purpose="")
    refused_post("an unknown purpose", "Choose what the money is for.", purpose="gold_bars")
    refused_post("medical without a location", "Say whether the treatment is in Turkey or abroad.", purpose="medical", medical_location="")
    refused_post("disaster recovery without an event", "Choose which event.", purpose="disaster_recovery", event="")
    for label, prov in (("a province in the wrong case", "istanbul"), ("an empty province", ""), ("an unknown province", "Atlantis"),
                        ("a hostile province", "'; DROP TABLE needs;--"), ("markup as a province", "<script>alert(1)</script>")):
        refused_post(label, "Choose a province from the list.", province=prov)
    other = next(p for p in DISTRICTS_BY_PROVINCE if p != "Istanbul" and DISTRICTS_BY_PROVINCE[p][0] not in DISTRICTS_BY_PROVINCE["Istanbul"])
    refused_post("a district from another province", "Choose a district that belongs to the province.", district=DISTRICTS_BY_PROVINCE[other][0])
    for label, hh in (("household size 0", "0"), ("household size 13", "13"), ("household size abc", "abc"), ("an empty household size", "")):
        refused_post(label, "Household size must be a whole number from 1 to 12.", household_size=hh)
    for label, g in (("a goal of 99", "99"), ("a negative goal", "-5"), ("a goal with decimals", "100.5"), ("a goal in exponent form", "1e3"), ("a goal of text", "abc")):
        refused_post(label, "The amount must be a whole number of at least 100.", goal=g)
    refused_post("a rent goal above its limit", "above the limit of 300,000 TRY", goal="300001")
    refused_post("a medical abroad goal above its limit", "above the limit of 2,000,000 EUR", purpose="medical", medical_location="abroad", goal="2000001")
    refused_post("a description that is too short", "The description must be 10 to 1000 characters (it is 5).", description="short")
    refused_post("a malformed phone number", "A phone or WhatsApp number must have 7 to 15 digits", contact="12ab")
    refused_post("a malformed email", "does not look like an email", contact_method="email", contact="not-an-email")
    refused_post("an empty payee name", "Enter the account holder", payee_name="")
    refused_post("an IBAN of the wrong shape", "Enter a Turkish IBAN: TR followed by 24 digits.", iban="TR12345")
    refused_post("an IBAN with a mistyped digit", "check digits of this IBAN do not match", iban="TR33 0006 1005 1978 6457 8413 27")

    clear_pending("rq_a")
    r = post(a, ta, province="Atlantis", description="<script>alert(1)</script> keep this text", payee_name="Typed Name")
    check("a refused post gives back what was typed, with hostile text escaped",
          r.status_code == 400 and "keep this text" in r.text and "Typed Name" in r.text and "&lt;script&gt;alert(1)" in r.text
          and "<script>alert(1)" not in r.text and ISTANBUL in r.text)
    check("a refusal never repeats the account number or contact in an error message",
          "TR330006100519786457841326" not in re.sub(r'value="[^"]*"', "", r.text))

    # ---- places
    clear_pending("rq_a")
    r = post(a, ta, province="  Istanbul  ", district=f"  {ISTANBUL}  ", goal="1500")
    check("a padded province and district are accepted and stored trimmed",
          r.status_code == 303 and one("SELECT province FROM needs WHERE requester_id = :u AND goal = 1500", u=uid["rq_a"]) == "Istanbul")
    owners = defaultdict(list)
    for pv, ds in DISTRICTS_BY_PROVINCE.items():
        for d in ds:
            owners[d].append(pv)
    shared = next(((d, pvs) for d, pvs in owners.items() if len(pvs) >= 2), None)
    if shared:
        d, pvs = shared
        outsider = next(p for p in DISTRICTS_BY_PROVINCE if p not in pvs)
        clear_pending("rq_a")
        good_shared = post(a, ta, province=pvs[0], district=d, goal="1600")
        bad_shared = post(a, ta, province=outsider, district=d, goal="1601")
        check(f"a district name shared by {len(pvs)} provinces ({d}) is accepted in its own and refused in another",
              good_shared.status_code == 303 and bad_shared.status_code == 400)
    else:
        print("skip  no district name is shared by two provinces in this data")

    # ---- the limit of 3 pending needs
    clear_pending("rq_a")
    codes = [post(a, ta, goal=str(1000 + i)).status_code for i in range(4)]
    check("three pending needs are allowed and the fourth gets 429 with a clear message: " + str(codes),
          codes == [303, 303, 303, 429] and "already have 3 needs waiting for review" in post(a, ta, goal="1005").text
          and one("SELECT count(*) FROM needs WHERE requester_id = :u AND status = 'pending'", u=uid["rq_a"]) == 3)
    clear_pending("rq_a")

    # ---- the limit under simultaneous posts
    conc = [client_for("rq_conc") for _ in range(8)]
    barrier, out, lock = threading.Barrier(8), [], threading.Lock()

    def worker(cl, tok):
        barrier.wait(timeout=30)
        r = post(cl, tok, goal="2000")
        with lock:
            out.append(r.status_code)

    ts = [threading.Thread(target=worker, args=cc) for cc in conc]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check(f"8 simultaneous posts by one requester: {out.count(303)} accepted, {out.count(429)} refused, {one('SELECT count(*) FROM needs WHERE requester_id = :u', u=uid['rq_conc'])} saved",
          out.count(303) == 3 and out.count(429) == 5 and one("SELECT count(*) FROM needs WHERE requester_id = :u", u=uid["rq_conc"]) == 3)

    # ---- totals on my needs and withdrawal
    clear_pending("rq_a")
    post(a, ta, goal="48000", description="Rent need used for the funding display.")
    nid = one("SELECT id FROM needs WHERE requester_id = :u AND description LIKE 'Rent need used%'", u=uid["rq_a"])
    with admin.begin() as c:
        c.execute(text("UPDATE needs SET status = 'published', public_text = 'Rent help.', payee_verified = true, reviewed_by = :v WHERE id = :n"),
                  {"v": uid["rq_v"], "n": nid})
        for sup, amt, steps in ((uid["rq_s"], 10000, ("sent", "confirmed")), (uid["rq_s2"], 5000, ("sent",)), (uid["rq_s"], 3000, ())):
            pid = c.execute(text("INSERT INTO pledges (need_id, supporter_id, amount_try, ack) VALUES (:n, :s, :a, true) RETURNING id"),
                            {"n": nid, "s": sup, "a": amt}).scalar_one()
            for st in steps:
                c.execute(text("UPDATE pledges SET status = :s WHERE id = :i"), {"s": st, "i": pid})
    mine = a.get("/needs/mine").text
    check("my needs shows confirmed and promised amounts and the number of donors",
          "On the board" in mine and "Confirmed 10,000 TRY" in mine and "promised 8,000 TRY more" in mine and "2 donors" in mine)
    check("another requester cannot withdraw my need (404) and nothing changes",
          b.post(f"/needs/{nid}/withdraw", data={"reason": "I am not the owner of this.", "csrf": tb}).status_code == 404
          and one("SELECT status FROM needs WHERE id = :n", n=nid) == "published")
    check("withdrawing with a forged CSRF token is refused (403)",
          a.post(f"/needs/{nid}/withdraw", data={"reason": "A reason that is long enough.", "csrf": "forged"}).status_code == 403)
    r = a.post(f"/needs/{nid}/withdraw", data={"reason": "too short", "csrf": ta})
    check("withdrawing without a real reason is refused (400), the reason typed is kept, the need is unchanged",
          r.status_code == 400 and "without one" in r.text and "too short" in r.text and one("SELECT status FROM needs WHERE id = :n", n=nid) == "published")
    check("withdrawing with an empty reason is refused (400)",
          a.post(f"/needs/{nid}/withdraw", data={"reason": "   ", "csrf": ta}).status_code == 400)
    r = a.post(f"/needs/{nid}/withdraw", data={"reason": "The rent was paid by the family.", "csrf": ta}, follow_redirects=False)
    sts = sorted(x[0] for x in rows("SELECT status FROM pledges WHERE need_id = :n", n=nid))
    check("a valid withdrawal works: 303, status withdrawn, reason stored, event written",
          r.status_code == 303 and one("SELECT status FROM needs WHERE id = :n", n=nid) == "withdrawn"
          and one("SELECT withdrawn_reason FROM needs WHERE id = :n", n=nid) == "The rent was paid by the family."
          and one("SELECT count(*) FROM need_events WHERE need_id = :n AND event = 'withdrawn' AND note = 'The rent was paid by the family.'", n=nid) == 1)
    check("the unsent pledge was cancelled and the sent and confirmed ones were left in place: " + str(sts), sts == ["cancelled", "confirmed", "sent"])
    mine = a.get("/needs/mine?withdrawn=1").text
    check("my needs shows it as withdrawn with my reason, the earlier totals, and no withdraw form",
          "Withdrawn. The need is off the board." in mine and "You withdrew this need: The rent was paid by the family." in mine
          and "Confirmed 10,000 TRY" in mine and f'/needs/{nid}/withdraw' not in mine)
    rejected_id = one("SELECT id FROM needs WHERE requester_id = :u AND status = 'rejected' LIMIT 1", u=uid["rq_a"])
    check("withdrawing it again, or a rejected need, is refused (409) and changes nothing",
          a.post(f"/needs/{nid}/withdraw", data={"reason": "Trying a second time now.", "csrf": ta}).status_code == 409
          and a.post(f"/needs/{rejected_id}/withdraw", data={"reason": "Trying on a rejected need.", "csrf": ta}).status_code == 409
          and one("SELECT status FROM needs WHERE id = :n", n=rejected_id) == "rejected")

    clear_pending("rq_a")
    post(a, ta, goal="4200", description="A pending need that I will withdraw.")
    pend = one("SELECT id FROM needs WHERE requester_id = :u AND description LIKE 'A pending need that%'", u=uid["rq_a"])
    r = a.post(f"/needs/{pend}/withdraw", data={"reason": "I found help from my family.", "csrf": ta}, follow_redirects=False)
    check("a pending need can be withdrawn too", r.status_code == 303 and one("SELECT status FROM needs WHERE id = :n", n=pend) == "withdrawn")
    check("a rejected need shows the verifier's note to the requester",
          "Verifier note: cleared by the test" in a.get("/needs/mine").text)
    euro = one("SELECT id FROM needs WHERE requester_id = :u AND currency = 'EUR'", u=uid["rq_a"])
    check("a euro need is shown in euros", "85,000 EUR" in a.get("/needs/mine").text and euro is not None)
finally:
    cleanup()
    left = one("SELECT count(*) FROM users WHERE left(username, 3) = 'rq_'")
    print(f"test rows removed (users left with the rq_ prefix: {left})")

assert not fails, f"failed: {fails}"
print(f"PASS: {total} checks")
