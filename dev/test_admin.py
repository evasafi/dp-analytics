import re

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

pws = {u: input(f"{u} password (visible): ") for u in ("admin1", "analyst1", "field1")}


def csrf_from(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def spent():
    with engine.connect() as c:
        return c.execute(text("SELECT epsilon_spent FROM datasets WHERE id = 1")).scalar_one()


def client_for(user):
    cl = TestClient(app, base_url="http://127.0.0.1:8000")
    token = csrf_from(cl.get("/login").text)
    code = cl.post("/login", data={"username": user, "password": pws[user], "csrf": token},
                   follow_redirects=False).status_code
    print(f"login {user}:", code, "(expect 303)")
    return cl


admin, analyst, field = (client_for(u) for u in ("admin1", "analyst1", "field1"))
anon = TestClient(app, base_url="http://127.0.0.1:8000")

r = admin.get("/admin")
print("1. admin GET /admin:", r.status_code, "(expect 200)")
print("2. page shows budget and audit rows:", "Audit log" in r.text and "Privacy budget" in r.text, "(expect True)")
print("3. page shows no raw result values, only hash prefixes:",
      re.search(r"<td>[0-9a-f]{12}</td>", r.text) is not None, "(expect True)")

print("4a. analyst GET /admin:", analyst.get("/admin").status_code, "(expect 403)")
print("4b. field_agent GET /admin:", field.get("/admin").status_code, "(expect 403)")
print("4c. anonymous GET /admin:", anon.get("/admin").status_code, "(expect 401)")

before = spent()
atoken = csrf_from(admin.get("/admin").text)
print("5a. admin GET /analyst:", admin.get("/analyst").status_code, "(expect 403)")
for path, data in [("/engine/preview", {"measure": "count", "group_by": ["region"]}),
                   ("/engine/run", {"measure": "count", "group_by": ["region"]}),
                   ("/engine/run", {"measure": "mean", "group_by": ["region"]})]:
    print(f"5b. admin POST {path}:",
          admin.post(path, data={**data, "csrf": atoken}).status_code, "(expect 403)")
print("5c. admin may not submit field records:",
      admin.post("/field/submit", data={"region": "region_1", "household_size": 2,
                                        "monthly_transfer": "50", "csrf": atoken}).status_code,
      "(expect 403)")
print("6. budget unchanged by refused requests:", spent() == before, "(expect True)")
