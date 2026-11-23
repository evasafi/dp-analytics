"""Verifier pages: the review queue, a need's private details, approve or reject, the look-again list, and pledges
that were disputed or left unanswered."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from app import items
from app.auth import require_role
from app.db import engine
from app.reference import STATUS_TEXT, money, need_label
from app.routes.board import _expire
from app.proofs import KEEP_SQL, photo_response
from app.validation import public_text_problem
from app.web import check_csrf, render

router = APIRouter()
CHECKS_NOTE = ("Checked outside the platform: the account holder's name matches the requester's identity; "
               "the supporting document was seen and matches the goal.")
UNANSWERED_DAYS = 14  # a pledge marked sent that the requester has not answered for this long is listed for follow-up
# Readable names for the pledge events the verifier's history shows. Keys are the stored event names.
EVENT_NAMES = {
    "pledged": "Pledged",
    "pledge_sent": "Supporter marked it as sent",
    "pledge_cancelled": "Pledge cancelled",
    "receipt_confirmed": "Requester confirmed receipt",
    "receipt_disputed": "Requester says the money did not arrive",
    "dispute_arrived": "Verifier settled: the money arrived",
    "dispute_not_arrived": "Verifier settled: the money did not arrive",
}
OUTCOMES = {"arrived": ("confirmed", "dispute_arrived"), "not_arrived": ("cancelled", "dispute_not_arrived")}
PLEDGE_LIST = ("SELECT p.id, p.amount_try, p.amount_need, p.sent_at, n.purpose, n.medical_location, n.province, n.district, "
               "n.currency FROM pledges p JOIN needs n ON n.id = p.need_id WHERE {} ORDER BY p.sent_at LIMIT 50")


def _units(q) -> str:
    return f"{q} unit" + ("" if q == 1 else "s")


def _pledge_amount(amount_try, amount_need, currency) -> str:
    return money(amount_try, "TRY") + (f" (about {money(amount_need, 'EUR')})" if currency == "EUR" else "")


def _pledge_rows(rows):
    return [{"id": r.id, "label": need_label(r.purpose, r.medical_location), "place": f"{r.district}, {r.province}",
             "amount": _pledge_amount(r.amount_try, r.amount_need, r.currency), "sent": r.sent_at.strftime("%Y-%m-%d")} for r in rows]


def _need_for_review(nid: int):
    with engine.connect() as c:
        return c.execute(text(
            "SELECT n.id, n.requester_id, u.username, n.purpose, n.medical_location, n.event, n.province, n.district, "
            "n.household_size, n.currency, n.goal, n.description, n.contact, n.contact_method, n.payee_name, "
            "n.payee_details, n.status, n.created_at, n.review_note, n.withdrawn_reason, "
            "n.need_kind, n.quantity_needed, n.specification, n.used_ok, n.handover_method, "
            "t.category AS item_category, t.name AS item_name "
            "FROM needs n JOIN users u ON u.id = n.requester_id LEFT JOIN item_types t ON t.id = n.item_type_id "
            "WHERE n.id = :i"), {"i": nid}).one_or_none()


def _detail_page(request, user, n, status_code=200, draft=None, error=None, payee_ok=False, document_ok=False,
                 contact_ok=False, report_ok=False):
    is_item = n.need_kind == "item"
    label = "Item: " + n.item_name if is_item else need_label(n.purpose, n.medical_location)
    with engine.connect() as c:
        earlier = c.execute(text("SELECT status, count(*) AS k FROM needs WHERE requester_id = :u AND id <> :i GROUP BY status ORDER BY status"),
                            {"u": n.requester_id, "i": n.id}).all()
    history = ", ".join(f"{e.k} {STATUS_TEXT[e.status].lower()}" for e in earlier) or "none"
    return render(request, "verify_detail.html", status_code=status_code, user=user, n=n, label=label,
                  goal=None if is_item else money(n.goal, n.currency),
                  iban=None if is_item else " ".join(n.payee_details[i:i + 4] for i in range(0, len(n.payee_details), 4)),
                  handover_label=dict(items.HANDOVER_METHODS).get(n.handover_method, n.handover_method or ""),
                  needs_report=is_item and n.item_category in REPORT_CATEGORIES,
                  contact_ok=contact_ok, report_ok=report_ok,
                  status_text=STATUS_TEXT[n.status], history=history,
                  draft=draft if draft is not None else (f"{n.item_name}, {_units(n.quantity_needed)} needed in {n.district}, {n.province}."
                                          if is_item else f"{label} need in {n.district}, {n.province}."),
                  error=error, payee_ok=payee_ok, document_ok=document_ok)


@router.get("/verify")
def verify_queue(request: Request, done: str = "", need: int = 0, user=Depends(require_role("verifier"))):
    _expire()
    with engine.connect() as c:
        pending = c.execute(text(
            "SELECT n.id, n.created_at, n.purpose, n.medical_location, n.province, n.district, n.currency, n.goal, "
            "n.need_kind, n.quantity_needed, t.name AS item_name, "
            "left(n.description, 140) AS snippet FROM needs n LEFT JOIN item_types t ON t.id = n.item_type_id "
            "WHERE n.status = 'pending' ORDER BY n.id LIMIT 50")).all()
        again = c.execute(text(
            "SELECT n.id, n.purpose, n.medical_location, n.province, n.district, n.currency, n.withdrawn_reason, p.confirmed "
            "FROM needs n JOIN (SELECT need_id, sum(amount_need) AS confirmed FROM pledges WHERE status = 'confirmed' "
            "GROUP BY need_id) p ON p.need_id = n.id WHERE n.status = 'withdrawn' ORDER BY n.withdrawn_at DESC LIMIT 50")).all()
        disputed = c.execute(text(PLEDGE_LIST.format("p.status = 'disputed'"))).all()
        offers = c.execute(text(
            "SELECT k.id, k.units, k.created_at, t.name AS item_name, n.province, n.district FROM item_claims k "
            "JOIN needs n ON n.id = k.need_id LEFT JOIN item_types t ON t.id = n.item_type_id "
            "WHERE k.status = 'pledged' AND k.released_at IS NULL ORDER BY k.id LIMIT 50")).all()
        idisputes = c.execute(text(
            "SELECT k.id, k.units, k.sent_at, t.name AS item_name, n.province, n.district FROM item_claims k "
            "JOIN needs n ON n.id = k.need_id LEFT JOIN item_types t ON t.id = n.item_type_id "
            "WHERE k.status = 'disputed' ORDER BY k.id LIMIT 50")).all()
        iwaiting = c.execute(text(
            "SELECT k.id, k.units, k.sent_at, t.name AS item_name, n.province, n.district FROM item_claims k "
            "JOIN needs n ON n.id = k.need_id LEFT JOIN item_types t ON t.id = n.item_type_id "
            "WHERE k.status = 'sent' AND k.sent_at < now() - make_interval(days => :d) ORDER BY k.sent_at LIMIT 50"),
            {"d": int(UNANSWERED_DAYS)}).all()
        unanswered = c.execute(text(PLEDGE_LIST.format("p.status = 'sent' AND p.sent_at < now() - make_interval(days => :d)")),
                               {"d": UNANSWERED_DAYS}).all()
    rows = [{"id": r.id, "created": r.created_at.strftime("%Y-%m-%d %H:%M"),
             "label": ("Item: " + r.item_name if r.need_kind == "item" else need_label(r.purpose, r.medical_location)),
             "place": f"{r.district}, {r.province}", "goal": (_units(r.quantity_needed) if r.need_kind == "item" else money(r.goal, r.currency)),
             "snippet": r.snippet} for r in pending]
    flags = [{"id": r.id, "label": need_label(r.purpose, r.medical_location), "place": f"{r.district}, {r.province}",
              "confirmed": money(r.confirmed, r.currency), "reason": r.withdrawn_reason} for r in again]
    return render(request, "verify_queue.html", user=user, rows=rows, flags=flags, done=done, need=need,
                  disputed=_pledge_rows(disputed), unanswered=_pledge_rows(unanswered), days=UNANSWERED_DAYS,
                  offers=[{"id": r.id, "created": r.created_at.strftime("%Y-%m-%d %H:%M"),
                           "label": f"{r.item_name}, {_units(r.units)}", "place": f"{r.district}, {r.province}"}
                          for r in offers],
                  item_disputes=[{"id": r.id, "created": r.sent_at.strftime("%Y-%m-%d %H:%M"),
                                  "label": f"{r.item_name}, {_units(r.units)}", "place": f"{r.district}, {r.province}"}
                                 for r in idisputes],
                  item_waiting=[{"id": r.id, "created": r.sent_at.strftime("%Y-%m-%d %H:%M"),
                                 "label": f"{r.item_name}, {_units(r.units)}", "place": f"{r.district}, {r.province}"}
                                for r in iwaiting])


@router.get("/verify/{nid}")
def verify_detail(request: Request, nid: int, user=Depends(require_role("verifier"))):
    n = _need_for_review(nid)
    if n is None:
        raise HTTPException(status_code=404, detail="not found")
    return _detail_page(request, user, n)


@router.post("/verify/{nid}/approve")
def verify_approve(request: Request, nid: int, public_text: str = Form(""), check_payee: str = Form(""),
                   check_document: str = Form(""), check_contact: str = Form(""), check_report: str = Form(""),
                   csrf: str = Form(...), user=Depends(require_role("verifier"))):
    check_csrf(request, csrf)
    n = _need_for_review(nid)
    if n is None:
        raise HTTPException(status_code=404, detail="not found")
    if n.need_kind == "item":
        return _approve_item(request, user, n, public_text, bool(check_contact), bool(check_report))
    ticked = dict(payee_ok=bool(check_payee), document_ok=bool(check_document))
    if not (check_payee and check_document):
        return _detail_page(request, user, n, 400, draft=public_text,
                            error="Tick both checks before approving. Money goes straight to this account, so both must be done.", **ticked)
    problem = public_text_problem(public_text, n.contact, n.payee_name, n.username)
    if problem:
        return _detail_page(request, user, n, 400, draft=public_text, error=problem, **ticked)
    with engine.begin() as conn:
        done = conn.execute(
            text("UPDATE needs SET status = 'published', public_text = :t, payee_verified = true, reviewed_by = :v, "
                 "reviewed_at = now(), review_note = NULL "
                 "WHERE id = :i AND status = 'pending' AND requester_id <> :v RETURNING id"),
            {"t": public_text.strip(), "v": user.id, "i": nid}).one_or_none()
        if done is None:
            return _detail_page(request, user, n, 409, error="This need is no longer pending, or you cannot decide it. Nothing was changed.")
        conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'approved', :c)"),
                     {"n": nid, "a": user.id, "c": CHECKS_NOTE})
    return RedirectResponse(f"/verify?done=approved&need={nid}", status_code=303)


REPORT_CATEGORIES = ("mobility", "medical_equipment")
ITEM_CHECKS_NOTE = ("Checked outside the platform: I spoke to the requester using the private contact details; "
                    "the household and the need match the request.")
REPORT_NOTE = "Checked outside the platform: a medical report or prescription supports this device or supply."


def _approve_item(request, user, n, public_text, contact_ok, report_ok):
    needs_report = n.item_category in REPORT_CATEGORIES
    ticked = dict(contact_ok=contact_ok, report_ok=report_ok)
    if not contact_ok or (needs_report and not report_ok):
        return _detail_page(request, user, n, 400, draft=public_text,
                            error="Tick every check before approving. The requester is a person you have not met, so the checks must be done.",
                            **ticked)
    problem = public_text_problem(public_text, n.contact, n.payee_name or n.username, n.username)
    if problem:
        return _detail_page(request, user, n, 400, draft=public_text, error=problem, **ticked)
    with engine.begin() as conn:
        done = conn.execute(
            text("UPDATE needs SET status = 'published', public_text = :t, reviewed_by = :v, reviewed_at = now(), "
                 "review_note = NULL WHERE id = :i AND status = 'pending' AND requester_id <> :v "
                 "AND need_kind = 'item' RETURNING id"),
            {"t": public_text.strip(), "v": user.id, "i": n.id}).one_or_none()
        if done is None:
            return _detail_page(request, user, n, 409, error="This need is no longer pending, or you cannot decide it. Nothing was changed.")
        conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'approved', :c)"),
                     {"n": n.id, "a": user.id, "c": ITEM_CHECKS_NOTE})
        if needs_report:
            conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'report_checked', :c)"),
                         {"n": n.id, "a": user.id, "c": REPORT_NOTE})
    return RedirectResponse(f"/verify?done=approved&need={n.id}", status_code=303)


@router.post("/verify/{nid}/reject")
def verify_reject(request: Request, nid: int, note: str = Form(""), csrf: str = Form(...),
                  user=Depends(require_role("verifier"))):
    check_csrf(request, csrf)
    n = _need_for_review(nid)
    if n is None:
        raise HTTPException(status_code=404, detail="not found")
    note = note.strip()
    if not 10 <= len(note) <= 500:
        return _detail_page(request, user, n, 400, error="Write a reason of 10 to 500 characters. The requester will see it.")
    with engine.begin() as conn:
        done = conn.execute(
            text("UPDATE needs SET status = 'rejected', reviewed_by = :v, reviewed_at = now(), review_note = :r "
                 "WHERE id = :i AND status = 'pending' AND requester_id <> :v RETURNING id"),
            {"v": user.id, "r": note, "i": nid}).one_or_none()
        if done is None:
            return _detail_page(request, user, n, 409, error="This need is no longer pending, or you cannot decide it. Nothing was changed.")
        conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'rejected', :r)"),
                     {"n": nid, "a": user.id, "r": note})
    return RedirectResponse("/verify?done=rejected", status_code=303)


def _pledge_page(request, user, pid: int, status_code=200, error=None, note=""):
    with engine.connect() as c:
        p = c.execute(text(
            "SELECT p.id, p.status, p.amount_try, p.amount_need, p.created_at, p.sent_at, p.contact_method AS d_method, "
            "p.contact AS d_contact, d.username AS donor, n.id AS nid, n.purpose, n.medical_location, n.province, n.district, "
            "n.currency, n.status AS nstatus, n.contact_method AS r_method, n.contact AS r_contact, r.username AS requester, "
            "n.payee_name, n.payee_details FROM pledges p JOIN needs n ON n.id = p.need_id "
            "JOIN users d ON d.id = p.supporter_id JOIN users r ON r.id = n.requester_id WHERE p.id = :p"), {"p": pid}).one_or_none()
        if p is None:
            raise HTTPException(status_code=404, detail="not found")
        history = c.execute(text(
            "SELECT e.ts, e.event, e.note, u.username FROM need_events e JOIN users u ON u.id = e.actor_id "
            "WHERE e.need_id = :n AND (e.note = :tag OR e.note LIKE :prefix) "
            # only events the pledge routes write: a withdrawal reason is free text and could imitate a pledge note
            "AND e.event IN ('pledged', 'pledge_sent', 'pledge_cancelled', 'receipt_confirmed', 'receipt_disputed', "
            "'dispute_arrived', 'dispute_not_arrived') ORDER BY e.id"),
            {"n": p.nid, "tag": f"pledge {pid}", "prefix": f"pledge {pid}:%"}).all()
    events = [{"ts": e.ts.strftime("%Y-%m-%d %H:%M"), "event": EVENT_NAMES.get(e.event, e.event), "who": e.username,
               "note": e.note.split(":", 1)[1].strip() if ":" in e.note else ""} for e in history]
    return render(request, "verify_pledge.html", status_code=status_code, user=user, p=p, events=events, error=error, note=note,
                  label=need_label(p.purpose, p.medical_location), amount=_pledge_amount(p.amount_try, p.amount_need, p.currency),
                  need_status=STATUS_TEXT[p.nstatus],
                  iban=" ".join(p.payee_details[i:i + 4] for i in range(0, len(p.payee_details), 4)))


@router.get("/verify/pledges/{pid}")
def verify_pledge(request: Request, pid: int, user=Depends(require_role("verifier"))):
    return _pledge_page(request, user, pid)


@router.post("/verify/pledges/{pid}/resolve")
def verify_resolve(request: Request, pid: int, outcome: str = Form(""), note: str = Form(""), csrf: str = Form(...),
                   user=Depends(require_role("verifier"))):
    check_csrf(request, csrf)
    note = note.strip()
    if outcome not in OUTCOMES:
        return _pledge_page(request, user, pid, 400, error="Choose whether the money arrived. Nothing was changed.", note=note)
    if not 10 <= len(note) <= 450:
        return _pledge_page(request, user, pid, 400, error="Write a note of 10 to 450 characters on what you found. Nothing was changed.", note=note)
    new, event = OUTCOMES[outcome]
    with engine.begin() as conn:
        row = conn.execute(text("UPDATE pledges SET status = :s WHERE id = :p AND status = 'disputed' RETURNING need_id"),
                           {"s": new, "p": pid}).one_or_none()
        if row is not None:
            conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, :e, :t)"),
                         {"n": row.need_id, "a": user.id, "e": event, "t": f"pledge {pid}: {note}"})
    if row is None:
        return _pledge_page(request, user, pid, 409, error="This pledge is not disputed (any more). Nothing was changed.")
    return RedirectResponse("/verify?done=resolved", status_code=303)


CLAIM_STATUS = {"pledged": "Waiting for the contact to be released", "sent": "Handed over, waiting for the requester",
                "confirmed": "Confirmed by the requester", "disputed": "Disputed", "expired": "Expired", "cancelled": "Cancelled"}


def _claim_for_review(cid: int):
    with engine.connect() as c:
        return c.execute(text(
            "SELECT k.id, k.need_id, k.units, k.status, k.contact_method AS d_method, k.contact AS d_contact, k.created_at, "
            "k.expires_at, k.released_at, k.donor_id, ud.username AS donor, ur.username AS requester, "
            "n.contact_method AS r_method, n.contact AS r_contact, n.province, n.district, n.quantity_needed, "
            "n.handover_method, t.name AS item_name "
            "FROM item_claims k JOIN users ud ON ud.id = k.donor_id JOIN needs n ON n.id = k.need_id "
            "JOIN users ur ON ur.id = n.requester_id LEFT JOIN item_types t ON t.id = n.item_type_id WHERE k.id = :c"),
            {"c": cid}).one_or_none()


def _claim_page(request, user, k, status_code=200, error=None, reason=""):
    with engine.connect() as c:
        earlier = c.execute(text("SELECT status, count(*) AS cnt FROM item_claims WHERE donor_id = :d AND id <> :i "
                                 "GROUP BY status ORDER BY status"), {"d": k.donor_id, "i": k.id}).all()
    history = ", ".join(f"{e.cnt} {e.status}" for e in earlier) or "none"
    status_text = CLAIM_STATUS[k.status] if not (k.status == "pledged" and k.released_at is not None) else "Contact released"
    return render(request, "verify_claim.html", status_code=status_code, user=user, k=k, history=history,
                  status_text=status_text, error=error, reason=reason, **_claim_extras(k),
                  handover_label=dict(items.HANDOVER_METHODS).get(k.handover_method, "").replace(" (recommended)", ""))


@router.get("/verify/claims/{cid}")
def verify_claim(request: Request, cid: int, user=Depends(require_role("verifier"))):
    _expire()
    k = _claim_for_review(cid)
    if k is None:
        raise HTTPException(status_code=404, detail="not found")
    return _claim_page(request, user, k)


@router.post("/verify/claims/{cid}/release")
def verify_claim_release(request: Request, cid: int, csrf: str = Form(...), user=Depends(require_role("verifier"))):
    check_csrf(request, csrf)
    _expire()
    k = _claim_for_review(cid)
    if k is None:
        raise HTTPException(status_code=404, detail="not found")
    with engine.begin() as conn:
        done = conn.execute(
            text("UPDATE item_claims SET released_at = now(), released_by = :v WHERE id = :c AND status = 'pledged' "
                 "AND released_at IS NULL RETURNING need_id"), {"v": user.id, "c": cid}).one_or_none()
        if done is None:
            return _claim_page(request, user, k, 409, error="This offer was already released, declined or cancelled, or it has expired. Nothing was changed.")
        conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'claim_released', :p)"),
                     {"n": done.need_id, "a": user.id, "p": f"claim {cid}"})
    return RedirectResponse("/verify?done=released", status_code=303)


@router.post("/verify/claims/{cid}/decline")
def verify_claim_decline(request: Request, cid: int, reason: str = Form(""), csrf: str = Form(...),
                         user=Depends(require_role("verifier"))):
    check_csrf(request, csrf)
    _expire()
    k = _claim_for_review(cid)
    if k is None:
        raise HTTPException(status_code=404, detail="not found")
    reason = reason.strip()
    if not 10 <= len(reason) <= 450:
        return _claim_page(request, user, k, 400, error="Write a reason of 10 to 450 characters. The donor will see it.", reason=reason)
    with engine.begin() as conn:
        done = conn.execute(
            text("UPDATE item_claims SET status = 'cancelled' WHERE id = :c AND status = 'pledged' AND released_at IS NULL "
                 "RETURNING need_id"), {"c": cid}).one_or_none()
        if done is None:
            return _claim_page(request, user, k, 409, error="This offer was already released, declined or cancelled, or it has expired. Nothing was changed.")
        conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'claim_declined', :p)"),
                     {"n": done.need_id, "a": user.id, "p": f"claim {cid}: {reason}"})
    return RedirectResponse("/verify?done=declined", status_code=303)


def _claim_extras(k):
    with engine.connect() as c:
        proof = c.execute(text(
            "SELECT (file_ref IS NOT NULL AND deleted_at IS NULL) AS has_photo, (deleted_at IS NOT NULL) AS gone, waived_reason "
            "FROM handover_proofs WHERE claim_id = :c"), {"c": k.id}).one_or_none()
        note = c.execute(text(
            "SELECT e.note FROM need_events e WHERE e.need_id = :n AND e.event = 'claim_disputed' AND starts_with(e.note, :p) "
            "ORDER BY e.id DESC LIMIT 1"), {"n": k.need_id, "p": f"claim {k.id}: "}).scalar()
    with engine.connect() as c:
        overdue = c.execute(text("SELECT (status = 'sent' AND sent_at < now() - make_interval(days => :d)) "
                                 "FROM item_claims WHERE id = :c"), {"c": k.id, "d": int(UNANSWERED_DAYS)}).scalar()
    return {"proof": proof, "dispute": note.split(": ", 1)[1] if note else None,
            "overdue": bool(overdue), "wait_days": UNANSWERED_DAYS}


@router.post("/verify/claims/{cid}/waive")
def verify_claim_waive(request: Request, cid: int, reason: str = Form(""), csrf: str = Form(...),
                       user=Depends(require_role("verifier"))):
    check_csrf(request, csrf)
    _expire()
    k = _claim_for_review(cid)
    if k is None:
        raise HTTPException(status_code=404, detail="not found")
    reason = reason.strip()
    if not 10 <= len(reason) <= 500:
        return _claim_page(request, user, k, 400, error="Write a reason of 10 to 500 characters.")
    with engine.begin() as conn:
        done = conn.execute(text(
            "INSERT INTO handover_proofs (claim_id, waived_reason) SELECT CAST(:c AS bigint), CAST(:r AS text) "
            "WHERE EXISTS (SELECT 1 FROM item_claims WHERE id = :c AND status = 'pledged' AND released_at IS NOT NULL) "
            "ON CONFLICT (claim_id) DO NOTHING RETURNING id"), {"c": cid, "r": reason}).first()
        if done is None:
            return _claim_page(request, user, k, 409, error="The photo cannot be waived for this offer now (not released, already handed over, or already waived). Nothing was changed.")
        conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'claim_photo_waived', :p)"),
                     {"n": k.need_id, "a": user.id, "p": f"claim {cid}: {reason}"})
    return RedirectResponse("/verify?done=waived", status_code=303)


@router.post("/verify/claims/{cid}/settle")
def verify_claim_settle(request: Request, cid: int, outcome: str = Form(""), reason: str = Form(""), csrf: str = Form(...),
                        user=Depends(require_role("verifier"))):
    check_csrf(request, csrf)
    _expire()
    k = _claim_for_review(cid)
    if k is None:
        raise HTTPException(status_code=404, detail="not found")
    reason = reason.strip()
    if outcome not in ("confirm", "cancel"):
        return _claim_page(request, user, k, 400, error="Choose how to settle the dispute.")
    if not 10 <= len(reason) <= 450:
        return _claim_page(request, user, k, 400, error="Write a reason of 10 to 450 characters.")
    new = "confirmed" if outcome == "confirm" else "cancelled"
    with engine.begin() as conn:
        done = conn.execute(text("UPDATE item_claims SET status = :s WHERE id = :c AND status = 'disputed' RETURNING need_id"),
                            {"s": new, "c": cid}).one_or_none()
        if done is None:
            return _claim_page(request, user, k, 409, error="This offer is not in dispute any more. Nothing was changed.")
        conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'claim_settled', :p)"),
                     {"n": done.need_id, "a": user.id, "p": f"claim {cid}: {outcome}: {reason}"})
        conn.execute(text(KEEP_SQL), {"c": cid})
    return RedirectResponse("/verify?done=settled", status_code=303)


@router.get("/verify/claims/{cid}/photo")
def verify_claim_photo(cid: int, user=Depends(require_role("verifier"))):
    with engine.connect() as c:
        ref = c.execute(text("SELECT file_ref FROM handover_proofs WHERE claim_id = :c AND deleted_at IS NULL"), {"c": cid}).scalar()
    if ref is None:
        raise HTTPException(status_code=404, detail="not found")
    return photo_response(ref)


@router.post("/verify/claims/{cid}/chase")
def verify_claim_chase(request: Request, cid: int, outcome: str = Form(""), reason: str = Form(""), csrf: str = Form(...),
                       user=Depends(require_role("verifier"))):
    check_csrf(request, csrf)
    _expire()
    k = _claim_for_review(cid)
    if k is None:
        raise HTTPException(status_code=404, detail="not found")
    reason = reason.strip()
    if outcome not in ("confirm", "dispute"):
        return _claim_page(request, user, k, 400, error="Choose whether to confirm the handover or open a dispute.")
    if not 10 <= len(reason) <= 450:
        return _claim_page(request, user, k, 400, error="Write a reason of 10 to 450 characters.")
    new = "confirmed" if outcome == "confirm" else "disputed"
    with engine.begin() as conn:
        done = conn.execute(text(
            "UPDATE item_claims SET status = :s WHERE id = :c AND status = 'sent' AND sent_at < now() - make_interval(days => :d) "
            "RETURNING need_id"), {"s": new, "c": cid, "d": int(UNANSWERED_DAYS)}).one_or_none()
        if done is None:
            return _claim_page(request, user, k, 409, error="This offer is not waiting for the requester any more, or it was handed over too recently. Nothing was changed.")
        if outcome == "confirm":
            conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'claim_confirmed', :p)"),
                         {"n": done.need_id, "a": user.id, "p": f"claim {cid}: confirmed by a verifier: {reason}"})
            conn.execute(text(KEEP_SQL), {"c": cid})
        else:
            conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'claim_disputed', :p)"),
                         {"n": done.need_id, "a": user.id, "p": f"claim {cid}: {reason} (opened by a verifier)"})
    return RedirectResponse("/verify?done=chased", status_code=303)
