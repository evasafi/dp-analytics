import getpass
from decimal import Decimal
from urllib.parse import quote

from sqlalchemy import create_engine, text

from app.db import engine
from app.queries import Refused, mean_transfer

pw = getpass.getpass("postgres password: ")
admin = create_engine(
    f"postgresql+psycopg://postgres:{quote(pw, safe='')}@localhost:5432/dp_analytics"
)

with admin.begin() as c:
    c.execute(text("DELETE FROM audit_log WHERE dataset_id IN "
                   "(SELECT id FROM datasets WHERE name = 'refusal_test')"))
    c.execute(text("DELETE FROM beneficiaries WHERE dataset_id IN "
                   "(SELECT id FROM datasets WHERE name = 'refusal_test')"))
    c.execute(text("DELETE FROM datasets WHERE name = 'refusal_test'"))
    c.execute(text("INSERT INTO datasets (name, epsilon_total) VALUES ('refusal_test', 50)"))
    ds = c.execute(text("SELECT id FROM datasets WHERE name = 'refusal_test'")).scalar_one()
    c.execute(text("INSERT INTO beneficiaries (dataset_id, beneficiary_id, region, "
                   "household_size, monthly_transfer) VALUES (:d, 'solo', 'region_1', 2, 100)"),
              {"d": ds})

def books():
    with engine.connect() as c:
        spent = c.execute(text("SELECT epsilon_spent FROM datasets WHERE id = :d"), {"d": ds}).scalar_one()
        logged = c.execute(text("SELECT count(*) FROM audit_log WHERE dataset_id = :d"), {"d": ds}).scalar_one()
    return spent, logged

refused = answered = 0
for _ in range(60):
    before = books()
    with engine.begin() as conn:
        r = mean_transfer(conn, 2, ds, "region_1")
    after = books()
    assert after[0] - before[0] == Decimal("0.2"), "spend did not move by 0.2"
    assert after[1] - before[1] == 1, "audit row missing"
    if isinstance(r, Refused):
        refused += 1
    else:
        answered += 1

print("refused:", refused, " answered:", answered)
print("final books (spent, audit rows):", books())
assert refused > 0, "never saw a refusal; rerun"
print("PASS: refusals are charged and logged")

