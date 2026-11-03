"""Differentially private counts of needs (person-level protection).

Each person's first CAP needs (by id) in the source view count; the rest are ignored. One person can therefore
change any released total by at most CAP, so Laplace noise uses sensitivity CAP. Counts only, fixed groupings,
no filters, every cell returned (empty ones included), noisy values below SUPPRESS_BELOW are withheld.

Sources are views, chosen from a whitelist by dataset name: needs_synthetic reads synth_analytics (assumed data),
needs_live reads needs_analytics (verified needs on the platform). A view name is never taken from input.
"""
import hashlib
import json
from itertools import product

from sqlalchemy import text

from app.budget import spend
from app.config import settings
from app.db import engine
from app.engine import MAX_CELLS, Spec
from app.noise import _laplace
from app.provinces import PROVINCES
from app.reference import PURPOSES

CAP = 3
SENSITIVITY = CAP
SUPPRESS_BELOW = 30

# dataset name -> source view. Tests add throwaway dataset names here; nothing else does.
SOURCES = {"needs_synthetic": "synth_analytics", "needs_live": "needs_analytics"}
DATASET_TEXT = {"needs_synthetic": "Synthetic needs (assumed data)", "needs_live": "Needs on the platform (verified, demo data)"}

NEED_TYPES = [k for k, _ in PURPOSES if k != "medical"] + ["medical_turkey", "medical_abroad"]
# dimension -> (SQL expression over the view, from this whitelist only; fixed list of values or None for the goal bands)
DIMS = {
    "need_type": ("CASE WHEN purpose = 'medical' THEN 'medical_' || medical_location ELSE purpose END", NEED_TYPES),
    "province": ("province", [p[1] for p in PROVINCES]),
    "status": ("status", ["published", "fulfilled"]),
    "band": ("band", None),   # lira needs only; euro needs have no band and land in no cell
}
ALLOWED_GROUPINGS = {frozenset(["need_type"]), frozenset(["province"]), frozenset(["status"]),
                     frozenset(["need_type", "status"]), frozenset(["band"])}


def dim_values(d: str) -> list:
    """The fixed values of a dimension. Goal bands come from the amount_bands reference table, not from any need."""
    values = DIMS[d][1]
    if values is None:
        with engine.connect() as c:
            values = [r[0] for r in c.execute(text("SELECT code FROM amount_bands ORDER BY code"))]
    return values


def cell_count(spec: Spec) -> int:
    n = 1
    for d in spec.group_by:
        n *= len(dim_values(d))
    return n


def validate_needs(spec: Spec, dataset: str) -> None:
    if dataset not in SOURCES:
        raise ValueError("dataset not available for needs analytics")
    if spec.measure != "count":
        raise ValueError("needs analytics supports counts only")
    if spec.filters:
        raise ValueError("filters are not supported for needs analytics")
    if len(set(spec.group_by)) != len(spec.group_by):
        raise ValueError("repeated dimension")
    if any(d not in DIMS for d in spec.group_by):
        raise ValueError("unknown dimension")
    if frozenset(spec.group_by) not in ALLOWED_GROUPINGS:
        raise ValueError("this grouping is not allowed")
    if cell_count(spec) > MAX_CELLS:
        raise ValueError("too many cells")


def dataset_id(conn, dataset: str) -> int:
    return conn.execute(text("SELECT id FROM datasets WHERE name = :n"), {"n": dataset}).scalar_one()


def cost(spec: Spec, dataset: str) -> float:
    validate_needs(spec, dataset)
    return settings.epsilon_per_count


def estimate(spec: Spec, dataset: str) -> dict:
    """Public facts only: nothing here depends on the data."""
    eps = cost(spec, dataset)
    return {"epsilon": eps, "cells": cell_count(spec), "suppress_below": SUPPRESS_BELOW,
            "cap_per_person": CAP, "typical_error": f"about {SENSITIVITY / eps:.0f} needs per cell"}


def true_counts(conn, dataset: str, spec: Spec) -> dict:
    """{cell: count} for every cell, including empty ones, after the per-person cap."""
    validate_needs(spec, dataset)
    view = SOURCES[dataset]
    sel = [f"{DIMS[d][0]} AS g{i}" for i, d in enumerate(spec.group_by)]
    sql = (f"WITH capped AS (SELECT *, row_number() OVER (PARTITION BY person_id ORDER BY id) AS rn FROM {view}) "
           f"SELECT {', '.join(sel)}, count(*) AS n FROM capped WHERE rn <= {int(CAP)} "
           f"GROUP BY " + ", ".join(f"g{i}" for i in range(len(spec.group_by))))
    cells = {key: 0 for key in product(*[dim_values(d) for d in spec.group_by])}
    for r in conn.execute(text(sql)):
        key = tuple(r[i] for i in range(len(spec.group_by)))
        if key in cells:
            cells[key] = int(r.n)
    return cells


def noisy_counts(true_cells: dict, epsilon: float) -> dict:
    """{cell: value or None}. None means suppressed. Every input cell is returned."""
    out = {}
    for key, n in true_cells.items():
        v = max(0, round(_laplace(n, epsilon, SENSITIVITY)))
        out[key] = None if v < SUPPRESS_BELOW else int(v)
    return out


def _spec_params(spec: Spec, dataset: str) -> dict:
    return {"measure": spec.measure, "group_by": list(spec.group_by),
            "filters": [[d, list(v)] for d, v in spec.filters], "dataset": dataset}


def run_needs_query(conn, user_id: int, dataset: str, spec: Spec) -> dict:
    """Validate, spend, compute, audit. Call inside engine.begin(); any error rolls back everything."""
    eps = cost(spec, dataset)             # validates before anything is charged
    ds = dataset_id(conn, dataset)
    spend(conn, ds, eps)                  # row lock; raises BudgetExhausted if short
    truth = true_counts(conn, dataset, spec)
    noisy = noisy_counts(truth, eps)
    cells = [[list(k), noisy[k]] for k in sorted(noisy)]
    digest = hashlib.sha256(json.dumps(cells).encode()).hexdigest()
    spec_json = json.dumps(_spec_params(spec, dataset), sort_keys=True)
    audit_id = conn.execute(
        text("INSERT INTO audit_log (user_id, dataset_id, query_type, params, "
             "epsilon_charged, result_hash) VALUES (:u, :d, :q, CAST(:p AS jsonb), :e, :h) RETURNING id"),
        {"u": user_id, "d": ds, "q": "needs_count", "p": spec_json, "e": eps, "h": digest},
    ).scalar_one()
    result_id = conn.execute(
        text("INSERT INTO results (user_id, dataset_id, audit_id, spec, cells) "
             "VALUES (:u, :d, :a, CAST(:s AS jsonb), CAST(:c AS jsonb)) RETURNING id"),
        {"u": user_id, "d": ds, "a": audit_id, "s": spec_json, "c": json.dumps(cells)},
    ).scalar_one()
    return {"epsilon": eps, "cells": {tuple(k): v for k, v in cells}, "audit_id": audit_id, "result_id": result_id}
