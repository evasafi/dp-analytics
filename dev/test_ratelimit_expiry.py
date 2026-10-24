import re

from fastapi.testclient import TestClient

from app.main import app

cl = TestClient(app, base_url="http://127.0.0.1:8000")
token = re.search(r'name="csrf" value="([^"]+)"', cl.get("/login").text).group(1)
r = cl.post("/login", data={"username": "ratelimit_test", "password": "correct-horse-battery",
                            "csrf": token}, follow_redirects=False)
print("after the window, correct password:", r.status_code, "(expect 303)")
