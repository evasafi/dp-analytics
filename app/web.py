"""Shared web helpers: templates, CSRF tokens and page rendering.

Kept out of app/main.py so route modules can use them without importing main, which would be circular.
"""
import secrets
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import settings

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


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
