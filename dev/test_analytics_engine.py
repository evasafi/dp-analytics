"""Needs analytics engine on the financial schema: the per-person cap on both views, fixed groupings, refusals that
charge nothing, charging and audit, budget exhaustion, many analysts spending one budget at once, the noise scale and
suppression.

Run from the project root as a module: python -m dev.test_analytics_engine
Needs the database owner's password (hidden prompt). The owner connection is built from the app's own
DP_DATABASE_URL and the test refuses to run against a database without the financial-needs schema.
It spends only throwaway datasets (names starting an_test_), never needs_synthetic or needs_live, and checks that.
Cap probes insert rows as the owner inside a transaction that is always rolled back. Users have the prefix ae_.
"""
import getpass
import math
import threading
from decimal import Decimal

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import app.needs_engine as ne
from app.auth import hash_password
from app.budget import BudgetExhausted
from app.config import settings
from app.db import engine
from app.districts import DISTRICTS_BY_PROVINCE
from app.engine import Spec
from app.noise import _laplace

admin = create_engine(make_url(settings.database_url).set(username="postgres", password=getpass.getpass("postgres password: ")))
EPS = settings.epsilon_per_count
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


def one(sql, **p):
    with admin.connect() as c:
        return c.execute(text(sql), p).scalar_one()


def spent(name):
    return one("SELECT epsilon_spent FROM datasets WHERE name = :n", n=name)


def cleanup():
    with admin.begin() as c:
        tests = "(SELECT id FROM datasets WHERE left(name, 8) = 'an_test_')"
        c.execute(text(f"DELETE FROM results WHERE dataset_id IN {tests}"))
        c.execute(text(f"DELETE FROM audit_log WHERE dataset_id IN {tests}"))
        c.execute(text("DELETE FROM datasets WHERE left(name, 8) = 'an_test_'"))
        c.execute(text("DELETE FROM users WHERE left(username, 3) = 'ae_'"))


def new_dataset(name, runs, view="synth_analytics"):
    with admin.begin() as c:
        c.execute(text("INSERT INTO datasets (name, epsilon_total) VALUES (:n, :t)"), {"n": name, "t": Decimal(str(EPS)) * Decimal(str(runs))})
    ne.SOURCES[name] = view


cleanup()
with admin.begin() as c:
    for u in ("ae_an1", "ae_an2", "ae_req"):
        c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r)"),
                  {"u": u, "h": hash_password("throwaway-password-1"), "r": "requester" if u == "ae_req" else "analyst"})
uid = {u: one("SELECT id FROM users WHERE username = :u", u=u) for u in ("ae_an1", "ae_an2", "ae_req")}
REAL = {n: spent(n) for n in ("needs_synthetic", "needs_live")}
ISTANBUL, ANKARA = "Istanbul", "Ankara"
D_IST, D_ANK = DISTRICTS_BY_PROVINCE[ISTANBUL][0], DISTRICTS_BY_PROVINCE[ANKARA][0]
orig_max_cells = ne.MAX_CELLS
ALL = {"need_type": ("need_type",), "province": ("province",), "status": ("status",),
       "need_type_status": ("need_type", "status"), "band": ("band",)}

# a synthetic row: (purpose, medical_location, event, province, currency, goal, status)
RENT = ("rent_housing", None, None, ISTANBUL, "TRY", 2000, "published")
FOOD = ("food", None, None, ANKARA, "TRY", 600, "fulfilled")
DEBT = ("debt", None, None, ISTANBUL, "TRY", 30000, "published")
OTHER = ("other", None, None, ANKARA, "TRY", 150000, "published")
ABROAD = ("medical", "abroad", None, ISTANBUL, "EUR", 5000, "published")


def synth_probe(rows, spec):
    """Add one person's rows to needs_synth as the owner, compare exact counts before and after, always roll back."""
    with admin.connect() as c:
        t = c.begin()
        before = ne.true_counts(c, "needs_synthetic", spec)
        for pu, loc, ev, pv, cur, g, st in rows:
            c.execute(text("INSERT INTO needs_synth (person_id, purpose, medical_location, event, province, household_size, currency, goal, status) "
                           "VALUES ('captest_person', :pu, :loc, :ev, :pv, 3, :cur, :g, :st)"),
                      {"pu": pu, "loc": loc, "ev": ev, "pv": pv, "cur": cur, "g": g, "st": st})
        after = ne.true_counts(c, "needs_synthetic", spec)
        t.rollback()
    return {k: after[k] - before[k] for k in after}


def live_probe(rows, spec):
    """Same on the live view: one requester's needs, each moved to the given status, always rolled back."""
    with admin.connect() as c:
        t = c.begin()
        before = ne.true_counts(c, "needs_live", spec)
        for pu, loc, ev, pv, cur, g, st in rows:
            nid = c.execute(text(
                "INSERT INTO needs (requester_id, purpose, medical_location, event, currency, province, district, household_size, goal, "
                "description, contact, payee_name, payee_details) VALUES (:r, :pu, :loc, :ev, :cur, :pv, :di, 3, :g, "
                "'A need created by the analytics test.', 'ask the test', 'Ayse Demir', 'TR330006100519786457841326') RETURNING id"),
                {"r": uid["ae_req"], "pu": pu, "loc": loc, "ev": ev, "cur": cur, "pv": pv, "di": D_IST if pv == ISTANBUL else D_ANK, "g": g}).scalar_one()
            if st in ("published", "fulfilled", "withdrawn"):
                c.execute(text("UPDATE needs SET status = 'published', public_text = 'Help.', payee_verified = true WHERE id = :i"), {"i": nid})
            if st in ("fulfilled", "withdrawn", "rejected"):
                c.execute(text("UPDATE needs SET status = :s, withdrawn_reason = CASE WHEN :s = 'withdrawn' THEN 'No longer needed.' END "
                               "WHERE id = :i"), {"s": st, "i": nid})
        after = ne.true_counts(c, "needs_live", spec)
        t.rollback()
    return {k: after[k] - before[k] for k in after}


try:
    # ---- A. the per-person cap (exact counts, no noise, always rolled back)
    nt = Spec("count", ("need_type",))
    d = synth_probe([RENT] * 10, nt)
    check("a person with 10 needs adds exactly 3", sum(d.values()) == 3 and d[("rent_housing",)] == 3)
    d = synth_probe([RENT, FOOD, ABROAD] + [DEBT] * 7, nt)
    check("the counted needs are the person's first 3 by id",
          d[("rent_housing",)] == 1 and d[("food",)] == 1 and d[("medical_abroad",)] == 1 and d[("debt",)] == 0)
    check("a person with 2 needs adds 2", sum(synth_probe([RENT, FOOD], nt).values()) == 2)
    for g, dims in ALL.items():
        d = synth_probe([RENT, FOOD, ABROAD, OTHER] + [DEBT] * 6, Spec("count", dims))
        check(f"grouping {g}: one person changes the total by at most 3 and any cell by at most 3",
              sum(d.values()) <= 3 and max(abs(v) for v in d.values()) <= 3)
    d = synth_probe([ABROAD, RENT, RENT, DEBT, DEBT], Spec("count", ("band",)))
    check("a euro need uses one of the 3 places but lands in no goal band", sum(d.values()) == 2 and d[(2,)] == 2 and d[(4,)] == 0)
    d = synth_probe([RENT, FOOD], Spec("count", ("need_type", "status")))
    check("need type by status puts each need in its own cell", d[("rent_housing", "published")] == 1 and d[("food", "fulfilled")] == 1)
    d = live_probe([RENT] * 3 + [FOOD], nt)
    check("live view: the cap holds there too (4 published needs add 3)", sum(d.values()) == 3 and d[("food",)] == 0)
    d = live_probe([(*RENT[:6], "pending"), (*RENT[:6], "rejected"), (*RENT[:6], "withdrawn"), FOOD], nt)
    check("live view: pending, rejected and withdrawn needs are not counted, and do not use up a place",
          sum(d.values()) == 1 and d[("food",)] == 1)

    # ---- B. refusals charge nothing
    new_dataset("an_test_refuse", 10)
    bad = {"a mean": Spec("mean", ("need_type",)), "a filter": Spec("count", ("need_type",), (("need_type", ("food",)),)),
           "no grouping": Spec("count", ()), "province by status": Spec("count", ("province", "status")),
           "province by need type": Spec("count", ("province", "need_type")), "an unknown dimension": Spec("count", ("payee_name",)),
           "household size": Spec("count", ("household_size",)), "a repeated dimension": Spec("count", ("need_type", "need_type")),
           "three dimensions": Spec("count", ("need_type", "status", "band"))}
    for label, spec in bad.items():
        try:
            with engine.begin() as conn:
                ne.run_needs_query(conn, uid["ae_an1"], "an_test_refuse", spec)
            refused = False
        except ValueError:
            refused = True
        check(f"refused, nothing charged: {label}", refused and spent("an_test_refuse") == 0)
    with admin.begin() as c:
        c.execute(text("INSERT INTO datasets (name, epsilon_total) VALUES ('an_test_unlisted', 1.0)"))
    for name in ("an_test_unlisted", "needs_synth", "datasets", "synth_analytics; DROP TABLE needs", ""):
        try:
            with engine.begin() as conn:
                ne.run_needs_query(conn, uid["ae_an1"], name, nt)
            refused = False
        except ValueError:
            refused = True
        check(f"refused: a dataset that is not on the list ({name!r})", refused)
    check("the unlisted dataset was not charged", spent("an_test_unlisted") == 0)
    sizes = {g: ne.cell_count(Spec("count", dims)) for g, dims in ALL.items()}
    check(f"the allowed groupings have fixed sizes: {sizes}", sizes == {"need_type": 9, "province": 81, "status": 2, "need_type_status": 18, "band": 6})
    ne.MAX_CELLS = 10
    try:
        ne.validate_needs(Spec("count", ("province",)), "an_test_refuse")
        guard = False
    except ValueError:
        guard = True
    ne.MAX_CELLS = orig_max_cells
    check("the cell limit refuses 81 cells when it is set to 10", guard)

    # ---- C. charging, audit, storage and exhaustion (the budget allows exactly 2 runs)
    new_dataset("an_test_two", 2.5)
    with engine.begin() as conn:
        res = ne.run_needs_query(conn, uid["ae_an1"], "an_test_two", Spec("count", ("need_type", "status")))
    check("a run returns all 18 cells", len(res["cells"]) == 18)
    check("every value is suppressed (None) or a whole number of at least 30",
          all(v is None or (isinstance(v, int) and v >= 30) for v in res["cells"].values()))
    check("it charged exactly one query's epsilon", spent("an_test_two") == Decimal(str(EPS)))
    ds = one("SELECT id FROM datasets WHERE name = 'an_test_two'")
    with admin.connect() as c:
        a = c.execute(text("SELECT query_type, epsilon_charged, params->>'dataset' AS d FROM audit_log WHERE dataset_id = :d"), {"d": ds}).one()
        stored = c.execute(text("SELECT cells FROM results WHERE id = :r"), {"r": res["result_id"]}).scalar_one()
    check("one audit row: needs_count, the epsilon and the dataset name", tuple(a) == ("needs_count", Decimal(str(EPS)), "an_test_two"))
    check("the stored cells equal the returned cells", {tuple(k): v for k, v in stored} == res["cells"])
    with engine.begin() as conn:
        ne.run_needs_query(conn, uid["ae_an2"], "an_test_two", Spec("count", ("band",)))
    check("a second run, by another analyst, charges the same budget again", spent("an_test_two") == Decimal(str(EPS)) * 2)
    try:
        with engine.begin() as conn:
            ne.run_needs_query(conn, uid["ae_an1"], "an_test_two", Spec("count", ("status",)))
        exhausted = False
    except BudgetExhausted:
        exhausted = True
    check("a third run is refused: budget exhausted", exhausted)
    check("the refused run left the budget, the audit log and the results unchanged",
          spent("an_test_two") == Decimal(str(EPS)) * 2 and one("SELECT count(*) FROM audit_log WHERE dataset_id = :d", d=ds) == 2
          and one("SELECT count(*) FROM results WHERE dataset_id = :d", d=ds) == 2)

    # ---- D. two analysts spending one budget at once (the budget allows exactly 5 runs)
    new_dataset("an_test_race", 5)
    barrier, lock, out = threading.Barrier(20), threading.Lock(), []

    def worker(i):
        barrier.wait(timeout=30)
        try:
            with engine.begin() as conn:
                ne.run_needs_query(conn, uid["ae_an1" if i % 2 else "ae_an2"], "an_test_race", Spec("count", ("status",)))
            r = "ok"
        except BudgetExhausted:
            r = "refused"
        with lock:
            out.append(r)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    rid = one("SELECT id FROM datasets WHERE name = 'an_test_race'")
    check(f"20 simultaneous runs by two analysts on a budget for 5: {out.count('ok')} answered, {out.count('refused')} refused",
          out.count("ok") == 5 and out.count("refused") == 15)
    check("the budget is spent exactly, never more, with 5 audit rows",
          spent("an_test_race") == Decimal(str(EPS)) * 5 and one("SELECT count(*) FROM audit_log WHERE dataset_id = :d", d=rid) == 5)

    # ---- E. noise scale and suppression (statistical; the tolerances are my choice)
    N = 5000
    b = ne.SENSITIVITY / EPS
    mad = sum(abs(ne.noisy_counts({("x",): 1000}, EPS)[("x",)] - 1000) for _ in range(N)) / N
    print(f"     mean absolute noise: {mad:.2f}; expected scale {b:.2f}")
    check("the noise matches the scale 3 / epsilon (within 10 percent)", abs(mad / b - 1) < 0.10)
    mad1 = sum(abs(_laplace(1000, EPS, 1) - 1000) for _ in range(N)) / N
    check("it is about 3 times the noise at sensitivity 1", mad > 2.5 * mad1)
    p = 0.5 * math.exp(-29.5 / b)
    shown = [ne.noisy_counts({("x",): 0}, EPS)[("x",)] for _ in range(N)]
    rate = sum(v is not None for v in shown) / N
    print(f"     a true 0 shown as 30 or more: {rate:.3f}; expected about {p:.3f}")
    check("a truly empty cell is shown at the expected rate (within 0.02)", abs(rate - p) < 0.02)
    check("nothing between 0 and 29 is ever released", all(v is None or v >= 30 for v in shown))

    # ---- F. the real budgets
    check("needs_synthetic and needs_live were not charged by this test", {n: spent(n) for n in REAL} == REAL)
finally:
    ne.MAX_CELLS = orig_max_cells
    for n in [k for k in ne.SOURCES if k.startswith("an_test_")]:
        ne.SOURCES.pop(n)
    cleanup()
    left = one("SELECT count(*) FROM datasets WHERE left(name, 8) = 'an_test_'")
    print(f"throwaway datasets and users removed (datasets left with the an_test_ prefix: {left})")

assert not fails, f"failed: {fails}"
print(f"PASS: {total} checks")
