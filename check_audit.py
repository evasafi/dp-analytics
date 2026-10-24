from sqlalchemy import text

from app.db import engine

with engine.connect() as c:
    print("budget:", c.execute(text(
        "SELECT epsilon_spent, epsilon_total FROM datasets WHERE id = 1")).one())
    for r in c.execute(text(
            "SELECT id, user_id, query_type, params, epsilon_charged, left(result_hash, 12) "
            "FROM audit_log ORDER BY id")):
        print(tuple(r))
