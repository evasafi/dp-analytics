"""Account deletion for requesters and supporters. The work is done by delete_account() in the database."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import text

from app.auth import COOKIE, require_role, verify_password
from app.db import engine
from app.ratelimit import is_locked, record_failure
from app.web import check_csrf, client_ip, render

router = APIRouter()

BLOCKERS = {
    "pending_needs": ("Requests waiting for review", "/needs/mine"),
    "published_needs": ("Requests that are public on the board", "/needs/mine"),
    "unanswered_pledges_on_mine": ("Money pledges on your requests that still need an answer", "/needs/mine"),
    "unanswered_offers_on_mine": ("Item offers on your requests that still need an answer", "/needs/mine"),
    "my_open_pledges": ("Your money pledges that are still open", "/support"),
    "my_open_offers": ("Your item offers that are still open", "/support"),
}
ROLES = ("requester", "supporter")


def _blockers(uid):
    with engine.connect() as c:
        rows = c.execute(text("SELECT blocker, cnt FROM account_blockers(:u) WHERE cnt > 0"), {"u": uid}).all()
    return [{"label": BLOCKERS[r.blocker][0], "count": r.cnt, "link": BLOCKERS[r.blocker][1]} for r in rows]


def _page(request, user, status_code=200, error=None):
    return render(request, "account_delete.html", status_code=status_code, user=user,
                  blockers=_blockers(user.id), error=error)


@router.get("/account/delete")
def delete_page(request: Request, user=Depends(require_role(*ROLES))):
    return _page(request, user)


@router.post("/account/delete")
def delete_submit(request: Request, password: str = Form(...), confirm: str = Form(""), csrf: str = Form(...),
                  user=Depends(require_role(*ROLES))):
    check_csrf(request, csrf)
    ip = client_ip(request)
    if is_locked(user.username, ip):
        return _page(request, user, 429, "Too many wrong passwords. Try again in a few minutes. Nothing was deleted.")
    with engine.connect() as c:
        stored = c.execute(text("SELECT password_hash FROM users WHERE id = :i"), {"i": user.id}).scalar_one()
    if not verify_password(stored, password):
        record_failure(user.username, ip)
        return _page(request, user, 400, "That password is not right. Nothing was deleted.")
    if not confirm:
        return _page(request, user, 400, "Tick the box to confirm. Nothing was deleted.")
    with engine.begin() as c:
        done = c.execute(text("SELECT delete_account(:u)"), {"u": user.id}).scalar_one()
    if not done:
        return _page(request, user, 409, "Something on your account is still open, so nothing was deleted. Finish the items below first.")
    resp = render(request, "account_deleted.html")
    resp.delete_cookie(COOKIE)
    return resp
