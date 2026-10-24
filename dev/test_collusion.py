import getpass
import threading
from decimal import Decimal
from urllib.parse import quote

from sqlalchemy import create_engine, text

from app.auth import hash_password
from app.budget import BudgetExhausted
from app.db import engine
from app.queries import count_by_region

pw = getpass.getpass("postgres password: ")
admin = create_engine(
    f"postgresql+psycopg://postgres:{quote(pw, safe='')}@localhost:5432/dp_analytics"
)

with admin.begin() as c:
    c.execute(text("DELETE FROM audit_log WHERE dataset_id IN "
                   "(SELECT id FROM datasets WHERE name = 'collusion_test')"))
    c.execute(text("DELETE FROM beneficiaries WHERE dataset_id IN "
                   "(SELECT id FROM datasets WHERE name = 'collusion_test')"))
    c.execute(text("DELETE FROM datasets WHERE name = 'collusion_test'"))
    c.execute(text("INSERT INTO datasets (name, epsilon_total) VALUES ('collusion_test', 1.0)"))
    ds = c.execute(text("SELECT id FROM datasets WHERE name = 'collusion_test'")).scalar_one()
    c.execute(text("INSERT INTO beneficiaries (dataset_id, beneficiary_id, region, "
                   "household_size, monthly_transfer) VALUES (:d, 'a1', 'region_1', 2, 100)"),
              {"d": ds})
    ids = {}
    for name in ("collude_a", "collude_b"):
        c.execute(text("DELETE FROM audit_log WHERE user_id IN (SELECT id FROM users WHERE username = :u)"),
                  {"u": name})
        c.execute(text("DELETE FROM users WHERE username = :u"), {"u": name})
        c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, 'analyst')"),
                  {"u": name, "h": hash_password("throwaway-password-1")})
        ids[name] = c.execute(text("SELECT id FROM users WHERE username = :u"), {"u": name}).scalar_one()

results = []
lock = threading.Lock()
barrier = threading.Barrier(20)


def worker(user_id):
    barrier.wait()
    try:
        with engine.begin() as conn:
            count_by_region(conn, user_id, ds, "region_1")
        outcome = (user_id, "ok")
    except BudgetExhausted:
        outcome = (user_id, "refused")
    with lock:
        results.append(outcome)


threads = [threading.Thread(target=worker, args=(ids["collude_a" if i % 2 == 0 else "collude_b"],))
           for i in range(20)]
for t in threads:
    t.start()
for t in threads:
    t.join()

with engine.connect() as c:
    spent = c.execute(text("SELECT epsilon_spent FROM datasets WHERE id = :d"), {"d": ds}).scalar_one()
    per_user = dict(c.execute(text(
        "SELECT user_id, count(*) FROM audit_log WHERE dataset_id = :d GROUP BY user_id"), {"d": ds}).all())

ok = sum(1 for _, o in results if o == "ok")
print("succeeded:", ok, " refused:", len(results) - ok, "(expect 10 and 10)")
print("final epsilon_spent:", spent, "(expect 1.0000)")
print("audit rows per account:", per_user, "(expect the two counts to sum to 10)")
assert ok == 10, "two accounts got more than the shared budget allows"
assert spent == Decimal("1.0000"), "budget total is wrong"
assert sum(per_user.values()) == 10, "audit rows do not match successful queries"
assert set(per_user) <= set(ids.values()), "audit rows attributed to an unknown account"
print("PASS: two analyst accounts share one budget, and every spend is attributed")
