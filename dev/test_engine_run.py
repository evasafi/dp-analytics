import getpass
import threading
from decimal import Decimal
from urllib.parse import quote

from sqlalchemy import create_engine, text

from app.budget import BudgetExhausted
from app.db import engine
from app.engine import Spec
from app.engine_run import run_query

pw = getpass.getpass("postgres password: ")
admin = create_engine(f"postgresql+psycopg://postgres:{quote(pw, safe='')}@localhost:5432/dp_analytics_clone")


def cleanup(c):
    c.execute(text("DELETE FROM results WHERE dataset_id IN (SELECT id FROM datasets WHERE name='engine_test')"))
    c.execute(text("DELETE FROM audit_log WHERE dataset_id IN (SELECT id FROM datasets WHERE name='engine_test')"))
    c.execute(text("DELETE FROM beneficiaries WHERE dataset_id IN (SELECT id FROM datasets WHERE name='engine_test')"))
    c.execute(text("DELETE FROM datasets WHERE name='engine_test'"))


with admin.begin() as c:
    cleanup(c)
    c.execute(text("INSERT INTO datasets (name, epsilon_total) VALUES ('engine_test', 1.0)"))
    ds = c.execute(text("SELECT id FROM datasets WHERE name='engine_test'")).scalar_one()
    uid = c.execute(text("SELECT id FROM users WHERE username='analyst1'")).scalar_one()
    for i, (r, h, t) in enumerate([("region_1", 2, 100), ("region_1", 3, 150), ("region_2", 2, 90)]):
        c.execute(text("INSERT INTO beneficiaries (dataset_id, beneficiary_id, region, household_size, "
                       "monthly_transfer) VALUES (:d, :b, :r, :h, :t)"),
                  {"d": ds, "b": f"e{i}", "r": r, "h": h, "t": t})


def one(sql):
    with engine.connect() as c:
        return c.execute(text(sql), {"d": ds}).scalar_one()


try:
    # 1. a bad spec is refused before anything is charged
    try:
        with engine.begin() as conn:
            run_query(conn, uid, ds, Spec("count", ("password_hash",)))
        print("1. bad spec: NOT refused")
    except ValueError:
        pass
    print("1. bad spec refused, spent:", one("SELECT epsilon_spent FROM datasets WHERE id=:d"), "(expect 0.0000)")

    # 2. one 96-cell query: charged once, one audit row, all cells returned
    with engine.begin() as conn:
        res = run_query(conn, uid, ds, Spec("count", ("region", "household_size")))
    print("2. cells:", len(res["cells"]), "(expect 96); spent:", one("SELECT epsilon_spent FROM datasets WHERE id=:d"),
          "(expect 0.1000); audit rows:", one("SELECT count(*) FROM audit_log WHERE dataset_id=:d"), "(expect 1)")

    # 3. a mean costs 0.5
    with engine.begin() as conn:
        run_query(conn, uid, ds, Spec("mean", ("region",)))
    print("3. spent after a mean:", one("SELECT epsilon_spent FROM datasets WHERE id=:d"), "(expect 0.6000)")

    # 4. concurrency: reset to 0 spent, then 20 simultaneous 96-cell queries on a budget of 1.0
    with admin.begin() as c:
        c.execute(text("ALTER TABLE datasets DISABLE TRIGGER datasets_spend_only"))
        c.execute(text("UPDATE datasets SET epsilon_spent = 0 WHERE id = :d"), {"d": ds})
        c.execute(text("ALTER TABLE datasets ENABLE TRIGGER datasets_spend_only"))
        c.execute(text("DELETE FROM results WHERE dataset_id = :d"), {"d": ds})
        c.execute(text("DELETE FROM audit_log WHERE dataset_id = :d"), {"d": ds})
    results, lock, barrier = [], threading.Lock(), threading.Barrier(20)

    def worker():
        barrier.wait()
        try:
            with engine.begin() as conn:
                run_query(conn, uid, ds, Spec("count", ("region", "household_size")))
            out = "ok"
        except BudgetExhausted:
            out = "refused"
        with lock:
            results.append(out)

    ts = [threading.Thread(target=worker) for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ok = results.count("ok")
    spent = one("SELECT epsilon_spent FROM datasets WHERE id=:d")
    rows = one("SELECT count(*) FROM audit_log WHERE dataset_id=:d")
    print("4. succeeded:", ok, "refused:", results.count("refused"), "(expect 10 and 10); spent:", spent,
          "(expect 1.0000); audit rows:", rows, "(expect 10)")
    assert ok == 10 and spent == Decimal("1.0000") and rows == 10
    print("PASS: one charge per query, not per cell, and the budget holds under concurrency")
finally:
    with admin.begin() as c:
        cleanup(c)
    print("throwaway dataset removed")
