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
    home = {"analyst": "/analyst", "field_agent": "/field", "admin": "/admin"}
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