from sqlalchemy import text

from app.db import engine
from app.engine import Spec
from app.engine_run import run_query

with engine.connect() as c:
    uid = c.execute(text("SELECT id FROM users WHERE username='analyst1'")).scalar_one()

with engine.begin() as conn:
    res = run_query(conn, uid, 1, Spec("count", ("region",)))
print("returned ids:", res["audit_id"], res["result_id"])

with engine.connect() as c:
    row = c.execute(text("SELECT user_id, audit_id, jsonb_array_length(cells) AS n FROM results "
                         "WHERE id = :r"), {"r": res["result_id"]}).one()
    spent = c.execute(text("SELECT epsilon_spent FROM datasets WHERE id = 1")).scalar_one()
print("stored: owner", row.user_id, "(expect", uid, "), audit", row.audit_id,
      "(expect", res["audit_id"], "), cells", row.n, "(expect 8)")
print("spent:", spent, "(expect 0.1000)")
assert row.user_id == uid and row.audit_id == res["audit_id"] and row.n == 8
print("PASS")
