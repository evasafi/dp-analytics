"""Requester pages: post a need, see my needs, withdraw a need with a reason, confirm or dispute a pledge marked sent."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.auth import require_role
from app.db import engine
from app.districts import DISTRICTS_BY_PROVINCE
from app.provinces import PROVINCES
from app.reference import (EVENTS, MAX_PENDING, MEDICAL_LOCATIONS, PURPOSES, STATUS_TEXT, load_limits, money,
                           need_label)
from app.validation import need_problems, normalize_iban, public_text_problem
from app.web import check_csrf, render

router = APIRouter()

FIELDS = ("purpose", "medical_location", "event", "province", "district", "household_size", "goal",
          "description", "contact_method", "contact", "payee_name", "iban")


def _form_page(request, user, status_code, errors, form):
    with engine.connect() as c:
        limits = load_limits(c)
    return render(request, "need_new.html", status_code=status_code, user=user, purposes=PURPOSES, events=EVENTS,
                  provinces=PROVINCES, districts_by_province=DISTRICTS_BY_PROVINCE, errors=errors, form=form,
                  limits={k: {"currency": cur, "ceiling": n} for k, (cur, n) in limits.items()})


@router.get("/needs/new")
def need_new_page(request: Request, user=Depends(require_role("requester"))):
    return _form_page(request, user, 200, [], None)


@router.post("/needs/new")
def need_new(request: Request, purpose: str = Form(""), medical_location: str = Form(""), event: str = Form(""),
             province: str = Form(""), district: str = Form(""), household_size: str = Form(""), goal: str = Form(""),
             description: str = Form(""), contact_method: str = Form("other"), contact: str = Form(""),
             payee_name: str = Form(""), iban: str = Form(""), csrf: str = Form(...),
             user=Depends(require_role("requester"))):
    check_csrf(request, csrf)
    values = dict(zip(FIELDS, (purpose, medical_location, event, province, district, household_size, goal,
                               description, contact_method, contact, payee_name, iban)))
    form = {k: v.strip() for k, v in values.items()}
    with engine.connect() as c:
        limits = load_limits(c)
    errors = need_problems(form, limits)
    if errors:
        return _form_page(request, user, 400, errors, form)
    # a field that does not apply to the chosen purpose is ignored, not an error (a browser submits hidden fields)
    loc = form["medical_location"] if form["purpose"] == "medical" else None
    event_value = form["event"] if form["purpose"] == "disaster_recovery" else None
    currency = "EUR" if loc == "abroad" else "TRY"
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": 8000000 + user.id})
            pending = conn.execute(
                text("SELECT count(*) FROM needs WHERE requester_id = :u AND status = 'pending'"),
                {"u": user.id}).scalar_one()
            if pending >= MAX_PENDING:
                return _form_page(request, user, 429, [
                    f"You already have {MAX_PENDING} needs waiting for review. Please wait for a verifier."], form)
            nid = conn.execute(
                text("INSERT INTO needs (requester_id, purpose, medical_location, event, currency, province, district, "
                     "household_size, goal, description, contact_method, contact, payee_name, payee_details) "
                     "VALUES (:u, :pu, :loc, :ev, :cur, :pv, :di, :hh, :g, :de, :cm, :co, :pn, :pd) RETURNING id"),
                {"u": user.id, "pu": form["purpose"], "loc": loc, "ev": event_value, "cur": currency,
                 "pv": form["province"], "di": form["district"], "hh": int(form["household_size"]),
                 "g": int(form["goal"]), "de": form["description"], "cm": form["contact_method"],
                 "co": form["contact"], "pn": form["payee_name"], "pd": normalize_iban(form["iban"])}).scalar_one()
            conn.execute(text("INSERT INTO need_events (need_id, actor_id, event) VALUES (:n, :u, 'submitted')"),
                         {"n": nid, "u": user.id})
    except DBAPIError:
        return _form_page(request, user, 400, ["The need could not be saved. Check the amount against the limit and try again."], form)
    return RedirectResponse("/needs/mine?done=1", status_code=303)


RECEIPT_TEXT = {"sent": "The donor says it was sent", "disputed": "You said it did not arrive. A verifier is looking at it",
                "confirmed": "You confirmed it arrived"}


def _mine_page(request, user, status_code=200, done=False, withdrawn=False, received=False, disputed=False, error=None,
               open_id=None, reason="", open_pid=None, dispute_reason=""):
    with engine.connect() as c:
        sent = c.execute(text(
            "SELECT p.id, p.need_id, p.status, p.amount_try, p.amount_need, p.sent_at, n.currency FROM pledges p "
            "JOIN needs n ON n.id = p.need_id WHERE n.requester_id = :u AND p.status IN ('sent', 'disputed', 'confirmed') "
            "ORDER BY p.id"), {"u": user.id}).all()
        rows = c.execute(text(
            "SELECT n.id, n.created_at, n.purpose, n.medical_location, n.province, n.district, n.currency, n.goal, "
            "n.status, n.description, n.review_note, n.withdrawn_reason, "
            "COALESCE(p.promised, 0) AS promised, COALESCE(p.confirmed, 0) AS confirmed, COALESCE(p.donors, 0) AS donors "
            "FROM needs n LEFT JOIN (SELECT need_id, sum(amount_need) AS promised, "
            "COALESCE(sum(amount_need) FILTER (WHERE status = 'confirmed'), 0) AS confirmed, "
            "count(DISTINCT supporter_id) AS donors FROM pledges "
            "WHERE status IN ('pledged', 'sent', 'confirmed', 'disputed') GROUP BY need_id) p ON p.need_id = n.id "
            "WHERE n.requester_id = :u ORDER BY n.id DESC"), {"u": user.id}).all()
    by_need = {}
    for p in sent:
        by_need.setdefault(p.need_id, []).append({
            "id": p.id, "sent": p.sent_at.strftime("%Y-%m-%d"), "status": RECEIPT_TEXT[p.status], "open": p.status == "sent",
            "amount": money(p.amount_try, "TRY") + (f" (about {money(p.amount_need, 'EUR')})" if p.currency == "EUR" else "")})
    shown = []
    for r in rows:
        funding = ""
        if r.donors:
            parts = ["Confirmed " + money(r.confirmed, r.currency)]
            if r.promised - r.confirmed > 0:
                parts.append("promised %s more" % money(r.promised - r.confirmed, r.currency))
            parts.append("%d donor%s" % (r.donors, "" if r.donors == 1 else "s"))
            funding = ", ".join(parts) + "."
        shown.append({"id": r.id, "created": r.created_at.strftime("%Y-%m-%d"), "label": need_label(r.purpose, r.medical_location),
                      "place": f"{r.district}, {r.province}", "goal": money(r.goal, r.currency),
                      "status": STATUS_TEXT[r.status], "funding": funding, "description": r.description,
                      "review_note": r.review_note, "withdrawn_reason": r.withdrawn_reason,
                      "can_withdraw": r.status in ("pending", "published"), "pledges": by_need.get(r.id, [])})
    return render(request, "needs_mine.html", status_code=status_code, user=user, rows=shown, done=done,
                  withdrawn=withdrawn, received=received, disputed=disputed, error=error, open_id=open_id, reason=reason,
                  open_pid=open_pid, dispute_reason=dispute_reason)


@router.get("/needs/mine")
def needs_mine(request: Request, done: int = 0, withdrawn: int = 0, received: int = 0, disputed: int = 0,
               user=Depends(require_role("requester"))):
    return _mine_page(request, user, done=bool(done), withdrawn=bool(withdrawn), received=bool(received), disputed=bool(disputed))


def _my_pledge_status(pid: int, user):
    """The status of a pledge to one of this requester's needs, or a 404 when it is not theirs."""
    with engine.connect() as c:
        st = c.execute(text("SELECT p.status FROM pledges p JOIN needs n ON n.id = p.need_id WHERE p.id = :p AND n.requester_id = :u"),
                       {"p": pid, "u": user.id}).scalar()
    if st is None:
        raise HTTPException(status_code=404, detail="not found")
    return st


def _receipt(user, pid: int, new: str, event: str, note: str) -> bool:
    """Move a sent pledge to confirmed or disputed. False when it is no longer sent."""
    with engine.begin() as conn:
        row = conn.execute(
            text("UPDATE pledges SET status = :s WHERE id = :p AND status = 'sent' "
                 "AND need_id IN (SELECT id FROM needs WHERE requester_id = :u) RETURNING need_id"),
            {"s": new, "p": pid, "u": user.id}).one_or_none()
        if row is None:
            return False
        conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :u, :e, :t)"),
                     {"n": row.need_id, "u": user.id, "e": event, "t": note})
    return True


@router.post("/needs/pledges/{pid}/confirm")
def pledge_confirm(request: Request, pid: int, csrf: str = Form(...), user=Depends(require_role("requester"))):
    check_csrf(request, csrf)
    _my_pledge_status(pid, user)
    if not _receipt(user, pid, "confirmed", "receipt_confirmed", f"pledge {pid}"):
        return _mine_page(request, user, 409, error="This pledge has already been answered. Nothing was changed.")
    return RedirectResponse("/needs/mine?received=1", status_code=303)


@router.post("/needs/pledges/{pid}/dispute")
def pledge_dispute(request: Request, pid: int, reason: str = Form(""), csrf: str = Form(...),
                   user=Depends(require_role("requester"))):
    check_csrf(request, csrf)
    reason = reason.strip()
    if _my_pledge_status(pid, user) != "sent":
        return _mine_page(request, user, 409, error="This pledge has already been answered. Nothing was changed.")
    if not 10 <= len(reason) <= 450:
        return _mine_page(request, user, 400, error="Say in 10 to 450 characters what you checked. Only verifiers see it. Nothing was changed.",
                          open_pid=pid, dispute_reason=reason)
    if not _receipt(user, pid, "disputed", "receipt_disputed", f"pledge {pid}: {reason}"):
        return _mine_page(request, user, 409, error="This pledge has already been answered. Nothing was changed.")
    return RedirectResponse("/needs/mine?disputed=1", status_code=303)


@router.post("/needs/{nid}/withdraw")
def need_withdraw(request: Request, nid: int, reason: str = Form(""), csrf: str = Form(...),
                  user=Depends(require_role("requester"))):
    check_csrf(request, csrf)
    reason = reason.strip()
    with engine.connect() as c:
        row = c.execute(text("SELECT status, contact, payee_name FROM needs WHERE id = :n AND requester_id = :u"),
                        {"n": nid, "u": user.id}).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if row.status not in ("pending", "published"):
        return _mine_page(request, user, 409, error="This need can no longer be withdrawn.")
    if not 10 <= len(reason) <= 500:
        return _mine_page(request, user, 400, error="Give a reason of 10 to 500 characters. A need cannot be withdrawn without one.",
                          open_id=nid, reason=reason)
    problem = public_text_problem(reason, row.contact, row.payee_name, user.username, max_len=500, what="reason", owner="your")
    if problem:
        return _mine_page(request, user, 400, error=problem + " Donors who pledged to this need will read it. Nothing was changed.",
                          open_id=nid, reason=reason)
    with engine.begin() as conn:
        changed = conn.execute(
            text("UPDATE needs SET status = 'withdrawn', withdrawn_reason = :r "
                 "WHERE id = :n AND requester_id = :u AND status IN ('pending', 'published')"),
            {"r": reason, "n": nid, "u": user.id}).rowcount
        if changed != 1:
            return _mine_page(request, user, 409, error="This need can no longer be withdrawn.")
        conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :u, 'withdrawn', :r)"),
                     {"n": nid, "u": user.id, "r": reason})
    return RedirectResponse("/needs/mine?withdrawn=1", status_code=303)
