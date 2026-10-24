from sqlalchemy import text

from app.db import engine

with engine.connect() as c:
    print(c.execute(text(
        "SELECT count(*), round(avg(monthly_transfer), 1) "
        "FROM beneficiaries WHERE region = 'region_8'")).one())
