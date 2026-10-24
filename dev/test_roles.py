import re

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

pw = input("field1 password (visible): ")


def csrf_from(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def spent():
    with engine.connect() as c:
        return c.execute(text("SELECT epsilon_spent FROM datasets WHERE id = 1")).scalar_one()


client = TestClient(app, base_url="http://127.0.0.1:8000")

# Test 1: wrong CSRF token on login is rejected
page = client.get("/login")
r = client.post("/login", data={"username": "field1", "password": pw, "csrf": "forged"},
                follow_redirects=False)
print("1. login with forged CSRF:", r.status_code, "(expect 403)")

# Test 2: field1 can sign in with the right token
token = csrf_from(client.get("/login").text)
r = client.post("/login", data={"username": "field1", "password": pw, "csrf": token},
                follow_redirects=False)
print("2. field1 login:", r.status_code, "(expect 303)")

# Test 3: field_agent is refused on the analyst page and every query route
before = spent()
r = client.get("/analyst")
print("3a. GET /analyst as field_agent:", r.status_code, "(expect 403)")
for path, data in [("/query/count", {"region": "region_8"}),
                   ("/query/mean", {"region": "region_8"}),
                   ("/query/histogram", {})]:
    r = client.post(path, data={**data, "csrf": token})
    print(f"3b. POST {path} as field_agent:", r.status_code, "(expect 403)")
print("3c. budget unchanged by refused requests:", spent() == before, "(expect True)")

# Test 4: signed in as field1, a forged CSRF token is still rejected on logout
r = client.post("/logout", data={"csrf": "forged"}, follow_redirects=False)
print("4. logout with forged CSRF:", r.status_code, "(expect 403)")
