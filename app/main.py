from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.auth import COOKIE, MAX_AGE, authenticate, current_user, make_session
from app.ratelimit import is_locked, record_failure

app = FastAPI()
from app.web import check_csrf, render


@app.get("/")
def root(request: Request):
    try:
        user = current_user(request)
    except HTTPException:
        return RedirectResponse("/login", status_code=303)
    home = {"analyst": "/analyst", "admin": "/admin", "requester": "/needs/mine", "verifier": "/verify", "supporter": "/support"}
    return RedirectResponse(home.get(user.role, "/me"), status_code=303)


@app.get("/login")
def login_page(request: Request):
    return render(request, "login.html")


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          csrf: str = Form(...)):
    check_csrf(request, csrf)
    ip = request.client.host if request.client else "unknown"
    if is_locked(username, ip):
        return render(request, "login.html", status_code=429,
                      error="Too many failed attempts. Try again in a few minutes.")
    user_id = authenticate(username, password)
    if user_id is None:
        record_failure(username, ip)
        return render(request, "login.html", status_code=401,
                      error="Invalid username or password.")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, make_session(user_id), max_age=MAX_AGE,
                    httponly=True, samesite="strict")
    return resp


@app.post("/logout")
def logout(request: Request, csrf: str = Form(...)):
    check_csrf(request, csrf)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/me")
def me(request: Request):
    user = current_user(request)
    return render(request, "me.html", user=user)


# ---------------- analyst routes ----------------
from fastapi import Depends
from sqlalchemy import text

from app.auth import require_role
from app.db import engine
from app.routes.analytics import budgets as _budgets



from sqlalchemy.exc import IntegrityError

# ---------------- admin routes ----------------
@app.get("/admin")
def admin_page(request: Request, user=Depends(require_role("admin"))):
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT a.ts, u.username, a.query_type, a.params, a.epsilon_charged, "
            "a.result_hash FROM audit_log a JOIN users u ON u.id = a.user_id "
            "ORDER BY a.id DESC LIMIT 50")).all()
    return render(request, "admin.html", user=user, budgets=_budgets(), rows=rows)



# ---------------- engine results ----------------
import csv
import io

from fastapi.responses import Response

from app.engine_noise import SUPPRESS_BELOW

def _load_result(rid: int, user):
    with engine.connect() as c:
        row = c.execute(
            text("SELECT r.id, r.spec, r.cells, a.query_type, d.name AS dataset FROM results r JOIN audit_log a ON a.id = r.audit_id "
                 "JOIN datasets d ON d.id = r.dataset_id WHERE r.id = :r AND r.user_id = :u"),
            {"r": rid, "u": user.id}).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return row


def _show(v):
    if v is None:
        return "suppressed"
    return v if isinstance(v, int) else round(v, 1)


@app.get("/engine/result/{rid}")
def engine_result(request: Request, rid: int, user=Depends(require_role("analyst"))):
    row = _load_result(rid, user)
    rows = [[*k, _show(v)] for k, v in row.cells]
    return render(request, "engine_result.html", user=user, rid=rid, spec=row.spec,
                  dims=row.spec["group_by"], rows=rows, suppress_below=SUPPRESS_BELOW,
                  is_needs=(row.query_type == "needs_count"), dataset=row.dataset)


@app.get("/engine/result/{rid}/csv")
def engine_result_csv(rid: int, user=Depends(require_role("analyst"))):
    row = _load_result(rid, user)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(list(row.spec["group_by"]) + ["value"])
    for k, v in row.cells:
        w.writerow(list(k) + [_show(v)])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="result_{rid}.csv"'})

# ---------------- builder ----------------
from fastapi.staticfiles import StaticFiles


@app.get("/engine/results")
def engine_results(request: Request, user=Depends(require_role("analyst"))):
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT r.id, r.ts, r.spec, a.epsilon_charged, a.query_type FROM results r "
            "JOIN audit_log a ON a.id = r.audit_id WHERE r.user_id = :u "
            "ORDER BY r.id DESC LIMIT 50"), {"u": user.id}).all()
    return render(request, "engine_list.html", user=user, rows=rows)


app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

# ---------------- requesters and supporters ----------------
import re as _re

from app.auth import hash_password
from app.ratelimit import record_registration, registration_blocked

SELF_ROLES = ("requester", "supporter")
USERNAME_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,29}$")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@app.get("/register")
def register_page(request: Request):
    return render(request, "register.html")


@app.post("/register")
def register(request: Request, username: str = Form(...), password: str = Form(...),
             role: str = Form(...), csrf: str = Form(...)):
    check_csrf(request, csrf)
    if registration_blocked(_client_ip(request)):
        return render(request, "register.html", status_code=429,
                      error="Too many accounts created from this address. Try again later.")
    if role not in SELF_ROLES or not USERNAME_RE.match(username) or not 12 <= len(password) <= 200:
        return render(request, "register.html", status_code=400,
                      error="Check the username (3 to 30 characters) and the password (12 or more).")
    try:
        with engine.begin() as conn:
            uid = conn.execute(
                text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r) RETURNING id"),
                {"u": username, "h": hash_password(password), "r": role}).scalar_one()
    except IntegrityError:
        return render(request, "register.html", status_code=400,
                      error="That username is not available.")
    record_registration(_client_ip(request))
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, make_session(uid), max_age=MAX_AGE, httponly=True, samesite="strict")
    return resp


from app.routes import needs as needs_routes

app.include_router(needs_routes.router)


from app.routes import verify as verify_routes

app.include_router(verify_routes.router)
from app.routes import board as board_routes

app.include_router(board_routes.router)
from app.routes import support as support_routes

app.include_router(support_routes.router)
from app.routes import analytics as analytics_routes

app.include_router(analytics_routes.router)


# ---------------- friendly error pages for browsers ----------------
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.web import templates as _error_templates

_ERROR_PAGES = {
    401: ("Please sign in", "This page is only for signed-in people. Sign in, or create an account, and then try again.",
          [("Sign in", "/login"), ("Create an account", "/register"), ("Back to the board", "/board")]),
    403: ("This page is for a different kind of account", "You are signed in, but this page belongs to another role. Requesters, supporters and verifiers each have their own pages.",
          [("Go to my page", "/"), ("Back to the board", "/board")]),
    404: ("Page not found", "We could not find that page. It may not exist, or it may not be public.",
          [("Back to the board", "/board")]),
}
_FORM_EXPIRED = ("This form expired", "The page you submitted from is out of date. Go back, reload it and try again.",
                 [("Back to the board", "/board")])


@app.exception_handler(StarletteHTTPException)
async def friendly_errors(request: Request, exc: StarletteHTTPException):
    """Browsers get a readable page. Anything else (tests, scripts) still gets the same JSON and status code as before.
    The page is rendered without a CSRF cookie, so public pages keep setting no cookies."""
    if "text/html" in request.headers.get("accept", "") and exc.status_code in _ERROR_PAGES:
        heading, message, links = _FORM_EXPIRED if exc.detail == "bad CSRF token" else _ERROR_PAGES[exc.status_code]
        return _error_templates.TemplateResponse(request, "error.html", {"heading": heading, "message": message, "links": links},
                                                 status_code=exc.status_code)
    return await http_exception_handler(request, exc)
