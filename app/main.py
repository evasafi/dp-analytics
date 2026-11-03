import secrets
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

from app.auth import COOKIE, MAX_AGE, authenticate, current_user, make_session
from app.config import settings
from app.ratelimit import is_locked, record_failure

app = FastAPI()
from app.web import CSRF_COOKIE, _csrf_signer, _read_csrf, check_csrf, render, templates  # noqa: F401 (moved to app/web.py)


@app.get("/")
def root(request: Request):
    try:
        user = current_user(request)
    except HTTPException:
        return RedirectResponse("/login", status_code=303)
    home = {"analyst": "/analyst", "admin": "/admin", "requester": "/needs/mine", "verifier": "/verify", "supporter": "/support"}
    return RedirectResponse(home.get(user.role, "/me"), status_code=303)


@app.get("/login")
def login_page(request: Request):
    return render(request, "login.html")


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          csrf: str = Form(...)):
    check_csrf(request, csrf)
    ip = request.client.host if request.client else "unknown"
    if is_locked(username, ip):
        return render(request, "login.html", status_code=429,
                      error="Too many failed attempts. Try again in a few minutes.")
    user_id = authenticate(username, password)
    if user_id is None:
        record_failure(username, ip)
        return render(request, "login.html", status_code=401,
                      error="Invalid username or password.")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, make_session(user_id), max_age=MAX_AGE,
                    httponly=True, samesite="strict")
    return resp


@app.post("/logout")
def logout(request: Request, csrf: str = Form(...)):
    check_csrf(request, csrf)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/me")
def me(request: Request):
    user = current_user(request)
    return render(request, "me.html", user=user)


# ---------------- analyst routes ----------------
from fastapi import Depends
from sqlalchemy import text

from app.auth import require_role
from app.budget import BudgetExhausted
from app.db import engine
def _budgets():
    """Both analytics budgets, for the admin and analyst pages."""
    with engine.connect() as c:
        rows = c.execute(text("SELECT name, epsilon_total, epsilon_spent FROM datasets ORDER BY id")).all()
    return [{"name": r.name, "total": r.epsilon_total, "spent": r.epsilon_spent,
             "remaining": r.epsilon_total - r.epsilon_spent} for r in rows]


def _analyst_page(request: Request, user, status_code: int = 200, **ctx):
    return render(request, "analyst.html", status_code=status_code, user=user, budgets=_budgets(), **ctx)


@app.get("/analyst")
def analyst_page(request: Request, user=Depends(require_role("analyst"))):
    return _analyst_page(request, user)


import json
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError

# ---------------- admin routes ----------------
@app.get("/admin")
def admin_page(request: Request, user=Depends(require_role("admin"))):
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT a.ts, u.username, a.query_type, a.params, a.epsilon_charged, "
            "a.result_hash FROM audit_log a JOIN users u ON u.id = a.user_id "
            "ORDER BY a.id DESC LIMIT 50")).all()
    return render(request, "admin.html", user=user, budgets=_budgets(), rows=rows)



# ---------------- engine results ----------------
import csv
import io

from fastapi.responses import Response

from app.engine_noise import SUPPRESS_BELOW

def _load_result(rid: int, user):
    with engine.connect() as c:
        row = c.execute(
            text("SELECT r.id, r.spec, r.cells, a.query_type FROM results r JOIN audit_log a ON a.id = r.audit_id WHERE r.id = :r AND r.user_id = :u"),
            {"r": rid, "u": user.id}).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return row


def _show(v):
    if v is None:
        return "suppressed"
    return v if isinstance(v, int) else round(v, 1)


@app.get("/engine/result/{rid}")
def engine_result(request: Request, rid: int, user=Depends(require_role("analyst"))):
    row = _load_result(rid, user)
    rows = [[*k, _show(v)] for k, v in row.cells]
    return render(request, "engine_result.html", user=user, rid=rid, spec=row.spec,
                  dims=row.spec["group_by"], rows=rows, suppress_below=SUPPRESS_BELOW,
                  is_needs=(row.query_type == "needs_count"))


@app.get("/engine/result/{rid}/csv")
def engine_result_csv(rid: int, user=Depends(require_role("analyst"))):
    row = _load_result(rid, user)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(list(row.spec["group_by"]) + ["value"])
    for k, v in row.cells:
        w.writerow(list(k) + [_show(v)])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="result_{rid}.csv"'})

# ---------------- builder ----------------
from fastapi.staticfiles import StaticFiles


@app.get("/engine/results")
def engine_results(request: Request, user=Depends(require_role("analyst"))):
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT r.id, r.ts, r.spec, a.epsilon_charged, a.query_type FROM results r "
            "JOIN audit_log a ON a.id = r.audit_id WHERE r.user_id = :u "
            "ORDER BY r.id DESC LIMIT 50"), {"u": user.id}).all()
    return render(request, "engine_list.html", user=user, rows=rows)


app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

# ---------------- requesters and supporters ----------------
import re as _re

from app.auth import hash_password
from app.ratelimit import record_registration, registration_blocked

SELF_ROLES = ("requester", "supporter")
USERNAME_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,29}$")
CATEGORIES = [("baby_child", "Baby and child"), ("education", "Education"),
              ("household", "Household goods"), ("electronics", "Electronics"),
              ("clothing", "Clothing"), ("culture_art", "Culture and art"),
              ("health", "Health"), ("street_animals", "Street animals"),
              ("sports", "Sports"), ("hygiene", "Cleaning and hygiene")]
MAX_PENDING = 3
from app.districts import DISTRICTS_BY_PROVINCE
from app.provinces import PROVINCE_NAMES, PROVINCES


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@app.get("/register")
def register_page(request: Request):
    return render(request, "register.html")


@app.post("/register")
def register(request: Request, username: str = Form(...), password: str = Form(...),
             role: str = Form(...), csrf: str = Form(...)):
    check_csrf(request, csrf)
    if registration_blocked(_client_ip(request)):
        return render(request, "register.html", status_code=429,
                      error="Too many accounts created from this address. Try again later.")
    if role not in SELF_ROLES or not USERNAME_RE.match(username) or not 12 <= len(password) <= 200:
        return render(request, "register.html", status_code=400,
                      error="Check the username (3 to 30 characters) and the password (12 or more).")
    try:
        with engine.begin() as conn:
            uid = conn.execute(
                text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r) RETURNING id"),
                {"u": username, "h": hash_password(password), "r": role}).scalar_one()
    except IntegrityError:
        return render(request, "register.html", status_code=400,
                      error="That username is not available.")
    record_registration(_client_ip(request))
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, make_session(uid), max_age=MAX_AGE, httponly=True, samesite="strict")
    return resp


from app.routes import needs as needs_routes

app.include_router(needs_routes.router)


# ---------------- verifiers ----------------
_PHONE_RE = _re.compile(r"(?:\d[\s\-().+]*){7,}")


def _public_text_problem(public_text: str, contact: str):
    t = public_text.strip()
    if not 10 <= len(t) <= 300:
        return "The public text must be 10 to 300 characters."
    if "@" in t or "://" in t or "www." in t.lower():
        return "The public text must not contain an email address or a link."
    if _PHONE_RE.search(t):
        return "The public text looks like it contains a phone number. Remove digits that could identify someone."
    if len(contact) >= 3 and contact.lower() in t.lower():
        return "The public text contains the requester's contact details."
    return None


def _need_for_review(nid: int):
    with engine.connect() as c:
        return c.execute(
            text("SELECT id, requester_id, category, province, district, description, contact, contact_method, status, created_at "
                 "FROM needs WHERE id = :i"), {"i": nid}).one_or_none()


def _verify_detail(request, user, n, status_code=200, draft=None, error=None):
    labels = dict(CATEGORIES)
    default = f"{labels[n.category]} need in {n.district}, {n.province}."
    return render(request, "verify_detail.html", status_code=status_code, user=user, n=n,
                  label=labels[n.category], draft=draft if draft is not None else default, error=error)


@app.get("/verify")
def verify_queue(request: Request, done: str = "", user=Depends(require_role("verifier"))):
    with engine.connect() as c:
        rows = c.execute(
            text("SELECT id, created_at, category, province, district, left(description, 140) AS snippet "
                 "FROM needs WHERE status = 'pending' ORDER BY id LIMIT 50")).all()
    return render(request, "verify_queue.html", user=user, rows=rows, done=done, labels=dict(CATEGORIES))


@app.get("/verify/{nid}")
def verify_detail(request: Request, nid: int, user=Depends(require_role("verifier"))):
    n = _need_for_review(nid)
    if n is None:
        raise HTTPException(status_code=404, detail="not found")
    return _verify_detail(request, user, n)


@app.post("/verify/{nid}/approve")
def verify_approve(request: Request, nid: int, public_text: str = Form(...), csrf: str = Form(...),
                   user=Depends(require_role("verifier"))):
    check_csrf(request, csrf)
    n = _need_for_review(nid)
    if n is None:
        raise HTTPException(status_code=404, detail="not found")
    problem = _public_text_problem(public_text, n.contact)
    if problem:
        return _verify_detail(request, user, n, status_code=400, draft=public_text, error=problem)
    with engine.begin() as conn:
        done = conn.execute(
            text("UPDATE needs SET status = 'published', public_text = :t, reviewed_by = :v, "
                 "reviewed_at = now(), review_note = NULL, updated_at = now() "
                 "WHERE id = :i AND status = 'pending' AND requester_id <> :v RETURNING id"),
            {"t": public_text.strip(), "v": user.id, "i": nid}).one_or_none()
        if done is None:
            return _verify_detail(request, user, n, status_code=409,
                                  error="This need is no longer pending. Nothing was changed.")
        conn.execute(text("INSERT INTO need_events (need_id, actor_id, event) VALUES (:n, :a, 'approved')"),
                     {"n": nid, "a": user.id})
    return RedirectResponse("/verify?done=approved", status_code=303)


@app.post("/verify/{nid}/reject")
def verify_reject(request: Request, nid: int, note: str = Form(...), csrf: str = Form(...),
                  user=Depends(require_role("verifier"))):
    check_csrf(request, csrf)
    n = _need_for_review(nid)
    if n is None:
        raise HTTPException(status_code=404, detail="not found")
    note = note.strip()
    if not 3 <= len(note) <= 500:
        return _verify_detail(request, user, n, status_code=400,
                              error="Write a reason of 3 to 500 characters.")
    with engine.begin() as conn:
        done = conn.execute(
            text("UPDATE needs SET status = 'rejected', reviewed_by = :v, reviewed_at = now(), "
                 "review_note = :r, updated_at = now() "
                 "WHERE id = :i AND status = 'pending' AND requester_id <> :v RETURNING id"),
            {"v": user.id, "r": note, "i": nid}).one_or_none()
        if done is None:
            return _verify_detail(request, user, n, status_code=409,
                                  error="This need is no longer pending. Nothing was changed.")
        conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'rejected', :r)"),
                     {"n": nid, "a": user.id, "r": note})
    return RedirectResponse("/verify?done=rejected", status_code=303)

# ---------------- public board ----------------
import threading
import time
from collections import deque
from urllib.parse import urlencode

from fastapi import Query
from fastapi.responses import JSONResponse

PAGE_SIZE = 20
BOARD_LIMIT, BOARD_WINDOW = 60, 60  # requests per window (seconds), per address, in memory
_RATE: dict = {}
_RATE_LOCK = threading.Lock()


def _board_rate_ok(ip: str) -> bool:
    now = time.monotonic()
    with _RATE_LOCK:
        if len(_RATE) > 5000:
            for k in [k for k, d in _RATE.items() if not d or now - d[-1] > BOARD_WINDOW]:
                del _RATE[k]
        d = _RATE.setdefault(ip, deque())
        while d and now - d[0] > BOARD_WINDOW:
            d.popleft()
        if len(d) >= BOARD_LIMIT:
            return False
        d.append(now)
        return True


def _too_many():
    return Response("Too many requests. Try again in a minute.", status_code=429, media_type="text/plain")


def _bad_filter(category: str, province: str) -> bool:
    return (bool(category) and category not in dict(CATEGORIES)) or (bool(province) and province not in PROVINCE_NAMES)


@app.get("/board")
def board(request: Request, category: str = "", province: str = "",
          page: int = Query(1, ge=1, le=1000)):
    if not _board_rate_ok(_client_ip(request)):
        return _too_many()
    if _bad_filter(category, province):
        return Response("Unknown filter.", status_code=400, media_type="text/plain")
    where, params = "status = 'published'", {}
    if category:
        where += " AND category = :c"
        params["c"] = category
    if province:
        where += " AND province = :p"
        params["p"] = province
    with engine.connect() as c:
        total = c.execute(text(f"SELECT count(*) FROM needs WHERE {where}"), params).scalar_one()
        rows = c.execute(
            text("SELECT category, province, district, public_text, to_char(created_at, 'YYYY-MM') AS month "
                 f"FROM needs WHERE {where} ORDER BY id DESC LIMIT :lim OFFSET :off"),
            {**params, "lim": PAGE_SIZE, "off": (page - 1) * PAGE_SIZE}).all()

    def link(n):
        q = {k: v for k, v in (("category", category), ("province", province)) if v}
        if n > 1:
            q["page"] = n
        return "/board" + ("?" + urlencode(q) if q else "")

    return templates.TemplateResponse(request, "board.html", {
        "rows": rows, "total": total, "category": category, "province": province,
        "categories": CATEGORIES, "provinces": PROVINCES, "labels": dict(CATEGORIES),
        "prev_url": link(page - 1) if page > 1 else None,
        "next_url": link(page + 1) if page * PAGE_SIZE < total else None})


@app.get("/board/map-data")
def board_map_data(request: Request, category: str = ""):
    if not _board_rate_ok(_client_ip(request)):
        return _too_many()
    if _bad_filter(category, ""):
        return Response("Unknown filter.", status_code=400, media_type="text/plain")
    sql = "SELECT province, count(*) FROM needs WHERE status = 'published'"
    params = {}
    if category:
        sql += " AND category = :c"
        params["c"] = category
    sql += " GROUP BY province"
    with engine.connect() as c:
        counts = dict(c.execute(text(sql), params).all())
    data = [{"name": n, "pcode": code, "lat": la, "lon": lo, "count": int(counts.get(n, 0))}
            for code, n, la, lo in PROVINCES]
    return JSONResponse(data, headers={"Cache-Control": "public, max-age=60"})

# ---------------- contact details ----------------
_EMAIL_SHAPE = _re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_PHONE_SHAPE = _re.compile(r"^\+?[0-9 ().-]{6,30}$")


def _contact_ok(method: str, contact: str) -> bool:
    if method not in ("phone", "email", "whatsapp", "other") or not 3 <= len(contact) <= 200:
        return False
    if method == "email":
        return bool(_EMAIL_SHAPE.match(contact))
    if method in ("phone", "whatsapp"):
        digits = sum(ch.isdigit() and ch.isascii() for ch in contact)
        return bool(_PHONE_SHAPE.match(contact)) and 7 <= digits <= 15
    return True

# ---------------- supporters ----------------
MAX_PLEDGES = 3
_ACTIVE_PLEDGE = "p.status IN ('pledged', 'handed_over') AND n.status <> 'fulfilled'"


def _my_pledges(user_id: int):
    with engine.connect() as c:
        return c.execute(
            text("SELECT p.id, p.status AS pstatus, p.created_at, n.category, n.province, n.district, "
                 "n.public_text, n.status AS nstatus FROM pledges p JOIN needs n ON n.id = p.need_id "
                 "WHERE p.supporter_id = :u ORDER BY p.id DESC"), {"u": user_id}).all()


def _published_need(nid: int):
    with engine.connect() as c:
        return c.execute(
            text("SELECT id, category, province, district, public_text, to_char(created_at, 'YYYY-MM') AS month "
                 "FROM needs WHERE id = :i AND status = 'published'"), {"i": nid}).one_or_none()


def _pledge_page(request, user, n, status_code=200, error=None):
    return render(request, "pledge_form.html", status_code=status_code, user=user, n=n,
                  label=dict(CATEGORIES)[n.category], error=error)


@app.get("/support")
def support_home(request: Request, category: str = "", province: str = "", done: str = "",
                 page: int = Query(1, ge=1, le=1000), user=Depends(require_role("supporter"))):
    if _bad_filter(category, province):
        return Response("Unknown filter.", status_code=400, media_type="text/plain")
    where, params = "status = 'published'", {}
    if category:
        where += " AND category = :c"
        params["c"] = category
    if province:
        where += " AND province = :p"
        params["p"] = province
    with engine.connect() as c:
        total = c.execute(text(f"SELECT count(*) FROM needs WHERE {where}"), params).scalar_one()
        rows = c.execute(
            text("SELECT id, category, province, district, public_text, to_char(created_at, 'YYYY-MM') AS month "
                 f"FROM needs WHERE {where} ORDER BY id DESC LIMIT :lim OFFSET :off"),
            {**params, "lim": PAGE_SIZE, "off": (page - 1) * PAGE_SIZE}).all()
    pledges = _my_pledges(user.id)
    active = sum(1 for p in pledges if p.pstatus in ("pledged", "handed_over") and p.nstatus != "fulfilled")

    def link(n):
        q = {k: v for k, v in (("category", category), ("province", province)) if v}
        if n > 1:
            q["page"] = n
        return "/support" + ("?" + urlencode(q) if q else "")

    return render(request, "support.html", user=user, rows=rows, total=total, category=category,
                  province=province, categories=CATEGORIES, provinces=PROVINCES, labels=dict(CATEGORIES),
                  pledges=pledges, active=active, max_pledges=MAX_PLEDGES, done=done,
                  prev_url=link(page - 1) if page > 1 else None,
                  next_url=link(page + 1) if page * PAGE_SIZE < total else None)


@app.get("/support/pledge/{nid}")
def pledge_page(request: Request, nid: int, user=Depends(require_role("supporter"))):
    n = _published_need(nid)
    if n is None:
        raise HTTPException(status_code=404, detail="not found")
    return _pledge_page(request, user, n)


@app.post("/support/pledge/{nid}")
def pledge(request: Request, nid: int, contact_method: str = Form(...), contact: str = Form(...),
           csrf: str = Form(...), user=Depends(require_role("supporter"))):
    check_csrf(request, csrf)
    n = _published_need(nid)
    if n is None:
        raise HTTPException(status_code=404, detail="not found")
    contact = contact.strip()
    problem = _contact_problem(contact_method, contact)
    if problem:
        return _pledge_page_form(request, user, n, 400, problem + " Nothing was saved.",
                                 {"contact_method": contact_method, "contact": contact})
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": 9000000 + user.id})
        active = conn.execute(
            text("SELECT count(*) FROM pledges p JOIN needs n ON n.id = p.need_id "
                 "WHERE p.supporter_id = :u AND " + _ACTIVE_PLEDGE), {"u": user.id}).scalar_one()
        if active >= MAX_PLEDGES:
            return _pledge_page(request, user, n, status_code=429,
                                error=f"You already have {MAX_PLEDGES} active pledges. Nothing was saved.")
        got = conn.execute(
            text("UPDATE needs SET status = 'pledged', updated_at = now() "
                 "WHERE id = :i AND status = 'published' RETURNING id"), {"i": nid}).one_or_none()
        if got is None:
            return _pledge_page(request, user, n, status_code=409, error="This need is no longer available.")
        conn.execute(
            text("INSERT INTO pledges (need_id, supporter_id, contact_method, contact) VALUES (:n, :s, :m, :k)"),
            {"n": nid, "s": user.id, "m": contact_method, "k": contact})
        conn.execute(text("INSERT INTO need_events (need_id, actor_id, event) VALUES (:n, :a, 'pledged')"),
                     {"n": nid, "a": user.id})
    return RedirectResponse("/support?done=pledged", status_code=303)


@app.post("/support/cancel/{pid}")
def pledge_cancel(request: Request, pid: int, csrf: str = Form(...), user=Depends(require_role("supporter"))):
    check_csrf(request, csrf)
    with engine.begin() as conn:
        row = conn.execute(
            text("UPDATE pledges SET status = 'cancelled' "
                 "WHERE id = :p AND supporter_id = :u AND status = 'pledged' RETURNING need_id"),
            {"p": pid, "u": user.id}).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="not found")
        back = conn.execute(
            text("UPDATE needs SET status = 'published', updated_at = now() "
                 "WHERE id = :n AND status = 'pledged' RETURNING id"), {"n": row.need_id}).one_or_none()
        if back is None:
            raise HTTPException(status_code=409, detail="the need could not be released")
        conn.execute(text("INSERT INTO need_events (need_id, actor_id, event) VALUES (:n, :a, 'pledge_cancelled')"),
                     {"n": row.need_id, "a": user.id})
    return RedirectResponse("/support?done=cancelled", status_code=303)

# ---------------- handovers (verifiers) ----------------
class _HandoverConflict(Exception):
    pass


def _handover_page(request, user, status_code=200, done="", error=None):
    with engine.connect() as c:
        rows = c.execute(
            text("SELECT p.id, p.status AS pstatus, p.created_at, p.contact_method AS s_method, p.contact AS s_contact, "
                 "su.username AS supporter, n.id AS nid, n.category, n.province, n.district, n.public_text, "
                 "n.description, n.contact_method AS r_method, n.contact AS r_contact "
                 "FROM pledges p JOIN needs n ON n.id = p.need_id JOIN users su ON su.id = p.supporter_id "
                 "WHERE p.status IN ('pledged', 'handed_over') AND n.status = 'pledged' "
                 "ORDER BY p.id LIMIT 100")).all()
    return render(request, "handovers.html", status_code=status_code, user=user, rows=rows, done=done,
                  error=error, labels=dict(CATEGORIES))


def _pledge_exists(pid: int) -> bool:
    with engine.connect() as c:
        return c.execute(text("SELECT 1 FROM pledges WHERE id = :p"), {"p": pid}).scalar() is not None


def _handover_conflict(request, user, pid: int):
    if not _pledge_exists(pid):
        raise HTTPException(status_code=404, detail="not found")
    return _handover_page(request, user, status_code=409,
                          error="This pledge has already changed. Nothing was done.")


@app.get("/handovers")
def handovers(request: Request, done: str = "", user=Depends(require_role("verifier"))):
    return _handover_page(request, user, done=done)


@app.post("/handovers/{pid}/arrange")
def handover_arrange(request: Request, pid: int, csrf: str = Form(...), user=Depends(require_role("verifier"))):
    check_csrf(request, csrf)
    with engine.begin() as conn:
        row = conn.execute(
            text("UPDATE pledges SET status = 'handed_over' WHERE id = :p AND status = 'pledged' "
                 "AND EXISTS (SELECT 1 FROM needs n WHERE n.id = pledges.need_id AND n.status = 'pledged') "
                 "RETURNING need_id"), {"p": pid}).one_or_none()
        if row is not None:
            conn.execute(text("INSERT INTO need_events (need_id, actor_id, event) VALUES (:n, :a, 'handover_arranged')"),
                         {"n": row.need_id, "a": user.id})
    if row is None:
        return _handover_conflict(request, user, pid)
    return RedirectResponse("/handovers?done=arranged", status_code=303)


@app.post("/handovers/{pid}/fulfil")
def handover_fulfil(request: Request, pid: int, csrf: str = Form(...), user=Depends(require_role("verifier"))):
    check_csrf(request, csrf)
    with engine.begin() as conn:
        row = conn.execute(
            text("UPDATE needs SET status = 'fulfilled', updated_at = now() WHERE status = 'pledged' "
                 "AND id = (SELECT need_id FROM pledges WHERE id = :p AND status = 'handed_over') RETURNING id"),
            {"p": pid}).one_or_none()
        if row is not None:
            conn.execute(text("INSERT INTO need_events (need_id, actor_id, event) VALUES (:n, :a, 'fulfilled')"),
                         {"n": row.id, "a": user.id})
    if row is None:
        return _handover_conflict(request, user, pid)
    return RedirectResponse("/handovers?done=fulfilled", status_code=303)


@app.post("/handovers/{pid}/release")
def handover_release(request: Request, pid: int, note: str = Form(...), csrf: str = Form(...),
                     user=Depends(require_role("verifier"))):
    check_csrf(request, csrf)
    note = note.strip()
    if not 3 <= len(note) <= 500:
        return _handover_page(request, user, status_code=400, error="Write a reason of 3 to 500 characters. Nothing was done.")
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text("UPDATE pledges SET status = 'cancelled' WHERE id = :p AND status IN ('pledged', 'handed_over') "
                     "RETURNING need_id"), {"p": pid}).one_or_none()
            if row is None:
                raise _HandoverConflict()
            back = conn.execute(
                text("UPDATE needs SET status = 'published', updated_at = now() WHERE id = :n AND status = 'pledged' "
                     "RETURNING id"), {"n": row.need_id}).one_or_none()
            if back is None:
                raise _HandoverConflict()
            conn.execute(text("INSERT INTO need_events (need_id, actor_id, event, note) VALUES (:n, :a, 'pledge_released', :r)"),
                         {"n": row.need_id, "a": user.id, "r": note})
    except _HandoverConflict:
        return _handover_conflict(request, user, pid)
    return RedirectResponse("/handovers?done=released", status_code=303)

# ---------------- form errors that keep what was typed ----------------
def _contact_problem(method: str, contact: str):
    if _contact_ok(method, contact):
        return None
    if method == "email":
        return "That does not look like an email address, for example name@example.com."
    if method in ("phone", "whatsapp"):
        return ("A phone or WhatsApp number must have 7 to 15 digits, and may contain only digits, spaces, "
                "brackets, dots and dashes, with an optional leading +. Use Other for anything else.")
    if method == "other":
        return "Write 3 to 200 characters on how a verifier can reach you."
    return "Choose how a verifier can reach you."


def _pledge_page_form(request, user, n, status_code, error, form):
    return render(request, "pledge_form.html", status_code=status_code, user=user, n=n,
                  label=dict(CATEGORIES)[n.category], error=error, form=form)