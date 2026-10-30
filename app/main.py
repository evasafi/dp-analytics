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
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

CSRF_COOKIE = "csrf"
_csrf_signer = URLSafeSerializer(settings.secret_key, salt="csrf")


def _read_csrf(request: Request):
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie:
        return None
    try:
        return _csrf_signer.loads(cookie)
    except BadSignature:
        return None


def render(request: Request, name: str, status_code: int = 200, **ctx):
    raw = _read_csrf(request)
    new_cookie = None
    if raw is None:
        raw = secrets.token_urlsafe(24)
        new_cookie = _csrf_signer.dumps(raw)
    resp = templates.TemplateResponse(
        request, name, {"csrf": raw, **ctx}, status_code=status_code
    )
    if new_cookie:
        resp.set_cookie(CSRF_COOKIE, new_cookie, httponly=True, samesite="strict")
    return resp


def check_csrf(request: Request, form_token: str):
    raw = _read_csrf(request)
    if raw is None or not secrets.compare_digest(raw, form_token):
        raise HTTPException(status_code=403, detail="bad CSRF token")


@app.get("/")
def root(request: Request):
    try:
        user = current_user(request)
    except HTTPException:
        return RedirectResponse("/login", status_code=303)
    home = {"analyst": "/analyst", "field_agent": "/field", "admin": "/admin", "requester": "/needs/mine", "verifier": "/verify"}
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
from app.queries import (REGIONS, Refused, count_by_region,
                         household_histogram, mean_transfer)

DATASET_ID = 1


def _budget():
    with engine.connect() as c:
        row = c.execute(
            text("SELECT epsilon_total, epsilon_spent FROM datasets WHERE id = :d"),
            {"d": DATASET_ID},
        ).one()
    return {"total": row.epsilon_total, "spent": row.epsilon_spent,
            "remaining": row.epsilon_total - row.epsilon_spent}


def _analyst_page(request: Request, user, status_code: int = 200, **ctx):
    costs = {"count": settings.epsilon_per_count,
             "histogram": settings.epsilon_per_histogram,
             "mean": settings.epsilon_per_mean}
    return render(request, "analyst.html", status_code=status_code, user=user,
                  budget=_budget(), regions=REGIONS, costs=costs, **ctx)


@app.get("/analyst")
def analyst_page(request: Request, user=Depends(require_role("analyst"))):
    return _analyst_page(request, user)


# ---------------- field agent routes ----------------
import json
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError


def _valid_submission(region, household_size, transfer_text):
    """Return the transfer as a Decimal if everything is valid, else None."""
    try:
        transfer = Decimal(transfer_text).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    if not transfer.is_finite():
        return None
    if region not in REGIONS or not 1 <= household_size <= 12:
        return None
    if not Decimal(0) <= transfer <= Decimal(500):
        return None
    return transfer


@app.get("/field")
def field_page(request: Request, done: int = 0,
               user=Depends(require_role("field_agent"))):
    return render(request, "field.html", user=user, done=bool(done), regions=REGIONS)


@app.post("/field/submit")
def field_submit(request: Request, region: str = Form(...),
                 household_size: int = Form(...), monthly_transfer: str = Form(...),
                 csrf: str = Form(...), user=Depends(require_role("field_agent"))):
    check_csrf(request, csrf)
    transfer = _valid_submission(region, household_size, monthly_transfer)
    if transfer is None:
        return render(request, "field.html", status_code=400, user=user, done=False,
                      regions=REGIONS, error="Invalid values. Nothing was saved.")
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": 7000000 + user.id})
            used = conn.execute(
                text("SELECT count(*) FROM audit_log WHERE user_id = :u AND "
                     "query_type = 'submit' AND ts > now() - interval '24 hours'"),
                {"u": user.id}).scalar_one()
            if used >= settings.field_daily_cap:
                return render(request, "field.html", status_code=429, user=user, done=False,
                              regions=REGIONS,
                              error="Daily submission limit reached. Nothing was saved.")
            conn.execute(
                text("INSERT INTO beneficiaries (dataset_id, beneficiary_id, region, "
                     "household_size, monthly_transfer) VALUES (:d, :b, :r, :h, :t)"),
                {"d": DATASET_ID, "b": secrets.token_hex(8), "r": region,
                 "h": household_size, "t": transfer},
            )
            conn.execute(
                text("INSERT INTO audit_log (user_id, dataset_id, query_type, params, "
                     "epsilon_charged, result_hash) VALUES "
                     "(:u, :d, 'submit', CAST(:p AS jsonb), 0, 'n/a')"),
                {"u": user.id, "d": DATASET_ID, "p": json.dumps({})},
            )
    except IntegrityError:
        return render(request, "field.html", status_code=500, user=user, done=False,
                      regions=REGIONS, error="Could not save. Please try again.")
    return RedirectResponse("/field?done=1", status_code=303)


# ---------------- admin routes ----------------
@app.get("/admin")
def admin_page(request: Request, user=Depends(require_role("admin"))):
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT a.ts, u.username, a.query_type, a.params, a.epsilon_charged, "
            "a.result_hash FROM audit_log a JOIN users u ON u.id = a.user_id "
            "ORDER BY a.id DESC LIMIT 50")).all()
    return render(request, "admin.html", user=user, budget=_budget(), rows=rows)



# ---------------- engine routes ----------------
import csv
import io

from fastapi.responses import Response

from app.engine import Spec, validate
from app.engine_noise import SUPPRESS_BELOW, estimate
from app.engine_run import run_query


def _build_spec(measure, group_by, f_region, f_household_size, f_transfer_band):
    filters = []
    if f_region:
        filters.append(("region", tuple(f_region)))
    if f_household_size:
        filters.append(("household_size", tuple(int(v) for v in f_household_size)))
    if f_transfer_band:
        filters.append(("transfer_band", tuple(int(v) for v in f_transfer_band)))
    group_by = [g for g in group_by if g]   # blank dropdowns mean no grouping
    spec = Spec(measure, tuple(group_by), tuple(filters))
    validate(spec)
    return spec


def _fields(measure, group_by, f_region, f_household_size, f_transfer_band):
    out = [("measure", measure)]
    out += [("group_by", g) for g in group_by]
    out += [("f_region", v) for v in f_region]
    out += [("f_household_size", v) for v in f_household_size]
    out += [("f_transfer_band", v) for v in f_transfer_band]
    return out


@app.post("/engine/preview")
def engine_preview(request: Request, measure: str = Form(...),
                   group_by: list[str] = Form(default=[]),
                   f_region: list[str] = Form(default=[]),
                   f_household_size: list[str] = Form(default=[]),
                   f_transfer_band: list[str] = Form(default=[]),
                   csrf: str = Form(...), user=Depends(require_role("analyst"))):
    check_csrf(request, csrf)
    try:
        spec = _build_spec(measure, group_by, f_region, f_household_size, f_transfer_band)
    except ValueError:
        return _analyst_page(request, user, status_code=400,
                             error="Invalid query. Nothing was charged.")
    est = estimate(spec)
    budget = _budget()
    after = budget["remaining"] - Decimal(str(est["epsilon"]))
    return render(request, "engine_preview.html", user=user, est=est,
                  remaining=budget["remaining"], after=after,
                  fields=_fields(measure, group_by, f_region, f_household_size, f_transfer_band))


@app.post("/engine/run")
def engine_run(request: Request, measure: str = Form(...),
               group_by: list[str] = Form(default=[]),
               f_region: list[str] = Form(default=[]),
               f_household_size: list[str] = Form(default=[]),
               f_transfer_band: list[str] = Form(default=[]),
               csrf: str = Form(...), user=Depends(require_role("analyst"))):
    check_csrf(request, csrf)
    try:
        spec = _build_spec(measure, group_by, f_region, f_household_size, f_transfer_band)
        with engine.begin() as conn:
            res = run_query(conn, user.id, DATASET_ID, spec)
    except BudgetExhausted:
        return _analyst_page(request, user, status_code=409,
                             error="Privacy budget exhausted. No answer was released.")
    except ValueError:
        return _analyst_page(request, user, status_code=400,
                             error="Invalid query. Nothing was charged.")
    return RedirectResponse(f"/engine/result/{res['result_id']}", status_code=303)


def _load_result(rid: int, user):
    with engine.connect() as c:
        row = c.execute(
            text("SELECT id, spec, cells FROM results WHERE id = :r AND user_id = :u"),
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
                  dims=row.spec["group_by"], rows=rows, suppress_below=SUPPRESS_BELOW)


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
            "SELECT r.id, r.ts, r.spec, a.epsilon_charged FROM results r "
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


@app.get("/needs/new")
def need_new_page(request: Request, user=Depends(require_role("requester"))):
    return render(request, "need_new.html", user=user, categories=CATEGORIES, provinces=PROVINCES, districts_by_province=DISTRICTS_BY_PROVINCE)


@app.post("/needs/new")
def need_new(request: Request, category: str = Form(...), province: str = Form(...),
             district: str = Form(...), description: str = Form(...), contact: str = Form(...),
             csrf: str = Form(...), user=Depends(require_role("requester"))):
    check_csrf(request, csrf)
    province, district = province.strip(), district.strip()
    description, contact = description.strip(), contact.strip()
    ok = (category in dict(CATEGORIES) and province in PROVINCE_NAMES and district in DISTRICTS_BY_PROVINCE.get(province, ())
          and 10 <= len(description) <= 1000 and 3 <= len(contact) <= 200)
    if not ok:
        return render(request, "need_new.html", status_code=400, user=user, categories=CATEGORIES, provinces=PROVINCES, districts_by_province=DISTRICTS_BY_PROVINCE,
                      error="Check the fields. Nothing was saved.")
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": 8000000 + user.id})
        pending = conn.execute(
            text("SELECT count(*) FROM needs WHERE requester_id = :u AND status = 'pending'"),
            {"u": user.id}).scalar_one()
        if pending >= MAX_PENDING:
            return render(request, "need_new.html", status_code=429, user=user, categories=CATEGORIES, provinces=PROVINCES, districts_by_province=DISTRICTS_BY_PROVINCE,
                          error=f"You already have {MAX_PENDING} needs waiting for review. Please wait for a verifier.")
        nid = conn.execute(
            text("INSERT INTO needs (requester_id, category, province, district, description, contact) "
                 "VALUES (:u, :c, :p, :d, :t, :k) RETURNING id"),
            {"u": user.id, "c": category, "p": province, "d": district, "t": description, "k": contact}).scalar_one()
        conn.execute(text("INSERT INTO need_events (need_id, actor_id, event) VALUES (:n, :u, 'submitted')"),
                     {"n": nid, "u": user.id})
    return RedirectResponse("/needs/mine?done=1", status_code=303)


@app.get("/needs/mine")
def needs_mine(request: Request, done: int = 0, user=Depends(require_role("requester"))):
    with engine.connect() as c:
        rows = c.execute(
            text("SELECT created_at, category, province, district, status, description, review_note "
                 "FROM needs WHERE requester_id = :u ORDER BY id DESC"), {"u": user.id}).all()
    return render(request, "needs_mine.html", user=user, rows=rows, done=bool(done))

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
            text("SELECT id, requester_id, category, province, district, description, contact, status, created_at "
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