import re

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

pw = input("analyst1 password (visible): ")


def spent():
    with engine.connect() as c:
        return c.execute(text("SELECT epsilon_spent FROM datasets WHERE id=1")).scalar_one()


cl = TestClient(app, base_url="http://127.0.0.1:8000")
tok = re.search(r'name="csrf" value="([^"]+)"', cl.get("/login").text).group(1)
print("login:", cl.post("/login", data={"username": "analyst1", "password": pw, "csrf": tok},
                          follow_redirects=False).status_code, "(expect 303)")
base = spent()
page = cl.get("/analyst")
tok = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
print("1. builder page:", page.status_code, "has group-by:", 'name="group_by"' in page.text, "(expect 200 True)")
print("2. css served:", TestClient(app).get("/static/style.css").status_code, "(expect 200)")
print("3. results list:", cl.get("/engine/results").status_code, "(expect 200)")
r = cl.post("/engine/preview", data={"measure": "count", "group_by": ["", ""], "csrf": tok})
print("4. preview with blank group-bys:", r.status_code, "(expect 200); spent unchanged:", spent() == base)
r = cl.post("/engine/preview", data={"measure": "count", "group_by": ["region", "region"], "csrf": tok})
print("5. same dimension twice:", r.status_code, "(expect 400); spent unchanged:", spent() == base)
r = cl.post("/engine/preview", data={"measure": "mean", "group_by": ["region", "household_size"],
                                      "f_region": ["region_8"], "csrf": tok})
print("6. mean, two group-bys, filter:", r.status_code, "(expect 200); cost shown:", "0.5" in r.text)
print("7. anonymous results list:", TestClient(app).get("/engine/results").status_code, "(expect 401)")
