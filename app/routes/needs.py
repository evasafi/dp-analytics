"""Requester pages: post a need, see my needs, withdraw a need with a reason."""
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
from app.validation import need_problems, normalize_iban
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


def _mine_page(request, user, status_code=200, done=False, withdrawn=False, error=None, open_id=None, reason=""):
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT n.id, n.created_at, n.purpose, n.medical_location, n.province, n.district, n.currency, n.goal, "
            "n.status, n.description, n.review_note, n.withdrawn_reason, "
            "COALESCE(p.promised, 0) AS promised, COALESCE(p.confirmed, 0) AS confirmed, COALESCE(p.donors, 0) AS donors "
            "FROM needs n LEFT JOIN (SELECT need_id, sum(amount_need) AS promised, "
            "COALESCE(sum(amount_need) FILTER (WHERE status = 'confirmed'), 0) AS confirmed, "
            "count(DISTINCT supporter_id) AS donors FROM pledges "
            "WHERE status IN ('pledged', 'sent', 'confirmed', 'disputed') GROUP BY need_id) p ON p.need_id = n.id "
            "WHERE n.requester_id = :u ORDER BY n.id DESC"), {"u": user.id}).all()
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
                      "can_withdraw": r.status in ("pending", "published")})
    return render(request, "needs_mine.html", status_code=status_code, user=user, rows=shown, done=done,
                  withdrawn=withdrawn, error=error, open_id=open_id, reason=reason)


@router.get("/needs/mine")
def needs_mine(request: Request, done: int = 0, withdrawn: int = 0, user=Depends(require_role("requester"))):
    return _mine_page(request, user, done=bool(done), withdrawn=bool(withdrawn))


@router.post("/needs/{nid}/withdraw")
def need_withdraw(request: Request, nid: int, reason: str = Form(""), csrf: str = Form(...),
                  user=Depends(require_role("requester"))):
    check_csrf(request, csrf)
    reason = reason.strip()
    with engine.connect() as c:
        row = c.execute(text("SELECT status FROM needs WHERE id = :n AND requester_id = :u"),
                        {"n": nid, "u": user.id}).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if row.status not in ("pending", "published"):
        return _mine_page(request, user, 409, error="This need can no longer be withdrawn.")
    if not 10 <= len(reason) <= 500:
        return _mine_page(request, user, 400, error="Give a reason of 10 to 500 characters. A need cannot be withdrawn without one.",
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
