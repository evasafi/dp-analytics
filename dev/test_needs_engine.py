import getpass
import math
from decimal import Decimal
from urllib.parse import quote

from sqlalchemy import create_engine, text

import app.needs_engine as ne
from app.budget import BudgetExhausted
from app.config import settings
from app.db import engine
from app.engine import Spec
from app.noise import _laplace

pw = getpass.getpass("postgres password: ")
admin = create_engine(f"postgresql+psycopg://postgres:{quote(pw, safe='')}@localhost:5432/dp_analytics")

EPS = settings.epsilon_per_count
SYN = ne.needs_dataset_id()
checks = 0


def check(cond, label):
    global checks
    checks += 1
    if not cond:
        raise AssertionError("FAILED: " + label)
    print("ok:", label)


def cleanup(c):
    c.execute(text("DELETE FROM results WHERE dataset_id IN (SELECT id FROM datasets WHERE name='needs_test')"))
    c.execute(text("DELETE FROM audit_log WHERE dataset_id IN (SELECT id FROM datasets WHERE name='needs_test')"))
    c.execute(text("DELETE FROM datasets WHERE name='needs_test'"))


def scalar(sql, **p):
    with engine.connect() as c:
        return c.execute(text(sql), p).scalar_one()


def spent(i):
    return scalar("SELECT epsilon_spent FROM datasets WHERE id=:d", d=i)


with admin.begin() as c:
    cleanup(c)
    c.execute(text("INSERT INTO datasets (name, epsilon_total) VALUES ('needs_test', :t)"),
              {"t": Decimal(str(EPS)) * Decimal("2.5")})
    ds = c.execute(text("SELECT id FROM datasets WHERE name='needs_test'")).scalar_one()
    uid = c.execute(text("SELECT id FROM users WHERE username='analyst1'")).scalar_one()

rows_before = scalar("SELECT count(*) FROM needs_synth")
LIVE = scalar("SELECT id FROM datasets WHERE name = 'needs_live'")
spent7, spent8 = spent(SYN), spent(LIVE)
orig_max_cells = ne.MAX_CELLS
ne.SOURCES[ds] = "needs_synth"      # test-only: whitelist the throwaway dataset


def cap_probe(person_rows, spec):
    """Insert one person's needs as owner, compare exact counts before and after, always roll back."""
    with admin.connect() as c:
        t = c.begin()
        before = ne.true_counts(c, SYN, spec)
        for cat, prov in person_rows:
            c.execute(text("INSERT INTO needs_synth (person_id, category, province, status, created_at) "
                           "VALUES ('captest_person', :c, :v, 'published', now())"), {"c": cat, "v": prov})
        after = ne.true_counts(c, SYN, spec)
        t.rollback()
    return {k: after[k] - before[k] for k in after}


try:
    # A. person cap (exact counts, no noise)
    cat = Spec("count", ("category",))
    d = cap_probe([("health", "Ordu")] * 10, cat)
    check(sum(d.values()) == 3 and d[("health",)] == 3, "A1 a person with 10 needs adds exactly 3 (category)")
    d = cap_probe([("health", "Ordu")] * 3 + [("sports", "Adana")] * 7, cat)
    check(d[("health",)] == 3 and d[("sports",)] == 0, "A2 the counted needs are the person's first 3 by id")
    d = cap_probe([("health", "Ordu")] * 3 + [("sports", "Adana")] * 7, Spec("count", ("province",)))
    check(d[("Ordu",)] == 3 and d[("Adana",)] == 0, "A2b same, by province")
    d = cap_probe([("health", "Ordu")] * 2, cat)
    check(sum(d.values()) == 2, "A3 a person with 2 needs adds 2, not 3")
    mixed = [("health", "Ordu"), ("sports", "Adana"), ("education", "Izmir")] + [("hygiene", "Bursa")] * 7
    for g in (("category",), ("province",), ("status",), ("category", "status")):
        d = cap_probe(mixed, Spec("count", g))
        check(sum(d.values()) == 3 and max(abs(v) for v in d.values()) <= 3, f"A4 total change 3, max cell change <= 3 for {g}")

    # B. refusals charge nothing
    bad = {
        "mean": Spec("mean", ("category",)),
        "filter": Spec("count", ("category",), (("category", ("health",)),)),
        "ungrouped": Spec("count", ()),
        "province x status": Spec("count", ("province", "status")),
        "province x category": Spec("count", ("province", "category")),
        "unknown dimension": Spec("count", ("password_hash",)),
        "repeated dimension": Spec("count", ("category", "category")),
        "three dimensions": Spec("count", ("category", "status", "province")),
    }
    for name, spec in bad.items():
        try:
            with engine.begin() as conn:
                ne.run_needs_query(conn, uid, ds, spec)
            check(False, f"B {name} was NOT refused")
        except ValueError:
            pass
        check(spent(ds) == 0, f"B refused: {name}; spent still 0")
    for other in (LIVE, 999999):
        try:
            with engine.begin() as conn:
                ne.run_needs_query(conn, uid, other, cat)
            check(False, f"B dataset {other} was NOT refused")
        except ValueError:
            pass
    check(spent(SYN) == spent7 and spent(LIVE) == spent8, "B unlisted dataset ids refused; budgets of needs_synthetic and needs_live untouched")
    for g, n in ((("category",), 10), (("province",), 81), (("status",), 5), (("category", "status"), 50)):
        ne.validate_needs(Spec("count", g), ds)
        check(ne.cell_count(Spec("count", g)) == n, f"B allowed grouping {g} has {n} cells")
    ne.MAX_CELLS = 10
    try:
        ne.validate_needs(Spec("count", ("province",)), ds)
        check(False, "B cell limit did not refuse 81 cells with limit 10")
    except ValueError:
        check(True, "B cell limit guard refuses 81 cells when limit is 10")
    ne.validate_needs(Spec("count", ("category",)), ds)
    ne.MAX_CELLS = orig_max_cells

    # C. charging, audit, storage, exhaustion (budget allows exactly 2 runs)
    with engine.begin() as conn:
        res = ne.run_needs_query(conn, uid, ds, Spec("count", ("category", "status")))
    check(len(res["cells"]) == 50, "C1 all 50 cells returned")
    check(all(v is None or (isinstance(v, int) and v >= 30) for v in res["cells"].values()),
          "C1 every value is None or an integer >= 30")
    check(spent(ds) == Decimal(str(EPS)), "C1 charged exactly one epsilon")
    check(scalar("SELECT count(*) FROM audit_log WHERE dataset_id=:d AND query_type='needs_count'", d=ds) == 1, "C1 one audit row, query_type needs_count")
    check(scalar("SELECT epsilon_charged FROM audit_log WHERE dataset_id=:d", d=ds) == Decimal(str(EPS)), "C1 audit epsilon matches")
    stored = scalar("SELECT cells FROM results WHERE id=:r", r=res["result_id"])
    check({tuple(k): v for k, v in stored} == res["cells"], "C1 stored cells equal returned cells")
    with engine.begin() as conn:
        ne.run_needs_query(conn, uid, ds, Spec("count", ("province",)))
    check(spent(ds) == Decimal(str(EPS)) * 2, "C2 second run charged again")
    try:
        with engine.begin() as conn:
            ne.run_needs_query(conn, uid, ds, Spec("count", ("status",)))
        check(False, "C3 third run was NOT refused")
    except BudgetExhausted:
        pass
    check(spent(ds) == Decimal(str(EPS)) * 2, "C3 exhausted run left the budget unchanged")
    check(scalar("SELECT count(*) FROM audit_log WHERE dataset_id=:d", d=ds) == 2
          and scalar("SELECT count(*) FROM results WHERE dataset_id=:d", d=ds) == 2, "C3 exhausted run left no audit or result rows")

    # D. noise scale (statistical; tolerance 10 percent is my choice)
    N = 5000
    b = ne.SENSITIVITY / EPS
    mad = sum(abs(ne.noisy_counts({("x",): 1000}, EPS)[("x",)] - 1000) for _ in range(N)) / N
    print(f"   mean absolute noise via noisy_counts: {mad:.2f}; expected scale {b:.2f}")
    check(abs(mad / b - 1) < 0.10, "D1 noisy_counts noise matches scale 3/epsilon")
    b1 = 1 / EPS
    mad1 = sum(abs(_laplace(1000, EPS, 1) - 1000) for _ in range(N)) / N
    print(f"   mean absolute noise at sensitivity 1: {mad1:.2f}; expected {b1:.2f}")
    check(abs(mad1 / b1 - 1) < 0.10, "D2 library scale is sensitivity/epsilon (checked at sensitivity 1)")
    check(mad > 2.5 * mad1, "D3 needs noise is about 3 times the sensitivity-1 noise")

    # E. suppression of a truly empty cell
    p = 0.5 * math.exp(-29.5 / b)
    shown = [ne.noisy_counts({("x",): 0}, EPS)[("x",)] for _ in range(N)]
    rate = sum(v is not None for v in shown) / N
    print(f"   true-0 cell shown (>= 30): {rate:.3f}; expected about {p:.3f}")
    check(abs(rate - p) < 0.02, "E1 a true-0 cell is shown at the expected rate")
    check(all(v is None or v >= 30 for v in shown), "E2 nothing between 0 and 29 is ever released")

    # F. nothing left behind
    check(scalar("SELECT count(*) FROM needs_synth") == rows_before, "F needs_synth row count unchanged")
    print(f"PASS: {checks} checks")
finally:
    ne.MAX_CELLS = orig_max_cells
    ne.SOURCES.pop(ds, None)
    with admin.begin() as c:
        cleanup(c)
    print("throwaway dataset removed")