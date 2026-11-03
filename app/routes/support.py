"""Supporter pages: pledge part of a need's goal, see my pledges with the account to pay into, mark a pledge as sent,
cancel one not yet sent.

The pledge trigger in schema.sql enforces the floor, the goal cap and the limit on unconfirmed pledges under locks;
the checks here only give readable messages. The account holder and IBAN are shown only to the supporter who
pledged, and only while the pledge is open (pledged, sent or disputed).
"""
import math
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.auth import require_role
from app.db import engine
from app.reference import STATUS_TEXT, money, need_label
from app.routes.board import COLUMNS, _card, _expire
from app.validation import contact_problem
from app.web import check_csrf, render

router = APIRouter()
_WHOLE = re.compile(r"[0-9]{1,9}")
PLEDGE_TEXT = {"pledged": "Pledged. Send the money, then mark it as sent", "sent": "Sent. Waiting for the requester to confirm",
               "confirmed": "Confirmed by the requester", "disputed": "Disputed. A verifier is looking at it",
               "expired": "Expired. It was not marked as sent in time", "cancelled": "Cancelled"}
SHOW_ACCOUNT = ("pledged", "sent", "disputed")


def _settings():
    with engine.connect() as c:
        return c.execute(text("SELECT eur_try_rate, pledge_floor, pledge_expiry_days, max_unconfirmed_pledges, "
                              "suggestion_min_needs FROM settings")).one()


def _open_need(nid: int):
    with engine.connect() as c:
        return c.execute(text(f"SELECT {COLUMNS}, band FROM public_needs WHERE id = :i"), {"i": nid}).one_or_none()


def _suggestion(n, s, open_try: int):
    """The open amount divided by the average number of donors of finished lira needs in the same goal band, rounded
    up to 10 lira, between the floor and the open amount. None for euro needs and for bands with too few finished needs."""
    if n.band is None or open_try < s.pledge_floor:
        return None
    with engine.connect() as c:
        st = c.execute(text("SELECT finished_needs, avg_donors FROM suggestion_stats WHERE band = :b"), {"b": n.band}).one_or_none()
    if st is None or st.finished_needs < s.suggestion_min_needs or not st.avg_donors:
        return None
    amount = min(max(math.ceil(open_try / st.avg_donors / 10) * 10, s.pledge_floor), open_try)
    return {"amount": amount, "avg_donors": f"{st.avg_donors:.1f}", "finished": st.finished_needs}


def _open_try(n, s) -> int:
    """Lira still open for pledges. Exact for lira needs, an estimate at the placeholder rate for euro needs."""
    left = max(0, n.goal - n.promised)
    return int(left * s.eur_try_rate) if n.currency == "EUR" else int(left)


def _pledge_page(request, user, n, s, status_code=200, error=None, form=None):
    open_try = _open_try(n, s)
    return render(request, "pledge_form.html", status_code=status_code, user=user, c=_card(n), open_try=open_try,
                  suggestion=_suggestion(n, s, open_try),
                  floor=s.pledge_floor, days=s.pledge_expiry_days, rate=f"{s.eur_try_rate:.0f}", error=error, form=form or {})


def _support_page(request, user, status_code=200, done="", error=None):
    _expire()
    s = _settings()
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT p.id, p.status, p.amount_try, p.amount_need, p.created_at, p.expires_at, n.id AS nid, n.purpose, "
            "n.medical_location, n.province, n.district, n.currency, n.status AS nstatus, n.payee_name, n.payee_details "
            "FROM pledges p JOIN needs n ON n.id = p.need_id WHERE p.supporter_id = :u ORDER BY p.id DESC"), {"u": user.id}).all()
    shown = []
    for r in rows:
        account = r.status in SHOW_ACCOUNT
        shown.append({"id": r.id, "nid": r.nid, "created": r.created_at.strftime("%Y-%m-%d"),
                      "label": need_label(r.purpose, r.medical_location), "place": f"{r.district}, {r.province}",
                      "amount": money(r.amount_try, "TRY") + (f" (about {money(r.amount_need, 'EUR')})" if r.currency == "EUR" else ""),
                      "status": PLEDGE_TEXT[r.status], "pledged": r.status == "pledged",
                      "expires": r.expires_at.strftime("%Y-%m-%d %H:%M") if r.status == "pledged" else None,
                      "need_status": STATUS_TEXT[r.nstatus] if r.nstatus != "published" else None,
                      "payee": r.payee_name if account else None,
                      "iban": " ".join(r.payee_details[i:i + 4] for i in range(0, len(r.payee_details), 4)) if account else None})
    unconfirmed = sum(1 for r in rows if r.status in SHOW_ACCOUNT)
    return render(request, "support.html", status_code=status_code, user=user, rows=shown, done=done, error=error,
                  unconfirmed=unconfirmed, max_unconfirmed=s.max_unconfirmed_pledges)


@router.get("/support")
def support_home(request: Request, done: str = "", user=Depends(require_role("supporter"))):
    return _support_page(request, user, done=done)


@router.get("/support/pledge/{nid}")
def pledge_page(request: Request, nid: int, user=Depends(require_role("supporter"))):
    _expire()
    n = _open_need(nid)
    if n is None:
        raise HTTPException(status_code=404, detail="not found")
    return _pledge_page(request, user, n, _settings())


@router.post("/support/pledge/{nid}")
def pledge(request: Request, nid: int, amount: str = Form(""), ack: str = Form(""), contact_method: str = Form("other"),
           contact: str = Form(""), csrf: str = Form(...), user=Depends(require_role("supporter"))):
    check_csrf(request, csrf)
    _expire()
    n = _open_need(nid)
    if n is None:
        raise HTTPException(status_code=404, detail="not found")
    s = _settings()
    form = {"amount": amount.strip(), "ack": bool(ack), "contact_method": contact_method, "contact": contact.strip()}
    open_try = _open_try(n, s)
    errors = []
    if not _WHOLE.fullmatch(form["amount"]) or int(form["amount"]) < s.pledge_floor:
        errors.append(f"The amount must be a whole number of at least {s.pledge_floor} lira.")
    elif int(form["amount"]) > open_try:
        errors.append(f"That is more than is still open. At most {open_try:,} lira can be pledged now.")
    if not ack:
        errors.append("Tick the box to say you have read the notice on withdrawals and refunds.")
    problem = contact_problem(form["contact_method"], form["contact"])
    if problem:
        errors.append(problem)
    if errors:
        return _pledge_page(request, user, n, s, 400, " ".join(errors) + " Nothing was saved.", form)
    try:
        with engine.begin() as conn:
            pid = conn.execute(
                text("INSERT INTO pledges (need_id, supporter_id, amount_try, ack, contact_method, contact) "
                     "VALUES (:n, :s, :a, true, :m, :k) RETURNING id"),
                {"n": nid, "s": user.id, "a": int(form["amount"]), "m": form["contact_method"], "k": form["contact"]}).scalar_one()
            conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'pledged', :p)"),
                         {"n": nid, "a": user.id, "p": f"pledge {pid}"})
    except DBAPIError as e:
        msg = str(e.orig)
        if "exceed the goal" in msg:
            error, code = "Someone else pledged in the meantime and this amount is now more than is still open. Nothing was saved.", 409
        elif "too many unconfirmed" in msg:
            error, code = (f"You already have {s.max_unconfirmed_pledges} pledges that are not confirmed yet. "
                           "Wait until some are confirmed. Nothing was saved."), 429
        elif "not open for pledges" in msg:
            error, code = "This need is no longer open for pledges. Nothing was saved.", 409
        else:
            error, code = "The pledge could not be saved. Nothing was saved.", 400
        fresh = _open_need(nid)
        if fresh is None:
            return _support_page(request, user, code, error=error)
        return _pledge_page(request, user, fresh, s, code, error, form)
    return RedirectResponse("/support?done=pledged", status_code=303)


def _change(request, user, pid: int, new: str, event: str, done: str):
    with engine.connect() as c:
        owner = c.execute(text("SELECT supporter_id FROM pledges WHERE id = :p"), {"p": pid}).scalar()
    if owner != user.id:
        raise HTTPException(status_code=404, detail="not found")
    _expire()
    with engine.begin() as conn:
        row = conn.execute(
            text("UPDATE pledges SET status = :s WHERE id = :p AND supporter_id = :u AND status = 'pledged' RETURNING need_id"),
            {"s": new, "p": pid, "u": user.id}).one_or_none()
        if row is not None:
            conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, :e, :p)"),
                         {"n": row.need_id, "a": user.id, "e": event, "p": f"pledge {pid}"})
    if row is None:
        return _support_page(request, user, 409, error="This pledge has already changed (sent, cancelled or expired). Nothing was done.")
    return RedirectResponse(f"/support?done={done}", status_code=303)


@router.post("/support/{pid}/sent")
def pledge_sent(request: Request, pid: int, csrf: str = Form(...), user=Depends(require_role("supporter"))):
    check_csrf(request, csrf)
    return _change(request, user, pid, "sent", "pledge_sent", "sent")


@router.post("/support/{pid}/cancel")
def pledge_cancel(request: Request, pid: int, csrf: str = Form(...), user=Depends(require_role("supporter"))):
    check_csrf(request, csrf)
    return _change(request, user, pid, "cancelled", "pledge_cancelled", "cancelled")
