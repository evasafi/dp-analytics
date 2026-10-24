from sqlalchemy import text

from app.db import engine
from app.queries import count_by_region, household_histogram, mean_transfer

DS, USER = 1, 2

with engine.begin() as conn:
    print("count region_8:", count_by_region(conn, USER, DS, "region_8"))
with engine.begin() as conn:
    print("histogram:", household_histogram(conn, USER, DS))
with engine.begin() as conn:
    print("mean region_8:", round(mean_transfer(conn, USER, DS, "region_8"), 1))

with engine.connect() as c:
    print("budget:", c.execute(text(
        "SELECT epsilon_spent, epsilon_total FROM datasets WHERE id = 1")).one())
    for r in c.execute(text(
            "SELECT id, query_type, params, epsilon_charged, left(result_hash, 12) "
            "FROM audit_log ORDER BY id")):
        print(r)
