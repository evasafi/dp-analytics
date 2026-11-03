"""Verifier pages: the review queue, a need's private details, approve or reject, and the look-again list."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from app.auth import require_role
from app.db import engine
from app.reference import STATUS_TEXT, money, need_label
from app.validation import public_text_problem
from app.web import check_csrf, render

router = APIRouter()
CHECKS_NOTE = ("Checked outside the platform: the account holder's name matches the requester's identity; "
               "the supporting document was seen and matches the goal.")


def _need_for_review(nid: int):
    with engine.connect() as c:
        return c.execute(text(
            "SELECT n.id, n.requester_id, u.username, n.purpose, n.medical_location, n.event, n.province, n.district, "
            "n.household_size, n.currency, n.goal, n.description, n.contact, n.contact_method, n.payee_name, "
            "n.payee_details, n.status, n.created_at, n.review_note, n.withdrawn_reason "
            "FROM needs n JOIN users u ON u.id = n.requester_id WHERE n.id = :i"), {"i": nid}).one_or_none()


def _detail_page(request, user, n, status_code=200, draft=None, error=None, payee_ok=False, document_ok=False):
    label = need_label(n.purpose, n.medical_location)
    with engine.connect() as c:
        earlier = c.execute(text("SELECT status, count(*) AS k FROM needs WHERE requester_id = :u AND id <> :i GROUP BY status ORDER BY status"),
                            {"u": n.requester_id, "i": n.id}).all()
    history = ", ".join(f"{e.k} {STATUS_TEXT[e.status].lower()}" for e in earlier) or "none"
    return render(request, "verify_detail.html", status_code=status_code, user=user, n=n, label=label,
                  goal=money(n.goal, n.currency), iban=" ".join(n.payee_details[i:i + 4] for i in range(0, len(n.payee_details), 4)),
                  status_text=STATUS_TEXT[n.status], history=history,
                  draft=draft if draft is not None else f"{label} need in {n.district}, {n.province}.",
                  error=error, payee_ok=payee_ok, document_ok=document_ok)


@router.get("/verify")
def verify_queue(request: Request, done: str = "", user=Depends(require_role("verifier"))):
    with engine.connect() as c:
        pending = c.execute(text(
            "SELECT id, created_at, purpose, medical_location, province, district, currency, goal, "
            "left(description, 140) AS snippet FROM needs WHERE status = 'pending' ORDER BY id LIMIT 50")).all()
        again = c.execute(text(
            "SELECT n.id, n.purpose, n.medical_location, n.province, n.district, n.currency, n.withdrawn_reason, p.confirmed "
            "FROM needs n JOIN (SELECT need_id, sum(amount_need) AS confirmed FROM pledges WHERE status = 'confirmed' "
            "GROUP BY need_id) p ON p.need_id = n.id WHERE n.status = 'withdrawn' ORDER BY n.withdrawn_at DESC LIMIT 50")).all()
    rows = [{"id": r.id, "created": r.created_at.strftime("%Y-%m-%d %H:%M"), "label": need_label(r.purpose, r.medical_location),
             "place": f"{r.district}, {r.province}", "goal": money(r.goal, r.currency), "snippet": r.snippet} for r in pending]
    flags = [{"id": r.id, "label": need_label(r.purpose, r.medical_location), "place": f"{r.district}, {r.province}",
              "confirmed": money(r.confirmed, r.currency), "reason": r.withdrawn_reason} for r in again]
    return render(request, "verify_queue.html", user=user, rows=rows, flags=flags, done=done)


@router.get("/verify/{nid}")
def verify_detail(request: Request, nid: int, user=Depends(require_role("verifier"))):
    n = _need_for_review(nid)
    if n is None:
        raise HTTPException(status_code=404, detail="not found")
    return _detail_page(request, user, n)


@router.post("/verify/{nid}/approve")
def verify_approve(request: Request, nid: int, public_text: str = Form(""), check_payee: str = Form(""),
                   check_document: str = Form(""), csrf: str = Form(...), user=Depends(require_role("verifier"))):
    check_csrf(request, csrf)
    n = _need_for_review(nid)
    if n is None:
        raise HTTPException(status_code=404, detail="not found")
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
    return RedirectResponse("/verify?done=approved", status_code=303)


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
