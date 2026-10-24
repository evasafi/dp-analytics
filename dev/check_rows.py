from sqlalchemy import text

from app.db import engine

with engine.connect() as c:
    print("rows:", c.execute(text("SELECT count(*) FROM beneficiaries WHERE dataset_id = 1")).scalar_one())
    for r in c.execute(text(
            "SELECT created_at, count(*) FROM beneficiaries WHERE dataset_id = 1 "
            "GROUP BY created_at ORDER BY created_at")):
        print(tuple(r))
