import secrets
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

from app.auth import COOKIE, MAX_AGE, authenticate, current_user, make_session
from app.config import settings

app = FastAPI()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

CSRF_COOKIE = "csrf"
_csrf_signer = URLSafeSerializer(settings.secret_key, salt="csrf")


def _read_csrf(request: Request):
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie:
        return None
    try:
        return _csrf_signer.loads(cookie)
    except BadSignature:
        return None


def render(request: Request, name: str, status_code: int = 200, **ctx):
    raw = _read_csrf(request)
    new_cookie = None
    if raw is None:
        raw = secrets.token_urlsafe(24)
        new_cookie = _csrf_signer.dumps(raw)
    resp = templates.TemplateResponse(
        request, name, {"csrf": raw, **ctx}, status_code=status_code
    )
    if new_cookie:
        resp.set_cookie(CSRF_COOKIE, new_cookie, httponly=True, samesite="strict")
    return resp


def check_csrf(request: Request, form_token: str):
    raw = _read_csrf(request)
    if raw is None or not secrets.compare_digest(raw, form_token):
        raise HTTPException(status_code=403, detail="bad CSRF token")


@app.get("/")
def root(request: Request):
    try:
        current_user(request)
    except HTTPException:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/me", status_code=303)


@app.get("/login")
def login_page(request: Request):
    return render(request, "login.html")


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          csrf: str = Form(...)):
    check_csrf(request, csrf)
    user_id = authenticate(username, password)
    if user_id is None:
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
