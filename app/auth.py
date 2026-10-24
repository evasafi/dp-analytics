from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import text

from app.config import settings
from app.db import engine

COOKIE = "session"
MAX_AGE = 8 * 3600  # seconds

_ph = PasswordHasher()
_signer = URLSafeTimedSerializer(settings.secret_key, salt="session")
_DUMMY_HASH = _ph.hash("dummy-password-for-timing")


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _ph.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def authenticate(username: str, password: str):
    """Return the user id on success, None on failure."""
    with engine.connect() as c:
        row = c.execute(
            text("SELECT id, password_hash FROM users WHERE username = :u"),
            {"u": username},
        ).one_or_none()
    if row is None:
        verify_password(_DUMMY_HASH, password)  # equalise timing
        return None
    return row.id if verify_password(row.password_hash, password) else None


def make_session(user_id: int) -> str:
    return _signer.dumps({"uid": user_id})


def current_user(request: Request):
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="not signed in")
    try:
        data = _signer.loads(token, max_age=MAX_AGE)
    except BadSignature:  # also covers expired tokens
        raise HTTPException(status_code=401, detail="invalid or expired session")
    with engine.connect() as c:
        row = c.execute(
            text("SELECT id, username, role FROM users WHERE id = :i"),
            {"i": data["uid"]},
        ).one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail="unknown user")
    return row


def require_role(*roles: str):
    def dependency(user=Depends(current_user)):
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="not allowed for your role")
        return user
    return dependency
