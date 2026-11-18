"""Admin audit log: query parameters read as a sentence, not as a Python dict.

Run from the project folder:  python -m dev.test_admin_params
Only reads the database (it picks an existing admin); it writes nothing.
"""
import sys

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.auth import COOKIE, make_session
from app.db import engine
from app.main import _params_text, app

fails = []
count = 0


def check(name, ok, extra=""):
    global count
    count += 1
    if not ok:
        fails.append(name)
        print("FAIL", name, extra)


real = {"dataset": "needs_synthetic", "filters": [], "measure": "count", "group_by": ["need_type"]}
out = _params_text(real)
check("the real audit-log shape reads as a sentence", out == "dataset needs_synthetic; measure count; grouped by need_type; no filters", out)
check("no braces, brackets or quotes are left", not any(ch in out for ch in "{}[]'"))

out = _params_text({"dataset": "d", "measure": "count", "group_by": [], "filters": []})
check("no grouping and no filters say so", out == "dataset d; measure count; no grouping; no filters", out)

out = _params_text({"dataset": "d", "measure": "count", "group_by": ["a", "b"], "filters": []})
check("several grouping columns are listed", "grouped by a, b" in out, out)

out = _params_text({"dataset": "d", "measure": "count", "group_by": [], "filters": [{"column": "region", "op": "=", "value": "Marmara"}]})
check("a column, operator and value filter reads as a comparison", "filters: region = Marmara" in out, out)

out = _params_text({"dataset": "d", "measure": "count", "group_by": [], "filters": [{"field": "region", "equals": "Marmara"}]})
check("an unfamiliar filter shape is still shown", "region" in out and "Marmara" in out and "{" not in out, out)

out = _params_text({"dataset": "d", "measure": "count", "group_by": [], "filters": [], "bins": 5})
check("an unknown extra key is shown as key: value", out.endswith("bins: 5"), out)

check("a non-dict value is shown as text", _params_text("odd") == "odd")

with engine.connect() as conn:
    ad = conn.execute(text("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")).scalar()
    logged = conn.execute(text("SELECT count(*) FROM audit_log")).scalar()
if ad is None:
    print("SKIP admin page: no admin user in the database")
else:
    r = TestClient(app).get("/admin", headers={"Cookie": f"{COOKIE}={make_session(ad)}"})
    check("the admin page loads", r.status_code == 200, r.status_code)
    check("the audit log columns are still there", "Parameters" in r.text and "Epsilon" in r.text and "Result hash" in r.text)
    if logged:
        check("the page shows readable parameters, not a dict", "dataset " in r.text and "{&#39;" not in r.text and "{'" not in r.text)
    else:
        print("NOTE: the audit log is empty, so the rendered-parameters check was skipped")

if fails:
    print(f"{len(fails)} of {count} checks FAILED")
    sys.exit(1)
print(f"admin params: all {count} checks passed")
