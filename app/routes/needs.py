"""Requester pages: post a need, see my needs, withdraw a need with a reason, confirm or dispute a pledge marked sent."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app import items
from app.auth import require_role
from app.routes.board import _expire
from app.proofs import KEEP_SQL, photo_response
from app.db import engine
from app.districts import DISTRICTS_BY_PROVINCE
from app.provinces import PROVINCES
from app.reference import (EVENTS, MAX_PENDING, PURPOSES, STATUS_TEXT, load_limits, money,
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
                  item_open=items.ITEM_NEEDS_OPEN,
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


ITEM_FIELDS = ("item_type_id", "quantity_needed", "specification", "used_ok", "handover_method", "province",
               "district", "household_size", "description", "contact_method", "contact")


def _item_form_page(request, user, status_code, errors, form):
    with engine.connect() as c:
        groups, _ = items.load_item_types(c)
    return render(request, "need_item_new.html", status_code=status_code, user=user, groups=groups,
                  handover_methods=items.HANDOVER_METHODS, provinces=PROVINCES,
                  districts_by_province=DISTRICTS_BY_PROVINCE, errors=errors, form=form)


@router.get("/needs/new/item")
def item_new_page(request: Request, user=Depends(require_role("requester"))):
    if not items.ITEM_NEEDS_OPEN:
        raise HTTPException(status_code=404)
    return _item_form_page(request, user, 200, [], None)


@router.post("/needs/new/item")
def item_new(request: Request, item_type_id: str = Form(""), quantity_needed: str = Form(""),
             specification: str = Form(""), used_ok: str = Form(""), handover_method: str = Form(""),
             province: str = Form(""), district: str = Form(""), household_size: str = Form(""),
             description: str = Form(""), contact_method: str = Form("other"), contact: str = Form(""),
             csrf: str = Form(...), user=Depends(require_role("requester"))):
    if not items.ITEM_NEEDS_OPEN:
        raise HTTPException(status_code=404)
    check_csrf(request, csrf)
    values = dict(zip(ITEM_FIELDS, (item_type_id, quantity_needed, specification, used_ok, handover_method, province,
                                    district, household_size, description, contact_method, contact)))
    form = {k: v.strip() for k, v in values.items()}
    with engine.connect() as c:
        _, type_ids = items.load_item_types(c)
    errors = items.item_problems(form, type_ids)
    if errors:
        return _item_form_page(request, user, 400, errors, form)
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": 8000000 + user.id})
            pending = conn.execute(
                text("SELECT count(*) FROM needs WHERE requester_id = :u AND status = 'pending'"),
                {"u": user.id}).scalar_one()
            if pending >= MAX_PENDING:
                return _item_form_page(request, user, 429, [
                    f"You already have {MAX_PENDING} needs waiting for review. Please wait for a verifier."], form)
            nid = conn.execute(
                text("INSERT INTO needs (requester_id, need_kind, item_type_id, quantity_needed, specification, "
                     "used_ok, handover_method, province, district, household_size, description, contact_method, "
                     "contact) VALUES (:u, 'item', :it, :q, :sp, :us, :hm, :pv, :di, :hh, :de, :cm, :co) "
                     "RETURNING id"),
                {"u": user.id, "it": int(form["item_type_id"]), "q": int(form["quantity_needed"]),
                 "sp": form["specification"] or None, "us": form["used_ok"] == "used",
                 "hm": form["handover_method"], "pv": form["province"], "di": form["district"],
                 "hh": int(form["household_size"]), "de": form["description"], "cm": form["contact_method"],
                 "co": form["contact"]}).scalar_one()
            conn.execute(text("INSERT INTO need_events (need_id, actor_id, event) VALUES (:n, :u, 'submitted')"),
                         {"n": nid, "u": user.id})
    except DBAPIError:
        return _item_form_page(request, user, 400, ["The need could not be saved. Check the form and try again."], form)
    return RedirectResponse("/needs/mine?done=1", status_code=303)


RECEIPT_TEXT = {"sent": "The donor says it was sent", "disputed": "You said it did not arrive. A verifier is looking at it",
                "confirmed": "You confirmed it arrived"}
OUTCOME_TEXT = {"dispute_arrived": "You said it did not arrive. A verifier checked and found it did. It now counts as confirmed",
                "dispute_not_arrived": "You said it did not arrive. A verifier checked and agreed. The pledge was cancelled"}


OFFER_TEXT = {"pledged": "Offered. Contact the donor to agree the handover", "sent": "The donor says it was handed over",
              "confirmed": "Confirmed", "disputed": "Disputed. A verifier is looking at it"}
CONTACT_LABELS = {"phone": "Phone", "email": "Email", "whatsapp": "WhatsApp", "other": "Other"}


def _mine_page(request, user, status_code=200, done=False, withdrawn=False, received=False, disputed=False, error=None,
               open_id=None, reason="", open_pid=None, dispute_reason=""):
    _expire()
    with engine.connect() as c:
        sent = c.execute(text(
            "SELECT p.id, p.need_id, p.status, p.amount_try, p.amount_need, p.sent_at, n.currency FROM pledges p "
            "JOIN needs n ON n.id = p.need_id WHERE n.requester_id = :u AND p.status IN ('sent', 'disputed', 'confirmed', 'cancelled') "
            "ORDER BY p.id"), {"u": user.id}).all()
        settled = c.execute(text(
            "SELECT p.id, e.event FROM need_events e JOIN pledges p ON p.need_id = e.need_id "
            "AND left(e.note, length('pledge ' || p.id || ':')) = 'pledge ' || p.id || ':' "
            "JOIN needs n ON n.id = p.need_id WHERE n.requester_id = :u "
            "AND e.event IN ('dispute_arrived', 'dispute_not_arrived')"), {"u": user.id}).all()
        rows = c.execute(text(
            "SELECT n.id, n.created_at, n.purpose, n.medical_location, n.province, n.district, n.currency, n.goal, "
            "n.need_kind, n.quantity_needed, t.name AS item_name, "
            "n.status, n.description, n.review_note, n.withdrawn_reason, "
            "COALESCE(p.promised, 0) AS promised, COALESCE(p.confirmed, 0) AS confirmed, COALESCE(p.donors, 0) AS donors "
            "FROM needs n LEFT JOIN (SELECT need_id, sum(amount_need) AS promised, "
            "COALESCE(sum(amount_need) FILTER (WHERE status = 'confirmed'), 0) AS confirmed, "
            "count(DISTINCT supporter_id) AS donors FROM pledges "
            "WHERE status IN ('pledged', 'sent', 'confirmed', 'disputed') GROUP BY need_id) p ON p.need_id = n.id "
            "LEFT JOIN item_types t ON t.id = n.item_type_id WHERE n.requester_id = :u ORDER BY n.id DESC"), {"u": user.id}).all()
        offers = c.execute(text(
            "SELECT k.id, k.need_id, k.units, k.status, k.contact_method, k.contact, k.expires_at, "
            "(SELECT h.file_ref IS NOT NULL AND h.deleted_at IS NULL FROM handover_proofs h WHERE h.claim_id = k.id) AS has_photo, "
            "(SELECT h.waived_reason IS NOT NULL FROM handover_proofs h WHERE h.claim_id = k.id) AS waived FROM item_claims k "
            "JOIN needs n ON n.id = k.need_id WHERE n.requester_id = :u AND k.released_at IS NOT NULL "
            "AND k.status IN ('pledged', 'sent', 'confirmed', 'disputed') ORDER BY k.id"), {"u": user.id}).all()
    by_need = {}
    outcome = {s.id: s.event for s in settled}
    offers_by_need = {}
    for k in offers:
        offers_by_need.setdefault(k.need_id, []).append({
            "id": k.id, "units": k.units, "status": OFFER_TEXT[k.status],
            "can_answer": k.status == "sent", "has_photo": bool(k.has_photo), "waived": bool(k.waived),
            "contact": None if k.status == "confirmed" else f"{CONTACT_LABELS[k.contact_method]}: {k.contact}",
            "expires": k.expires_at.strftime("%Y-%m-%d") if k.status == "pledged" and k.expires_at else None})
    for p in sent:
        if p.status == "cancelled" and p.id not in outcome:
            continue
        by_need.setdefault(p.need_id, []).append({
            "id": p.id, "sent": p.sent_at.strftime("%Y-%m-%d"), "status": OUTCOME_TEXT.get(outcome.get(p.id)) or RECEIPT_TEXT[p.status], "open": p.status == "sent",
            "amount": money(p.amount_try, "TRY") + (f" (about {money(p.amount_need, 'EUR')})" if p.currency == "EUR" else "")})
    shown = []
    for r in rows:
        funding = ""
        if r.donors:
            parts = ["Confirmed " + money(r.confirmed, r.currency) if r.confirmed else "Nothing confirmed yet"]
            if r.promised - r.confirmed > 0:
                parts.append(("promised %s more" if r.confirmed else "%s promised") % money(r.promised - r.confirmed, r.currency))
            parts.append("%d donor%s" % (r.donors, "" if r.donors == 1 else "s"))
            funding = ", ".join(parts) + "."
        shown.append({"id": r.id, "created": r.created_at.strftime("%Y-%m-%d"), "label": ("Item: " + r.item_name if r.need_kind == "item" else need_label(r.purpose, r.medical_location)),
                      "place": f"{r.district}, {r.province}", "goal": (f"{r.quantity_needed} unit{'' if r.quantity_needed == 1 else 's'}" if r.need_kind == "item"
                 else money(r.goal, r.currency)),
                      "status": STATUS_TEXT[r.status], "funding": funding, "description": r.description,
                      "review_note": r.review_note, "withdrawn_reason": r.withdrawn_reason,
                      "can_withdraw": r.status in ("pending", "published"), "pledges": by_need.get(r.id, []),
                      "offers": offers_by_need.get(r.id, [])})
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


def _offer_owner(cid: int):
    with engine.connect() as c:
        return c.execute(text("SELECT n.requester_id FROM item_claims k JOIN needs n ON n.id = k.need_id WHERE k.id = :c"),
                         {"c": cid}).scalar()


@router.post("/needs/offers/{cid}/confirm")
def offer_confirm(request: Request, cid: int, csrf: str = Form(...), user=Depends(require_role("requester"))):
    check_csrf(request, csrf)
    if _offer_owner(cid) != user.id:
        raise HTTPException(status_code=404, detail="not found")
    with engine.begin() as conn:
        row = conn.execute(text("UPDATE item_claims SET status = 'confirmed' WHERE id = :c AND status = 'sent' RETURNING need_id"),
                           {"c": cid}).one_or_none()
        if row is not None:
            conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'claim_confirmed', :p)"),
                         {"n": row.need_id, "a": user.id, "p": f"claim {cid}"})
            conn.execute(text(KEEP_SQL), {"c": cid})
    if row is None:
        return _mine_page(request, user, 409, error="This offer is not waiting for your answer any more. Nothing was changed.")
    return RedirectResponse("/needs/mine", status_code=303)


@router.post("/needs/offers/{cid}/dispute")
def offer_dispute(request: Request, cid: int, reason: str = Form(""), csrf: str = Form(...),
                  user=Depends(require_role("requester"))):
    check_csrf(request, csrf)
    if _offer_owner(cid) != user.id:
        raise HTTPException(status_code=404, detail="not found")
    reason = reason.strip()
    if not 10 <= len(reason) <= 450:
        return _mine_page(request, user, 400, error="Write a reason of 10 to 450 characters. A verifier will read it.")
    with engine.begin() as conn:
        row = conn.execute(text("UPDATE item_claims SET status = 'disputed' WHERE id = :c AND status = 'sent' RETURNING need_id"),
                           {"c": cid}).one_or_none()
        if row is not None:
            conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'claim_disputed', :p)"),
                         {"n": row.need_id, "a": user.id, "p": f"claim {cid}: {reason}"})
    if row is None:
        return _mine_page(request, user, 409, error="This offer is not waiting for your answer any more. Nothing was changed.")
    return RedirectResponse("/needs/mine", status_code=303)


@router.get("/needs/offers/{cid}/photo")
def offer_photo(cid: int, user=Depends(require_role("requester"))):
    with engine.connect() as c:
        ref = c.execute(text(
            "SELECT h.file_ref FROM handover_proofs h JOIN item_claims k ON k.id = h.claim_id JOIN needs n ON n.id = k.need_id "
            "WHERE h.claim_id = :c AND n.requester_id = :u AND h.deleted_at IS NULL AND h.file_ref IS NOT NULL"),
            {"c": cid, "u": user.id}).scalar()
    if ref is None:
        raise HTTPException(status_code=404, detail="not found")
    return photo_response(ref)
