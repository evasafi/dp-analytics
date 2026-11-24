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

from app import items
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


def _pledge_page(request, user, n, s, status_code=200, error=None, form=None, errors=None):
    open_try = _open_try(n, s)
    return render(request, "pledge_form.html", status_code=status_code, user=user, c=_card(n), open_try=open_try,
                  suggestion=_suggestion(n, s, open_try),
                  floor=s.pledge_floor, days=s.pledge_expiry_days, rate=f"{s.eur_try_rate:.0f}", error=error, form=form or {}, errors=errors or [])


def _support_page(request, user, status_code=200, done="", error=None):
    _expire()
    s = _settings()
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT p.id, p.status, p.amount_try, p.amount_need, p.ref_code, p.created_at, p.expires_at, n.id AS nid, n.purpose, "
            "n.medical_location, n.province, n.district, n.currency, n.status AS nstatus, n.payee_name, n.payee_details, "
            "n.withdrawn_reason "
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
                      "withdrawn_reason": r.withdrawn_reason if r.nstatus == "withdrawn" else None,
                      "code": r.ref_code if account else None,
                      "payee": r.payee_name if account else None,
                      "iban": " ".join(r.payee_details[i:i + 4] for i in range(0, len(r.payee_details), 4)) if account else None})
    unconfirmed = sum(1 for r in rows if r.status in SHOW_ACCOUNT)
    claims, claims_open = _my_claims(user.id)
    return render(request, "support.html", status_code=status_code, user=user, rows=shown, done=done, error=error,
                  unconfirmed=unconfirmed, max_unconfirmed=s.max_unconfirmed_pledges,
                  claims=claims, claims_open=claims_open, max_claims=s.max_unconfirmed_pledges)


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
        return _pledge_page(request, user, n, s, 400, None, form, errors=errors)
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


CLAIM_TEXT = {"sent": "Handed over. Waiting for the requester to confirm", "confirmed": "Confirmed by the requester",
              "disputed": "Disputed. A verifier is looking at it", "expired": "Expired. It was not handed over in time",
              "cancelled": "Cancelled"}
CLAIM_OPEN = ("pledged", "sent", "disputed")


def _claim_settings():
    with engine.connect() as c:
        return c.execute(text("SELECT claim_expiry_days, max_unconfirmed_pledges FROM settings")).one()


def _open_item(nid: int):
    with engine.connect() as c:
        return c.execute(text(f"SELECT {COLUMNS} FROM public_needs WHERE id = :i AND need_kind = 'item'"), {"i": nid}).one_or_none()


def _claim_form_page(request, user, n, s, status_code=200, error=None, form=None, errors=None):
    return render(request, "item_claim_form.html", status_code=status_code, user=user, c=_card(n),
                  remaining=max(0, int(n.quantity_needed) - int(n.promised_units)), days=s.claim_expiry_days,
                  error=error, form=form or {}, errors=errors or [])


def _my_claims(uid: int):
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT k.id, k.status, k.units, k.created_at, k.expires_at, k.released_at, n.id AS nid, n.province, n.district, "
            "n.handover_method, n.status AS nstatus, n.withdrawn_reason, t.name AS item_name, "
            "(SELECT e.note FROM need_events e WHERE e.need_id = n.id AND e.event = 'claim_declined' "
            "AND left(e.note, length('claim ' || k.id || ':')) = 'claim ' || k.id || ':' ORDER BY e.id DESC LIMIT 1) AS declined "
            "FROM item_claims k JOIN needs n ON n.id = k.need_id LEFT JOIN item_types t ON t.id = n.item_type_id "
            "WHERE k.donor_id = :u ORDER BY k.id DESC"), {"u": uid}).all()
    out = []
    for r in rows:
        declined = None
        if r.status == "cancelled" and r.declined:
            declined = r.declined.split(": ", 1)[1] if ": " in r.declined else r.declined
        if declined is not None:
            status = "Declined by a verifier"
        elif r.status == "pledged":
            status = ("Offered. A verifier will pass your contact to the requester" if r.released_at is None else
                      "Your contact was passed to the requester, who will get in touch. Agree the handover with them")
        else:
            status = CLAIM_TEXT[r.status]
        handover = None
        if r.released_at is not None and r.status in CLAIM_OPEN:
            handover = dict(items.HANDOVER_METHODS).get(r.handover_method, "").replace(" (recommended)", "")
        out.append({"id": r.id, "nid": r.nid, "created": r.created_at.strftime("%Y-%m-%d"), "label": r.item_name,
                    "place": f"{r.district}, {r.province}", "units": r.units, "status": status, "pledged": r.status == "pledged",
                    "expires": r.expires_at.strftime("%Y-%m-%d %H:%M") if r.status == "pledged" and r.expires_at else None,
                    "declined": declined, "handover": handover, "released": r.released_at is not None,
                    "need_status": STATUS_TEXT[r.nstatus] if r.nstatus != "published" else None,
                    "withdrawn_reason": r.withdrawn_reason if r.nstatus == "withdrawn" else None})
    return out, sum(1 for r in rows if r.status in CLAIM_OPEN)


@router.get("/support/item/{nid}")
def claim_page(request: Request, nid: int, user=Depends(require_role("supporter"))):
    _expire()
    n = _open_item(nid)
    if n is None:
        raise HTTPException(status_code=404, detail="not found")
    return _claim_form_page(request, user, n, _claim_settings())


@router.post("/support/item/{nid}")
def claim_item(request: Request, nid: int, units: str = Form(""), ack: str = Form(""), contact_method: str = Form("other"),
               contact: str = Form(""), csrf: str = Form(...), user=Depends(require_role("supporter"))):
    check_csrf(request, csrf)
    _expire()
    n = _open_item(nid)
    if n is None:
        raise HTTPException(status_code=404, detail="not found")
    s = _claim_settings()
    form = {"units": units.strip(), "ack": bool(ack), "contact_method": contact_method, "contact": contact.strip()}
    remaining = max(0, int(n.quantity_needed) - int(n.promised_units))
    errors = []
    if not _WHOLE.fullmatch(form["units"]) or int(form["units"]) < 1:
        errors.append("The number of units must be a whole number of at least 1.")
    elif int(form["units"]) > remaining:
        errors.append(f"That is more than is still open. At most {remaining} unit{'' if remaining == 1 else 's'} can be offered now.")
    if not ack:
        errors.append("Tick the box to say you have read the notice and accept that the requester will learn your contact.")
    problem = contact_problem(form["contact_method"], form["contact"])
    if problem:
        errors.append(problem)
    if errors:
        return _claim_form_page(request, user, n, s, 400, None, form, errors=errors)
    try:
        with engine.begin() as conn:
            cid = conn.execute(
                text("INSERT INTO item_claims (need_id, donor_id, units, ack, contact_method, contact) "
                     "VALUES (:n, :d, :u, true, :m, :k) RETURNING id"),
                {"n": nid, "d": user.id, "u": int(form["units"]), "m": form["contact_method"], "k": form["contact"]}).scalar_one()
            conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'claimed', :p)"),
                         {"n": nid, "a": user.id, "p": f"claim {cid}"})
    except DBAPIError as e:
        msg = str(e.orig)
        if "exceed the quantity" in msg:
            error, code = "Someone else offered in the meantime and this is now more than is still open. Nothing was saved.", 409
        elif "too many unconfirmed" in msg:
            error, code = (f"You already have {s.max_unconfirmed_pledges} offers that are not confirmed yet. "
                           "Wait until some are confirmed. Nothing was saved."), 429
        elif "not open for item claims" in msg:
            error, code = "This need is no longer open for offers. Nothing was saved.", 409
        elif "your own need" in msg:
            error, code = "You cannot offer help with your own need. Nothing was saved.", 403
        else:
            error, code = "The offer could not be saved. Nothing was saved.", 400
        fresh = _open_item(nid)
        if fresh is None:
            return _support_page(request, user, code, error=error)
        return _claim_form_page(request, user, fresh, s, code, error, form)
    return RedirectResponse("/support?done=claimed", status_code=303)


@router.post("/support/claims/{cid}/cancel")
def claim_cancel(request: Request, cid: int, csrf: str = Form(...), user=Depends(require_role("supporter"))):
    check_csrf(request, csrf)
    with engine.connect() as c:
        owner = c.execute(text("SELECT donor_id FROM item_claims WHERE id = :c"), {"c": cid}).scalar()
    if owner != user.id:
        raise HTTPException(status_code=404, detail="not found")
    _expire()
    with engine.begin() as conn:
        row = conn.execute(
            text("UPDATE item_claims SET status = 'cancelled' WHERE id = :c AND donor_id = :u AND status = 'pledged' RETURNING need_id"),
            {"c": cid, "u": user.id}).one_or_none()
        if row is not None:
            conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'claim_cancelled', :p)"),
                         {"n": row.need_id, "a": user.id, "p": f"claim {cid}"})
    if row is None:
        return _support_page(request, user, 409, error="This offer has already changed (handed over, cancelled or expired). Nothing was done.")
    return RedirectResponse("/support?done=claim_cancelled", status_code=303)


HANDOVER_STALE = ("This offer cannot be marked as handed over now. It may not be released yet, or it was already handed over, "
                  "cancelled or expired, or the need is closed. Nothing was changed.")


class _Stale(Exception):
    pass


def _handover_state(cid: int, uid: int):
    with engine.connect() as c:
        return c.execute(text(
            "SELECT k.id, k.units, k.status, k.released_at, n.status AS nstatus, n.province, n.district, t.name AS item_name "
            "FROM item_claims k JOIN needs n ON n.id = k.need_id LEFT JOIN item_types t ON t.id = n.item_type_id "
            "WHERE k.id = :c AND k.donor_id = :u"), {"c": cid, "u": uid}).one_or_none()


def _can_hand_over(k) -> bool:
    return k.status == "pledged" and k.released_at is not None and k.nstatus == "published"


def _handover_page(request, user, k, status_code=200, error=None):
    return render(request, "item_handover.html", status_code=status_code, user=user, k=k, error=error)


@router.get("/support/claims/{cid}/handover")
def handover_page(request: Request, cid: int, user=Depends(require_role("supporter"))):
    _expire()
    k = _handover_state(cid, user.id)
    if k is None:
        raise HTTPException(status_code=404, detail="not found")
    if not _can_hand_over(k):
        return _support_page(request, user, 409, error=HANDOVER_STALE)
    return _handover_page(request, user, k)


@router.post("/support/claims/{cid}/handover")
def handover(request: Request, cid: int, ack: str = Form(""), csrf: str = Form(...), user=Depends(require_role("supporter"))):
    check_csrf(request, csrf)
    _expire()
    k = _handover_state(cid, user.id)
    if k is None:
        raise HTTPException(status_code=404, detail="not found")
    if not _can_hand_over(k):
        return _support_page(request, user, 409, error=HANDOVER_STALE)
    if not ack:
        return _handover_page(request, user, k, 400, "Tick the box to confirm that you handed the items over.")
    try:
        with engine.begin() as conn:
            done = conn.execute(
                text("UPDATE item_claims SET status = 'sent' WHERE id = :c AND donor_id = :u AND status = 'pledged' "
                     "AND released_at IS NOT NULL RETURNING need_id"), {"c": cid, "u": user.id}).one_or_none()
            if done is None:
                raise _Stale()
            conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'claim_sent', :p)"),
                         {"n": done.need_id, "a": user.id, "p": f"claim {cid}"})
    except (_Stale, DBAPIError):
        return _support_page(request, user, 409, error=HANDOVER_STALE)
    return RedirectResponse("/support?done=claim_sent", status_code=303)
