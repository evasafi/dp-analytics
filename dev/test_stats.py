"""Front-page stats: the public_stats view matches the tables, the board shows it, and it is read-only.

Run from the project folder:  python -m dev.test_stats
Only reads the database; it writes nothing (the one write attempt is refused and rolled back).
"""
import sys

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import engine
from app.main import app
from app.reference import money

fails = []
count = 0


def check(name, ok, extra=""):
    global count
    count += 1
    if not ok:
        fails.append(name)
        print("FAIL", name, extra)


with engine.connect() as c:
    rows = c.execute(text("SELECT * FROM public_stats")).mappings().all()
    check("the view returns exactly one row", len(rows) == 1, len(rows))
    row = rows[0]
    check("the view has only the four total columns",
          set(row.keys()) == {"open_needs", "fulfilled_needs", "confirmed_try", "donors"}, sorted(row.keys()))
    want_open = c.execute(text("SELECT count(*) FROM needs WHERE status = 'published'")).scalar_one()
    want_done = c.execute(text("SELECT count(*) FROM needs WHERE status = 'fulfilled'")).scalar_one()
    want_try = c.execute(text("SELECT COALESCE(sum(amount_try), 0) FROM pledges WHERE status = 'confirmed'")).scalar_one()
    want_donors = c.execute(text("SELECT count(DISTINCT supporter_id) FROM pledges WHERE status = 'confirmed'")).scalar_one()

check("open needs match the table", row["open_needs"] == want_open, (row["open_needs"], want_open))
check("fulfilled needs match the table", row["fulfilled_needs"] == want_done, (row["fulfilled_needs"], want_done))
check("confirmed lira matches the table", row["confirmed_try"] == want_try, (row["confirmed_try"], want_try))
check("donors match the table", row["donors"] == want_donors, (row["donors"], want_donors))

r = TestClient(app).get("/board")
check("board loads", r.status_code == 200, r.status_code)
check("board shows the fulfilled count", f"<strong>{row['fulfilled_needs']}</strong>" in r.text)
check("board shows the confirmed amount", money(row["confirmed_try"], "TRY") in r.text)
check("board shows the donor count", f"<strong>{row['donors']}</strong>" in r.text)
check("board says the figures are demo data", "demo data" in r.text)
check("board still sets no cookie", "set-cookie" not in r.headers)

refused = False
try:
    with engine.begin() as c:
        c.execute(text("INSERT INTO public_stats (open_needs) VALUES (1)"))
except DBAPIError:
    refused = True
check("the app role cannot write to the view", refused)

if fails:
    print(f"{len(fails)} of {count} checks FAILED")
    sys.exit(1)
print(f"stats: all {count} checks passed")
