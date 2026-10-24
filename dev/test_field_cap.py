import re
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.db import engine
from app.main import app

pw = input("field1 password (visible): ")
settings.field_daily_cap = 5

def one(sql):
    with engine.connect() as c:
        return c.execute(text(sql)).scalar_one()

cl = TestClient(app, base_url="http://127.0.0.1:8000")
tok = re.search(r'name="csrf" value="([^"]+)"', cl.get("/login").text).group(1)
print("login:", cl.post("/login", data={"username": "field1", "password": pw, "csrf": tok},
                          follow_redirects=False).status_code, "(expect 303)")
tok = re.search(r'name="csrf" value="([^"]+)"', cl.get("/field").text).group(1)

rows0 = one("SELECT count(*) FROM beneficiaries WHERE dataset_id = 1")
used0 = one("SELECT count(*) FROM audit_log WHERE query_type = 'submit' AND ts > now() - interval '24 hours'")
settings.field_daily_cap = used0 + 5
codes = [cl.post("/field/submit", data={"region": "region_1", "household_size": 2,
         "monthly_transfer": "100", "csrf": tok}, follow_redirects=False).status_code
         for _ in range(7)]
print("7 submissions:", codes, "(expect five 303 then two 429)")
print("rows added:", one("SELECT count(*) FROM beneficiaries WHERE dataset_id = 1") - rows0, "(expect 5)")
print("audit rows added:", one("SELECT count(*) FROM audit_log WHERE query_type = 'submit' AND ts > now() - interval '24 hours'") - used0, "(expect 5)")
