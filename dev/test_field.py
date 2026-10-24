import re

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

field_pw = input("field1 password (visible): ")
analyst_pw = input("analyst1 password (visible): ")


def csrf_from(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def one(sql):
    with engine.connect() as c:
        return c.execute(text(sql)).scalar_one()


def rows():
    return one("SELECT count(*) FROM beneficiaries WHERE dataset_id = 1")


def submits():
    return one("SELECT count(*) FROM audit_log WHERE query_type = 'submit'")


def login(client, user, pw):
    token = csrf_from(client.get("/login").text)
    return client.post("/login", data={"username": user, "password": pw, "csrf": token},
                       follow_redirects=False).status_code


field = TestClient(app, base_url="http://127.0.0.1:8000")
analyst = TestClient(app, base_url="http://127.0.0.1:8000")
anon = TestClient(app, base_url="http://127.0.0.1:8000")

print("login field1:", login(field, "field1", field_pw), "(expect 303)")
print("login analyst1:", login(analyst, "analyst1", analyst_pw), "(expect 303)")

token = csrf_from(field.get("/field").text)
good = {"region": "region_8", "household_size": 3, "monthly_transfer": "120.50"}
n0, a0 = rows(), submits()

r = field.post("/field/submit", data={**good, "csrf": token}, follow_redirects=False)
print("1. valid submit:", r.status_code, r.headers.get("location"), "(expect 303 /field?done=1)")
print("2. exactly one row and one audit row added:", rows() - n0 == 1, submits() - a0 == 1, "(expect True True)")

bad = [("region_9", 3, "100"), ("region_1", 13, "100"), ("region_1", 0, "100"),
       ("region_1", 3, "501"), ("region_1", 3, "-1"), ("region_1", 3, "abc"),
       ("region_1", 3, "NaN"), ("region_1", 3, "1e999")]
codes = [field.post("/field/submit",
                    data={"region": a, "household_size": b, "monthly_transfer": c, "csrf": token}
                    ).status_code for a, b, c in bad]
print("3. invalid submissions:", codes, "(expect all 400)")
print("4. nothing saved by invalid ones:", rows() - n0 == 1, submits() - a0 == 1, "(expect True True)")

r = field.post("/field/submit", data={**good, "csrf": "forged"})
print("5. forged CSRF:", r.status_code, "(expect 403)")

atoken = csrf_from(analyst.get("/analyst").text)
print("6a. analyst GET /field:", analyst.get("/field").status_code, "(expect 403)")
r = analyst.post("/field/submit", data={**good, "csrf": atoken})
print("6b. analyst POST /field/submit:", r.status_code, "(expect 403)")

print("7a. anonymous GET /field:", anon.get("/field").status_code, "(expect 401)")
print("7b. anonymous POST /field/submit:",
      anon.post("/field/submit", data={**good, "csrf": "x"}).status_code, "(expect 401 or 403)")

print("8. no extra rows after all refused requests:", rows() - n0 == 1, "(expect True)")
