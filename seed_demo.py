"""Demo accounts and demo needs for presentations. ALL PEOPLE AND AMOUNTS ARE INVENTED.

Creates accounts whose names start with demo_ (one verifier, 20 requesters, 25 supporters, all with
the password you type), then builds needs by going through the normal database rules as the app role:
post, publish, pledge, send, confirm. So it also exercises the triggers. It makes:
  - 54 finished (fulfilled) lira needs across three goal bands, so the suggested-amount button has
    statistics (the top band gets only 8, to show the "not enough finished needs yet" message),
  - 6 open needs with pledges in different states (one in euros, one after an earthquake),
  - 2 pending needs for the verifier's queue,
  - 1 withdrawn need that had confirmed money, for the verifier's "look again" list.
Run once from the project root: python seed_demo.py. It refuses to run if demo_ accounts exist.
"""
import getpass
import random

from sqlalchemy import text

from app.auth import hash_password
from app.db import engine
from app.districts import DISTRICTS_BY_PROVINCE

rng = random.Random(2026)
PROVS = [p for p in ["Istanbul", "Ankara", "Izmir", "Hatay", "Gaziantep", "Bursa", "Konya", "Adana"] if p in DISTRICTS_BY_PROVINCE]
NEED = ("INSERT INTO needs (requester_id, purpose, medical_location, event, currency, province, district, household_size, goal, "
        "description, contact_method, contact, payee_name, payee_details) VALUES (:r, :pu, :loc, :ev, :cur, :pv, :di, :hh, :g, "
        ":de, 'phone', '+90 5XX XXX XX XX', :pn, :pd) RETURNING id")
TEXT = {"rent_housing": "Rent and deposit for a family that had to move.", "utilities": "Overdue electricity and gas bills after a loss of income.",
        "food": "Groceries for a household for two months.", "education": "School costs for the autumn term.",
        "debt": "Repayment of a small debt that is growing every month.", "other": "A one-off cost the household cannot cover.",
        "medical": "Treatment costs that are not covered by insurance.", "disaster_recovery": "Repairs after the damage to the family's home."}

pw1 = getpass.getpass("password for all demo accounts (12+ characters): ")
pw2 = getpass.getpass("repeat it: ")
if pw1 != pw2 or len(pw1) < 12:
    raise SystemExit("passwords differ or are too short; nothing was saved")
pwhash = hash_password(pw1)

with engine.connect() as c:
    if c.execute(text("SELECT count(*) FROM users WHERE left(username, 5) = 'demo_'")).scalar_one():
        raise SystemExit("demo_ accounts already exist; nothing was saved")
    floor = c.execute(text("SELECT pledge_floor FROM settings")).scalar_one()


def account(c, name, role):
    return c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r) RETURNING id"),
                     {"u": name, "h": pwhash, "r": role}).scalar_one()


def post(c, req, purpose, goal, n, loc=None, ev=None, cur="TRY"):
    prov = rng.choice(PROVS)
    return c.execute(text(NEED), {"r": req, "pu": purpose, "loc": loc, "ev": ev, "cur": cur, "pv": prov,
                                  "di": rng.choice(DISTRICTS_BY_PROVINCE[prov]), "hh": rng.randint(2, 7), "g": goal,
                                  "de": "Invented demo need number %d with enough text." % n,
                                  "pn": "Demo Payee %d" % n, "pd": "TR00 0000 0000 0000 0000 0000 %02d" % (n % 100)}).scalar_one()


def publish(c, nid, ver, purpose):
    c.execute(text("UPDATE needs SET status = 'published', public_text = :t, payee_verified = true, reviewed_by = :v, "
                   "reviewed_at = now() WHERE id = :i"), {"t": TEXT[purpose], "v": ver, "i": nid})


def pledge(c, nid, sup, amount, final):
    pid = c.execute(text("INSERT INTO pledges (need_id, supporter_id, amount_try, ack) VALUES (:n, :s, :a, true) RETURNING id"),
                    {"n": nid, "s": sup, "a": amount}).scalar_one()
    for st in {"pledged": [], "sent": ["sent"], "confirmed": ["sent", "confirmed"]}[final]:
        c.execute(text("UPDATE pledges SET status = :s WHERE id = :i"), {"s": st, "i": pid})


def split(total, d):
    while True:
        w = [rng.uniform(0.5, 1.5) for _ in range(d)]
        amts = [int(total * x / sum(w)) for x in w]
        amts[-1] += total - sum(amts)
        if min(amts) >= floor:
            return amts


with engine.begin() as c:
    ver = account(c, "demo_verifier", "verifier")
    reqs = [account(c, "demo_req%02d" % i, "requester") for i in range(1, 21)]
    sups = [account(c, "demo_sup%02d" % i, "supporter") for i in range(1, 26)]

    n = 0
    # finished needs: (label, how many, goal range, donors range, purposes)
    for how_many, lo, hi, dlo, dhi in [(22, 1000, 4900, 5, 10), (24, 5000, 24000, 8, 20), (8, 25000, 90000, 12, 24)]:
        for i in range(how_many):
            n += 1
            purpose = rng.choice(["rent_housing", "utilities", "food", "education", "debt", "other"])
            goal = int(rng.uniform(lo, hi) // 100 * 100)
            nid = post(c, reqs[n % len(reqs)], purpose, goal, n)
            publish(c, nid, ver, purpose)
            for amt, sup in zip(split(goal, rng.randint(dlo, dhi)), rng.sample(sups, 25)):
                pledge(c, nid, sup, amt, "confirmed")

    # open needs with pledges in different states: (purpose, goal, confirmed, sent, pledged, donor counts, extras)
    OPEN = [("rent_housing", 48000, 14200, 5000, 3000, (10, 3, 2), {}),
            ("medical", 620000, 120000, 60000, 30000, (14, 5, 3), {"loc": "turkey"}),
            ("medical", 85000, 800000, 200000, 100000, (16, 3, 2), {"loc": "abroad", "cur": "EUR"}),
            ("education", 9500, 3900, 0, 1200, (8, 0, 2), {}),
            ("utilities", 6200, 0, 0, 1300, (0, 0, 4), {}),
            ("disaster_recovery", 180000, 40000, 8000, 4000, (15, 3, 2), {"ev": "earthquake"})]
    for purpose, goal, conf, sent, pend, counts, extra in OPEN:
        n += 1
        nid = post(c, reqs[n % len(reqs)], purpose, goal, n, **extra)
        publish(c, nid, ver, purpose)
        pool = rng.sample(sups, sum(counts))
        k = 0
        for total, cnt, st in zip((conf, sent, pend), counts, ("confirmed", "sent", "pledged")):
            if cnt:
                for amt in split(total, cnt):
                    pledge(c, nid, pool[k], amt, st)
                    k += 1

    # pending needs for the verifier's queue
    for purpose, goal, extra in [("rent_housing", 36000, {}), ("medical", 150000, {"loc": "turkey"})]:
        n += 1
        post(c, reqs[n % len(reqs)], purpose, goal, n, **extra)

    # a need withdrawn after money was confirmed
    n += 1
    nid = post(c, reqs[n % len(reqs)], "debt", 40000, n)
    publish(c, nid, ver, "debt")
    pool = rng.sample(sups, 8)
    for (total, cnt, st), off in zip(((6000, 4, "confirmed"), (3000, 2, "sent"), (2000, 2, "pledged")), (0, 4, 6)):
        for j, amt in enumerate(split(total, cnt)):
            pledge(c, nid, pool[off + j], amt, st)
    c.execute(text("UPDATE needs SET status = 'withdrawn', withdrawn_reason = :r WHERE id = :i"),
              {"r": "The requester settled the debt with a family loan and no longer needs the money.", "i": nid})

with engine.connect() as c:
    q = lambda sql: [tuple(r) for r in c.execute(text(sql)).all()]
    print("demo accounts:", q("SELECT role, count(*) FROM users WHERE left(username, 5) = 'demo_' GROUP BY 1 ORDER BY 1"))
    print("needs by status:", q("SELECT status, count(*) FROM needs WHERE requester_id IN (SELECT id FROM users WHERE left(username, 5) = 'demo_') GROUP BY 1 ORDER BY 1"))
    print("pledges by status:", q("SELECT status, count(*) FROM pledges WHERE supporter_id IN (SELECT id FROM users WHERE left(username, 5) = 'demo_') GROUP BY 1 ORDER BY 1"))
    print("suggestion statistics (band, finished needs, average donors):", q("SELECT band, finished_needs, avg_donors FROM suggestion_stats ORDER BY band"))
