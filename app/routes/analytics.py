"""Analyst pages: both privacy budgets, a free preview of a query, and running it (which spends budget).

The engine (app/needs_engine.py) does the work; these routes only map form values onto its fixed whitelists.
"""
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from app import needs_engine as ne
from app.auth import require_role
from app.budget import BudgetExhausted
from app.config import settings
from app.db import engine
from app.engine import Spec
from app.web import check_csrf, render

router = APIRouter()
GROUPINGS = {"need_type": (("need_type",), "Need type"), "province": (("province",), "Province"),
             "status": (("status",), "Status (on the board or fully funded)"),
             "need_type_status": (("need_type", "status"), "Need type and status"),
             "band": (("band",), "Goal band (lira needs only)")}
PAGE_DATASETS = ("needs_synthetic", "needs_live")


def budgets():
    """Every analytics budget, for the admin and analyst pages."""
    with engine.connect() as c:
        rows = c.execute(text("SELECT name, epsilon_total, epsilon_spent FROM datasets ORDER BY id")).all()
    return [{"name": r.name, "total": r.epsilon_total, "spent": r.epsilon_spent,
             "remaining": r.epsilon_total - r.epsilon_spent} for r in rows]


def _analyst_page(request: Request, user, status_code: int = 200, **ctx):
    return render(request, "analyst.html", status_code=status_code, user=user, budgets=budgets(),
                  datasets=[(d, ne.DATASET_TEXT[d]) for d in PAGE_DATASETS],
                  groupings=[(k, label) for k, (_, label) in GROUPINGS.items()], cost=settings.epsilon_per_count, **ctx)


def _spec(dataset: str, grouping: str) -> Spec:
    if grouping not in GROUPINGS:
        raise ValueError("unknown grouping")
    spec = Spec("count", GROUPINGS[grouping][0])
    ne.validate_needs(spec, dataset)
    return spec


@router.get("/analyst")
def analyst_page(request: Request, user=Depends(require_role("analyst"))):
    return _analyst_page(request, user)


@router.post("/engine/needs/preview")
def needs_preview(request: Request, dataset: str = Form(""), grouping: str = Form(""), csrf: str = Form(...),
                  user=Depends(require_role("analyst"))):
    check_csrf(request, csrf)
    try:
        spec = _spec(dataset, grouping)
    except ValueError:
        return _analyst_page(request, user, status_code=400, error="Invalid query. Nothing was charged.")
    est = ne.estimate(spec, dataset)
    with engine.connect() as c:
        row = c.execute(text("SELECT epsilon_total - epsilon_spent FROM datasets WHERE name = :n"), {"n": dataset}).scalar_one()
    return render(request, "engine_preview.html", user=user, est=est, remaining=row, after=row - Decimal(str(est["epsilon"])),
                  dataset_text=ne.DATASET_TEXT.get(dataset, dataset), grouping_text=GROUPINGS[grouping][1],
                  run_action="/engine/needs/run", fields=[("dataset", dataset), ("grouping", grouping)])


@router.post("/engine/needs/run")
def needs_run(request: Request, dataset: str = Form(""), grouping: str = Form(""), csrf: str = Form(...),
              user=Depends(require_role("analyst"))):
    check_csrf(request, csrf)
    try:
        spec = _spec(dataset, grouping)
        with engine.begin() as conn:
            res = ne.run_needs_query(conn, user.id, dataset, spec)
    except BudgetExhausted:
        return _analyst_page(request, user, status_code=409, error="Privacy budget exhausted. No answer was released.")
    except ValueError:
        return _analyst_page(request, user, status_code=400, error="Invalid query. Nothing was charged.")
    return RedirectResponse(f"/engine/result/{res['result_id']}", status_code=303)
