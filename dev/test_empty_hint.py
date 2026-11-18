"""The board says why it is empty and how to get back: no matches for the filters, no needs at all, or a page past the end.

Run from the project folder:  python -m dev.test_empty_hint
Only reads the database (to find filter combinations that match nothing); it writes nothing.
If a request comes back 429, wait a minute and run it again.
"""
import re
import sys

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app
from app.routes.board import PAGE_SIZE, PROVINCE_NAMES, PURPOSE_LABELS

fails = []
count = 0


def check(name, ok, extra=""):
    global count
    count += 1
    if not ok:
        fails.append(name)
        print("FAIL", name, extra)


def get(path):
    return TestClient(app).get(path)


with engine.connect() as conn:
    rows = conn.execute(text("SELECT purpose, province FROM public_needs")).all()
used_pairs = {(r.purpose, r.province) for r in rows}
used_purposes = {r.purpose for r in rows}
used_provinces = {r.province for r in rows}

base = get("/board")
check("the unfiltered board loads", base.status_code == 200, base.status_code)
m = re.search(r"<h3>(\d+) open need", base.text)
total = int(m.group(1)) if m else -1
check("the board shows its total", m is not None)
check("the old wording is gone", "No needs match." not in base.text)

pair = next(((p, q) for p in sorted(PURPOSE_LABELS) for q in sorted(PROVINCE_NAMES) if (p, q) not in used_pairs), None)
if pair is None:
    print("SKIP both filters: every purpose and province combination has a need")
else:
    p, q = pair
    r = get(f"/board?purpose={p}&province={q}")
    check("both filters, no match: loads", r.status_code == 200, r.status_code)
    check("both filters, no match: says so", "No open needs match these filters." in r.text)
    check("both filters, no match: offers all provinces for this purpose", f'href="/board?purpose={p}"' in r.text)
    check("both filters, no match: offers all purposes for this province", 'href="/board?province=' in r.text and "all purposes" in r.text)
    check("both filters, no match: offers to clear both", 'href="/board"' in r.text and "clear both" in r.text)

q_only = next((q for q in sorted(PROVINCE_NAMES) if q not in used_provinces), None)
if q_only is None:
    print("SKIP province only: every province has a need")
else:
    r = get(f"/board?province={q_only}")
    check("province only, no match: says so and links to the full board",
          "No open needs match these filters." in r.text and 'href="/board"' in r.text and "clear both" not in r.text)

p_only = next((p for p in sorted(PURPOSE_LABELS) if p not in used_purposes), None)
if p_only is None:
    print("SKIP purpose only: every purpose has a need")
else:
    r = get(f"/board?purpose={p_only}")
    check("purpose only, no match: says so and links to the full board",
          "No open needs match these filters." in r.text and 'href="/board"' in r.text and "clear both" not in r.text)

if total > 0:
    check("a board with needs does not show an empty message",
          "No open needs match" not in base.text and "There are no open needs" not in base.text and "nothing on this page" not in base.text)
    far = total // PAGE_SIZE + 2
    if far <= 1000:
        r = get(f"/board?page={far}")
        check("a page past the end: loads", r.status_code == 200, r.status_code)
        check("a page past the end: says so and links back", "There is nothing on this page." in r.text and 'href="/board"' in r.text)
        check("a page past the end: does not claim nothing matches", "No open needs match" not in r.text)
else:
    print("NOTE: the board has no open needs, so the past-the-end and normal-board checks were skipped")

if fails:
    print(f"{len(fails)} of {count} checks FAILED")
    sys.exit(1)
print(f"empty hint: all {count} checks passed")
