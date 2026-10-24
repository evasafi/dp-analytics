import threading
from decimal import Decimal

from sqlalchemy import text

from app.budget import BudgetExhausted, spend
from app.db import engine

DATASET_ID = 1
EPS = 0.5
THREADS = 20

# Dev reset: only possible here because we connect as postgres for this one step.
# dp_app is not allowed to lower the budget (that is the point of the trigger).
import os
from sqlalchemy import create_engine
admin_url = os.environ["ADMIN_URL"]
admin = create_engine(admin_url)
with admin.begin() as c:
    c.execute(text("ALTER TABLE datasets DISABLE TRIGGER datasets_spend_only"))
    c.execute(text("UPDATE datasets SET epsilon_spent = 0 WHERE id = :i"), {"i": DATASET_ID})
    c.execute(text("ALTER TABLE datasets ENABLE TRIGGER datasets_spend_only"))

results = []
lock = threading.Lock()
barrier = threading.Barrier(THREADS)

def worker():
    barrier.wait()  # release all threads at the same moment
    try:
        with engine.begin() as conn:
            spend(conn, DATASET_ID, EPS)
        outcome = "ok"
    except BudgetExhausted:
        outcome = "refused"
    with lock:
        results.append(outcome)

threads = [threading.Thread(target=worker) for _ in range(THREADS)]
for t in threads:
    t.start()
for t in threads:
    t.join()

with engine.connect() as c:
    spent = c.execute(text("SELECT epsilon_spent FROM datasets WHERE id = :i"), {"i": DATASET_ID}).scalar_one()

print("succeeded:", results.count("ok"))
print("refused:  ", results.count("refused"))
print("final epsilon_spent:", spent)
assert results.count("ok") == 10, "lock failed: wrong number of successful spends"
assert spent == Decimal("5.0000"), "lock failed: budget total is wrong"
print("PASS: spend is atomic")
