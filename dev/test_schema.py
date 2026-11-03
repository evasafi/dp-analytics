"""Tests for the financial-needs schema (schema.sql): constraints, triggers, grants, views, concurrency.

Run from the project root as a module: python -m dev.test_schema
Needs the database owner's password (hidden prompt). The owner connection is built from the app's own
DP_DATABASE_URL, so it always targets the database the app is configured for. It refuses to run
against a database that does not have the financial-needs schema. Everything it creates uses the
username prefix sc_ (or the dataset name schema_test) and is removed at the end.
"""
import getpass
import threading

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db import engine

url = make_url(settings.database_url)
pw = getpass.getpass("postgres password: ")
admin = create_engine(url.set(username="postgres", password=pw))
fails, total = [], 0


def check(label, ok):
    global total
    total += 1
    print(("OK   " if ok else "FAIL ") + label)
    if not ok:
        fails.append(label)


def run(eng, sql, **p):
    try:
        with eng.begin() as c:
            r = c.execute(text(sql), p)
            rows = [tuple(x) for x in r.fetchall()] if r.returns_rows else None
        return "ok", rows
    except DBAPIError as e:
        return "err", str(e.orig).split("\n")[0]


def refused(eng, sql, why=None, **p):
    s, m = run(eng, sql, **p)
    return s == "err" and (why is None or why in m)


def one(eng, sql, **p):
    return run(eng, sql, **p)[1][0][0]


with admin.connect() as c:
    v2 = c.execute(text("SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'needs' AND column_name = 'payee_name'")).scalar_one()
if not v2:
    raise SystemExit("This database does not have the financial-needs schema; refusing to run.")


def cleanup():
    sub = "(SELECT id FROM users WHERE left(username, 3) = 'sc_')"
    mine = f"(SELECT id FROM needs WHERE requester_id IN {sub})"
    with admin.begin() as c:
        c.execute(text(f"DELETE FROM need_events WHERE actor_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM pledges WHERE supporter_id IN {sub} OR need_id IN {mine}"))
        c.execute(text(f"DELETE FROM needs WHERE requester_id IN {sub}"))
        c.execute(text("DELETE FROM users WHERE left(username, 3) = 'sc_'"))
        c.execute(text("DELETE FROM datasets WHERE name = 'schema_test'"))


cleanup()
real_budgets = run(admin, "SELECT name, epsilon_spent FROM datasets WHERE name IN ('needs_synthetic', 'needs_live') ORDER BY name")[1]
fl = one(admin, "SELECT pledge_floor FROM settings")
mx = one(admin, "SELECT max_unconfirmed_pledges FROM settings")
rate = one(admin, "SELECT eur_try_rate FROM settings")

run(admin, "INSERT INTO users (username, password_hash, role) VALUES "
           "('sc_req', 'x', 'requester'), ('sc_ver', 'x', 'verifier'), ('sc_s1', 'x', 'supporter'), ('sc_s2', 'x', 'supporter')")
uid = {u: one(admin, "SELECT id FROM users WHERE username = :u", u=u) for u in ("sc_req", "sc_ver", "sc_s1", "sc_s2")}

NEED = ("INSERT INTO needs (requester_id, purpose, medical_location, event, currency, province, district, household_size, goal, "
        "description, contact_method, contact, payee_name, payee_details) VALUES (:r, :p, :loc, :ev, :cur, 'Hatay', 'Antakya', 4, :goal, "
        "'Needs help with costs', 'phone', '+90 5XX', 'Example Payee', 'TR000000000000000000000001')")


def post(purpose, loc, ev, cur, goal):
    return run(engine, NEED, r=uid["sc_req"], p=purpose, loc=loc, ev=ev, cur=cur, goal=goal)


def nid_of(purpose, goal):
    return one(admin, "SELECT id FROM needs WHERE requester_id = :r AND purpose = :p AND goal = :g", r=uid["sc_req"], p=purpose, g=goal)


def publish(nid, txt="Public text for this need."):
    run(admin, "UPDATE needs SET status = 'published', public_text = :t, payee_verified = true, reviewed_by = :v WHERE id = :i",
        t=txt, v=uid["sc_ver"], i=nid)


P = "INSERT INTO pledges (need_id, supporter_id, amount_try, ack) VALUES (:n, :s, :a, :k)"


def pledge(n, s, a, k=True, eng=None):
    return run(eng or engine, P, n=n, s=s, a=a, k=k)


try:
    # ---- posting, ceilings, currencies
    check("a valid rent need can be posted by the app role", post("rent_housing", None, None, "TRY", 48000)[0] == "ok")
    check("a goal above the ceiling is refused", refused(engine, NEED, why="ceiling", r=uid["sc_req"], p="food", loc=None, ev=None, cur="TRY", goal=300001))
    check("a goal exactly at the ceiling is accepted", post("food", None, None, "TRY", 300000)[0] == "ok")
    check("medical in Turkey up to 1,000,000 TRY is accepted", post("medical", "turkey", None, "TRY", 1000000)[0] == "ok")
    check("medical abroad in euros is accepted", post("medical", "abroad", None, "EUR", 85000)[0] == "ok")
    check("medical abroad in lira is refused", post("medical", "abroad", None, "TRY", 85000)[0] == "err")
    check("a non-medical need in euros is refused", post("rent_housing", None, None, "EUR", 5000)[0] == "err")
    check("medical without a location is refused", post("medical", None, None, "TRY", 5000)[0] == "err")
    check("disaster recovery needs an event, other purposes must not have one",
          post("disaster_recovery", None, None, "TRY", 5000)[0] == "err"
          and post("rent_housing", None, "flood", "TRY", 5000)[0] == "err"
          and post("disaster_recovery", None, "earthquake", "TRY", 5000)[0] == "ok")
    check("a goal below 100 is refused", post("food", None, None, "TRY", 99)[0] == "err")

    # ---- what the app role cannot do
    check("the app role cannot insert a need as published",
          refused(engine, "INSERT INTO needs (requester_id, purpose, currency, province, district, household_size, goal, description, contact, "
                          "payee_name, payee_details, status, public_text, payee_verified) VALUES (:r, 'food', 'TRY', 'Hatay', 'Antakya', 3, 1000, "
                          "'ten chars++', 'abc', 'Pn', '12345', 'published', 'x', true)", why="permission", r=uid["sc_req"]))
    rent = nid_of("rent_housing", 48000)
    check("the app role cannot change payee details after posting", refused(engine, "UPDATE needs SET payee_details = 'TR999' WHERE id = :i", why="permission", i=rent))
    check("the app role cannot change the goal after posting", refused(engine, "UPDATE needs SET goal = 1 WHERE id = :i", why="permission", i=rent))
    check("the app role cannot delete a need", refused(engine, "DELETE FROM needs WHERE id = :i", why="permission", i=rent))
    check("the app role cannot change settings", refused(engine, "UPDATE settings SET pledge_floor = 1", why="permission"))
    check("the app role cannot update or delete the audit log",
          refused(engine, "DELETE FROM audit_log", why="permission") and refused(engine, "UPDATE audit_log SET query_type = 'x'", why="permission"))

    # ---- budgets (on a throwaway dataset, never the real ones)
    run(admin, "INSERT INTO datasets (name, epsilon_total) VALUES ('schema_test', 1.0)")
    check("the app role can spend budget", run(engine, "UPDATE datasets SET epsilon_spent = 0.5 WHERE name = 'schema_test'")[0] == "ok")
    check("budget is spend-only: a decrease is refused", refused(admin, "UPDATE datasets SET epsilon_spent = 0.1 WHERE name = 'schema_test'", why="spend-only"))
    check("the budget total cannot be raised", refused(admin, "UPDATE datasets SET epsilon_total = 5 WHERE name = 'schema_test'", why="spend-only"))
    check("spending cannot exceed the total", refused(admin, "UPDATE datasets SET epsilon_spent = 2 WHERE name = 'schema_test'"))

    # ---- publishing rules
    check("a need cannot be published without text and a verified payee",
          refused(engine, "UPDATE needs SET status = 'published' WHERE id = :i", why="public_needs_are_complete", i=rent))
    check("publishing works once text and the payee check are set",
          run(engine, "UPDATE needs SET status = 'published', public_text = 'A family needs help with rent.', payee_verified = true, reviewed_by = :v WHERE id = :i",
              v=uid["sc_ver"], i=rent)[0] == "ok")
    check("an illegal jump from pending to fulfilled is refused",
          refused(engine, "UPDATE needs SET status = 'fulfilled' WHERE id = :i", why="invalid need status", i=nid_of("food", 300000)))

    # ---- pledges
    check("a pledge below the floor is refused", refused(engine, P, why="floor", n=rent, s=uid["sc_s1"], a=fl - 1, k=True))
    check("a pledge without the acknowledgement is refused", refused(engine, P, n=rent, s=uid["sc_s1"], a=100, k=False))
    check("a requester cannot pledge to their own need", refused(engine, P, why="own need", n=rent, s=uid["sc_req"], a=100, k=True))
    check("a pledge to an unpublished need is refused",
          refused(engine, P, why="not open", n=nid_of("food", 300000), s=uid["sc_s1"], a=100, k=True))
    check("a pledge of exactly the floor is accepted", pledge(rent, uid["sc_s1"], fl)[0] == "ok")
    check("a pledge above what is still open is refused", refused(engine, P, why="exceed", n=rent, s=uid["sc_s2"], a=48000, k=True))
    st = run(admin, "SELECT status, amount_need, expires_at > now(), rate_used FROM pledges WHERE need_id = :n", n=rent)[1][0]
    check("the trigger set status, the amount in the need's currency and the expiry", st[0] == "pledged" and float(st[1]) == fl and st[2] and st[3] is None)

    eur = nid_of("medical", 85000)
    publish(eur, "Surgery abroad.")
    check("a euro pledge stores the rate and the euro equivalent",
          pledge(eur, uid["sc_s1"], 5000)[0] == "ok"
          and float(one(admin, "SELECT amount_need FROM pledges WHERE need_id = :n", n=eur)) == round(5000 / float(rate), 2)
          and float(one(admin, "SELECT rate_used FROM pledges WHERE need_id = :n", n=eur)) == float(rate))

    # ---- pledge lifecycle and fulfilment
    pid = one(admin, "SELECT id FROM pledges WHERE need_id = :n", n=rent)
    check("a pledge can go to sent and then confirmed, with timestamps",
          run(engine, "UPDATE pledges SET status = 'sent' WHERE id = :i", i=pid)[0] == "ok"
          and run(engine, "UPDATE pledges SET status = 'confirmed' WHERE id = :i", i=pid)[0] == "ok"
          and one(admin, "SELECT sent_at IS NOT NULL AND confirmed_at IS NOT NULL FROM pledges WHERE id = :i", i=pid))
    check("an illegal pledge jump from confirmed to cancelled is refused",
          refused(engine, "UPDATE pledges SET status = 'cancelled' WHERE id = :i", why="invalid pledge", i=pid))
    rest = 48000 - fl - (fl - 1)
    check("a pledge that leaves less than one minimum pledge open is accepted", pledge(rent, uid["sc_s2"], rest)[0] == "ok")
    p2 = one(admin, "SELECT id FROM pledges WHERE need_id = :n AND supporter_id = :s", n=rent, s=uid["sc_s2"])
    run(engine, "UPDATE pledges SET status = 'sent' WHERE id = :i", i=p2)
    run(engine, "UPDATE pledges SET status = 'confirmed' WHERE id = :i", i=p2)
    check("the need becomes fulfilled when less than one minimum pledge remains", one(admin, "SELECT status FROM needs WHERE id = :i", i=rent) == "fulfilled")
    check("a fulfilled need cannot be withdrawn",
          refused(engine, "UPDATE needs SET status = 'withdrawn', withdrawn_reason = 'changed my mind about it' WHERE id = :i", why="invalid need status", i=rent))

    # ---- withdrawal
    wid = nid_of("food", 300000)
    publish(wid, "Food help.")
    pledge(wid, uid["sc_s1"], 200)
    pledge(wid, uid["sc_s2"], 300)
    q = one(admin, "SELECT id FROM pledges WHERE need_id = :n AND supporter_id = :s", n=wid, s=uid["sc_s2"])
    run(engine, "UPDATE pledges SET status = 'sent' WHERE id = :i", i=q)
    check("withdrawal without a reason is refused",
          refused(engine, "UPDATE needs SET status = 'withdrawn' WHERE id = :i", why="withdrawal_has_reason", i=wid))
    check("withdrawal with a too-short reason is refused",
          refused(engine, "UPDATE needs SET status = 'withdrawn', withdrawn_reason = 'no' WHERE id = :i", i=wid))
    check("withdrawal with a reason is accepted",
          run(engine, "UPDATE needs SET status = 'withdrawn', withdrawn_reason = 'I no longer need the money.' WHERE id = :i", i=wid)[0] == "ok")
    sts = dict(run(admin, "SELECT supporter_id, status FROM pledges WHERE need_id = :n", n=wid)[1])
    check("an unsent pledge is cancelled and a sent one is left in place", sts[uid["sc_s1"]] == "cancelled" and sts[uid["sc_s2"]] == "sent")
    check("withdrawn_at is set and a withdrawn need cannot come back",
          one(admin, "SELECT withdrawn_at IS NOT NULL FROM needs WHERE id = :i", i=wid)
          and refused(engine, "UPDATE needs SET status = 'published' WHERE id = :i", why="invalid need status", i=wid))
    check("a sent pledge on a withdrawn need can still be confirmed",
          run(engine, "UPDATE pledges SET status = 'confirmed' WHERE id = :i", i=q)[0] == "ok")

    # ---- expiry
    dis = nid_of("disaster_recovery", 5000)
    publish(dis, "Repairs.")
    pledge(dis, uid["sc_s1"], 500)
    run(admin, "UPDATE pledges SET expires_at = now() - interval '1 day' WHERE need_id = :n", n=dis)
    check("expire_pledges expires an overdue pledge and frees its amount",
          run(engine, "SELECT expire_pledges()")[1][0][0] >= 1
          and one(admin, "SELECT status FROM pledges WHERE need_id = :n", n=dis) == "expired")

    # ---- the supporter limit
    med = nid_of("medical", 1000000)
    publish(med, "Treatment.")
    run(admin, "UPDATE pledges SET status = 'cancelled' WHERE supporter_id = :s AND status IN ('pledged', 'sent')", s=uid["sc_s1"])
    res = [pledge(med, uid["sc_s1"], fl)[0] for _ in range(mx + 2)]
    check(f"a supporter can hold only {mx} unconfirmed pledges ({res.count('ok')} accepted)", res.count("ok") == mx)

    # ---- concurrency: 20 simultaneous pledges against a goal that fits 10
    post("other", None, None, "TRY", 1000)
    cid = nid_of("other", 1000)
    publish(cid, "Race.")
    run(admin, "INSERT INTO users (username, password_hash, role) SELECT 'sc_c' || g, 'x', 'supporter' FROM generate_series(1, 20) g")
    cu = [r[0] for r in run(admin, "SELECT id FROM users WHERE left(username, 4) = 'sc_c' ORDER BY id")[1]]
    conc = create_engine(url, pool_size=20, max_overflow=0)
    barrier, out, lock = threading.Barrier(20), [], threading.Lock()

    def worker(u):
        try:
            with conc.begin() as c:
                barrier.wait(timeout=30)
                c.execute(text(P), {"n": cid, "s": u, "a": 100, "k": True})
            r = "ok"
        except DBAPIError:
            r = "err"
        except threading.BrokenBarrierError:
            r = "barrier"
        with lock:
            out.append(r)

    ts = [threading.Thread(target=worker, args=(u,)) for u in cu]
    [t.start() for t in ts]
    [t.join() for t in ts]
    tot = one(admin, "SELECT COALESCE(sum(amount_need), 0) FROM pledges WHERE need_id = :n", n=cid)
    check(f"20 simultaneous pledges of 100 against a goal of 1000: {out.count('ok')} accepted, total {tot}",
          out.count("ok") == 10 and out.count("err") == 10 and float(tot) == 1000.0)

    # ---- views
    def cols(v):
        return [r[0] for r in run(admin, "SELECT column_name FROM information_schema.columns WHERE table_name = :v ORDER BY ordinal_position", v=v)[1]]

    PRIVATE = {"description", "contact", "contact_method", "payee_name", "payee_details", "household_size", "requester_id",
               "review_note", "reviewed_by", "withdrawn_reason", "payee_verified"}
    check("public_needs lists no private column", not (set(cols("public_needs")) & PRIVATE))
    check("needs_analytics lists no private column except household size", not ((set(cols("needs_analytics")) & PRIVATE) - {"household_size"}))
    check("synth_analytics has the same columns as needs_analytics", cols("synth_analytics") == cols("needs_analytics"))
    check("the board shows only published needs",
          {r[0] for r in run(engine, "SELECT id FROM public_needs")[1]} == {r[0] for r in run(admin, "SELECT id FROM needs WHERE status = 'published'")[1]})
    pn = run(engine, "SELECT id, promised, confirmed, donors FROM public_needs WHERE id = :i", i=cid)[1][0]
    check("board totals are derived from pledges", float(pn[1]) == 1000.0 and float(pn[2]) == 0.0 and pn[3] == 10)
    check("band is null for euro needs and set for lira needs",
          one(engine, "SELECT band IS NULL FROM public_needs WHERE id = :i", i=eur) and one(engine, "SELECT band FROM public_needs WHERE id = :i", i=med) == 6)
    check("the app role cannot read needs_synth directly but can read its view",
          refused(engine, "SELECT * FROM needs_synth", why="permission") and run(engine, "SELECT count(*) FROM synth_analytics")[0] == "ok")
    exp = run(admin, "SELECT count(*), round(avg(d), 1) FROM (SELECT (SELECT count(DISTINCT supporter_id) FROM pledges p WHERE p.need_id = n.id AND p.status = 'confirmed') AS d "
                     "FROM needs n WHERE n.status = 'fulfilled' AND n.currency = 'TRY' AND n.goal >= 25000 AND n.goal < 100000) t WHERE d > 0")[1][0]
    got = run(engine, "SELECT finished_needs, avg_donors FROM suggestion_stats WHERE band = 4")[1]
    check("suggestion_stats matches an independent calculation for band 4",
          exp[0] >= 1 and got == [(exp[0], exp[1])])

    check("the real dataset budgets were not touched",
          run(admin, "SELECT name, epsilon_spent FROM datasets WHERE name IN ('needs_synthetic', 'needs_live') ORDER BY name")[1] == real_budgets)
finally:
    cleanup()
    left = one(admin, "SELECT count(*) FROM users WHERE left(username, 3) = 'sc_'")
    print(f"test rows removed (users left with the sc_ prefix: {left})")

assert not fails, f"failed: {fails}"
print(f"PASS: {total} checks")