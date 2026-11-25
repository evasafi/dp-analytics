"""The notification history page. Opening it marks everything read."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy import text

from app.auth import current_user
from app.db import engine
from app.web import render

router = APIRouter()


@router.get("/notifications")
def notifications_page(request: Request, user=Depends(current_user)):
    with engine.begin() as c:
        rows = c.execute(text(
            "SELECT id, message, link, created_at, read_at IS NULL AS unread FROM notifications "
            "WHERE user_id = :u ORDER BY id DESC LIMIT 100"), {"u": user.id}).all()
        c.execute(text("UPDATE notifications SET read_at = now() WHERE user_id = :u AND read_at IS NULL"), {"u": user.id})
    shown = [{"when": r.created_at.strftime("%Y-%m-%d %H:%M"), "message": r.message, "link": r.link, "unread": r.unread}
             for r in rows]
    return render(request, "notifications.html", user=user, rows=shown)
