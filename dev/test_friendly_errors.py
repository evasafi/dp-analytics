"""Friendly error pages for browsers, with the same status codes and JSON for everything else.

Run from the project root as a module: python -m dev.test_friendly_errors
It needs no password and creates no data: every request is anonymous, so nothing is written.
"""
from fastapi.testclient import TestClient

from app.main import app

HTML = {"accept": "text/html,application/xhtml+xml"}
cl = TestClient(app, base_url="http://127.0.0.1:8000")
fails, total = [], 0


def check(label, ok):
    global total
    total += 1
    print(("OK   " if ok else "FAIL ") + label)
    if not ok:
        fails.append(label)


PAGES = ["/support/pledge/1", "/needs/mine", "/needs/new", "/verify", "/admin", "/analyst", "/engine/results", "/support"]
for p in PAGES:
    r = cl.get(p, headers=HTML)
    check(f"a signed-out browser asking for {p} gets a readable page, status 401",
          r.status_code == 401 and "Please sign in" in r.text and 'href="/login"' in r.text and 'href="/register"' in r.text
          and '"detail"' not in r.text and "text/html" in r.headers["content-type"])

r = cl.get("/support/pledge/1", headers=HTML)
check("the page offers a way back to the board", 'href="/board"' in r.text)
check("the readable page sets no cookies, so public and signed-out pages keep setting none", "set-cookie" not in r.headers)

codes = {p: cl.get(p).status_code for p in PAGES}
bodies = {p: cl.get(p).json() for p in PAGES}
check("without a browser's Accept header the status codes are unchanged (401) " + str(sorted(set(codes.values()))),
      all(c == 401 for c in codes.values()))
check('and the body is still the same JSON: {"detail": "not signed in"}', all(b == {"detail": "not signed in"} for b in bodies.values()))

r = cl.get("/board/99999999", headers=HTML)
check("a public page that does not exist gives a readable 404 and sets no cookie",
      r.status_code == 404 and "Page not found" in r.text and "set-cookie" not in r.headers and '"detail"' not in r.text)
r = cl.get("/board/99999999")
check('without Accept it is still 404 with {"detail": "not found"}', r.status_code == 404 and r.json() == {"detail": "not found"})
check("a malformed id is still refused with 422 (that path was not changed)", cl.get("/board/abc", headers=HTML).status_code == 422)

r = cl.post("/login", data={"username": "nobody", "password": "x" * 12, "csrf": "forged"}, headers=HTML)
check("a forged form token gives a readable page that says the form expired, status 403",
      r.status_code == 403 and "This form expired" in r.text and "different kind of account" not in r.text and '"detail"' not in r.text)
r = cl.post("/login", data={"username": "nobody", "password": "x" * 12, "csrf": "forged"})
check('without Accept it is still 403 with {"detail": "bad CSRF token"}', r.status_code == 403 and r.json() == {"detail": "bad CSRF token"})

check("an error that has no readable page (a bad method) is left alone: 405", cl.delete("/board", headers=HTML).status_code == 405)

assert not fails, f"failed: {fails}"
print(f"PASS: {total} checks")
