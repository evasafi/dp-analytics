from sqlalchemy import text

from app.db import engine

with engine.connect() as c:
    for r in c.execute(text(
            "SELECT id, username, ts, round(extract(epoch FROM now() - ts)::numeric) AS age_s "
            "FROM login_failures WHERE username = 'ratelimit_test' ORDER BY id")):
        print(tuple(r))
