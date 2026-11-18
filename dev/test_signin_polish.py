"""Sign-in prompts on the board and need pages follow who is looking: visitors, supporters, other roles.

Run from the project folder:  python -m dev.test_signin_polish
Only reads the database (it picks an existing supporter, requester and published need); it writes nothing.
If a request comes back 429, wait a minute and run it again.
"""
import sys

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.auth import COOKIE, make_session
from app.db import engine
from app.main import app

fails = []
count = 0


def check(name, ok, extra=""):
    global count
    count += 1
    if not ok:
        fails.append(name)
        print("FAIL", name, extra)


def get(path, uid=None):
    c = TestClient(app)
    headers = {"Cookie": f"{COOKIE}={make_session(uid)}"} if uid else {}
    return c.get(path, headers=headers)


with engine.connect() as conn:
    sup = conn.execute(text("SELECT id FROM users WHERE role = 'supporter' ORDER BY id LIMIT 1")).scalar()
    req = conn.execute(text("SELECT id FROM users WHERE role = 'requester' ORDER BY id LIMIT 1")).scalar()
    nid = conn.execute(text("SELECT id FROM public_needs ORDER BY id LIMIT 1")).scalar()
if sup is None or req is None or nid is None:
    sys.exit("Needs at least one supporter, one requester and one published need in the database.")

PLEDGE = f'href="/support/pledge/{nid}"'
NOTE = "create a supporter account"

r = get("/board")
check("board, visitor: loads", r.status_code == 200, r.status_code)
check("board, visitor: told to create a supporter account", NOTE in r.text)

r = get("/board", sup)
check("board, supporter: loads", r.status_code == 200, r.status_code)
check("board, supporter: not told to create an account", NOTE not in r.text)
check("board, supporter: told they can pledge", "You can pledge to any need below." in r.text)

r = get("/board", req)
check("board, requester: loads", r.status_code == 200, r.status_code)
check("board, requester: not told to create a supporter account", NOTE not in r.text)
check("board, requester: told pledges come from supporter accounts", "Pledges are made from supporter accounts." in r.text)

r = get(f"/board/{nid}")
check("need, visitor: loads", r.status_code == 200, r.status_code)
check("need, visitor: sign-in and register links", 'href="/login"' in r.text and 'href="/register"' in r.text)
check("need, visitor: no pledge link", PLEDGE not in r.text)
check("need, visitor: no signed-in line", "Signed in as" not in r.text)

r = get(f"/board/{nid}", sup)
check("need, supporter: loads", r.status_code == 200, r.status_code)
check("need, supporter: signed-in line with my page link", "Signed in as" in r.text and 'href="/me"' in r.text)
check("need, supporter: pledge link", PLEDGE in r.text)
check("need, supporter: no sign-in or register links", 'href="/login"' not in r.text and 'href="/register"' not in r.text)

r = get(f"/board/{nid}", req)
check("need, requester: loads", r.status_code == 200, r.status_code)
check("need, requester: signed-in line", "Signed in as" in r.text)
check("need, requester: no pledge link", PLEDGE not in r.text)
check("need, requester: told pledges come from supporter accounts", "Pledges are made from supporter accounts." in r.text)
check("need, requester: no sign-in or register links", 'href="/login"' not in r.text and 'href="/register"' not in r.text)

if fails:
    print(f"{len(fails)} of {count} checks FAILED")
    sys.exit(1)
print(f"signin polish: all {count} checks passed")
