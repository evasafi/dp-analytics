"""Verifier pages: the review queue, a need's private details, approve or reject, the look-again list, and pledges
that were disputed or left unanswered."""
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
UNANSWERED_DAYS = 14  # a pledge marked sent that the requester has not answered for this long is listed for follow-up
OUTCOMES = {"arrived": ("confirmed", "dispute_arrived"), "not_arrived": ("cancelled", "dispute_not_arrived")}
PLEDGE_LIST = ("SELECT p.id, p.amount_try, p.amount_need, p.sent_at, n.purpose, n.medical_location, n.province, n.district, "
               "n.currency FROM pledges p JOIN needs n ON n.id = p.need_id WHERE {} ORDER BY p.sent_at LIMIT 50")


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
        disputed = c.execute(text(PLEDGE_LIST.format("p.status = 'disputed'"))).all()
        unanswered = c.execute(text(PLEDGE_LIST.format("p.status = 'sent' AND p.sent_at < now() - make_interval(days => :d)")),
                               {"d": UNANSWERED_DAYS}).all()
    rows = [{"id": r.id, "created": r.created_at.strftime("%Y-%m-%d %H:%M"), "label": need_label(r.purpose, r.medical_location),
             "place": f"{r.district}, {r.province}", "goal": money(r.goal, r.currency), "snippet": r.snippet} for r in pending]
    flags = [{"id": r.id, "label": need_label(r.purpose, r.medical_location), "place": f"{r.district}, {r.province}",
              "confirmed": money(r.confirmed, r.currency), "reason": r.withdrawn_reason} for r in again]
    return render(request, "verify_queue.html", user=user, rows=rows, flags=flags, done=done,
                  disputed=_pledge_rows(disputed), unanswered=_pledge_rows(unanswered), days=UNANSWERED_DAYS)


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
    events = [{"ts": e.ts.strftime("%Y-%m-%d %H:%M"), "event": e.event, "who": e.username,
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
