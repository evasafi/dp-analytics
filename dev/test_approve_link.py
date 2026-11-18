"""After approving, the verifier's queue links to the need's public page; other outcomes do not.

Run from the project folder:  python -m dev.test_approve_link
Only reads the database (it picks an existing verifier and a published need); it writes nothing.
The redirect itself is checked in the source here and end to end by dev.test_verifiers.
"""
import pathlib
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


def queue(query, uid):
    return TestClient(app).get("/verify" + query, headers={"Cookie": f"{COOKIE}={make_session(uid)}"})


with engine.connect() as conn:
    vf = conn.execute(text("SELECT id FROM users WHERE role = 'verifier' ORDER BY id LIMIT 1")).scalar()
    nid = conn.execute(text("SELECT id FROM public_needs ORDER BY id LIMIT 1")).scalar()
if vf is None or nid is None:
    sys.exit("Needs at least one verifier and one published need in the database.")

LINK = "View it on the board"
SOURCE = (pathlib.Path(__file__).resolve().parent.parent / "app" / "routes" / "verify.py").read_text(encoding="utf-8")

check("approving redirects with the need id", 'RedirectResponse(f"/verify?done=approved&need={nid}", status_code=303)' in SOURCE)

r = queue(f"?done=approved&need={nid}", vf)
check("approved with a need: loads", r.status_code == 200, r.status_code)
check("approved with a need: says approved and published", "Approved and published." in r.text)
check("approved with a need: links to its public page", f'href="/board/{nid}"' in r.text and LINK in r.text)

r = queue("?done=approved", vf)
check("approved without a need id: still says approved, no link", "Approved and published." in r.text and LINK not in r.text)

r = queue("?done=approved&need=0", vf)
check("approved with need 0: no link", LINK not in r.text)

r = queue("?done=approved&need=-5", vf)
check("approved with a negative need id: no link", r.status_code == 200 and LINK not in r.text, r.status_code)

r = queue(f"?done=rejected&need={nid}", vf)
check("rejected: says so and shows no public link", "Rejected." in r.text and LINK not in r.text)

r = queue(f"?done=resolved&need={nid}", vf)
check("settled dispute: says so and shows no public link", "Dispute settled." in r.text and LINK not in r.text)

r = queue("", vf)
check("the plain queue shows no link", r.status_code == 200 and LINK not in r.text, r.status_code)

if fails:
    print(f"{len(fails)} of {count} checks FAILED")
    sys.exit(1)
print(f"approve link: all {count} checks passed")
