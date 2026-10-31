"""Differentially private counts over the needs table (person-level protection).

Each person's first CAP needs (by id) count; the rest are ignored. One person can
therefore change any released total by at most CAP, so Laplace noise uses
sensitivity CAP. Counts only, fixed groupings, no filters. The beneficiary engine
(app/engine.py, engine_noise.py, engine_run.py) is not used or changed here,
except for the Spec dataclass, the noise function and the budget spend.
"""
import hashlib
import json
from itertools import product

from sqlalchemy import text

from app.budget import spend
from app.config import settings
from app.engine import MAX_CELLS, Spec
from app.noise import _laplace
from app.provinces import PROVINCES

CAP = 3
SENSITIVITY = CAP
SUPPRESS_BELOW = 30

CATEGORIES = ["household", "clothing", "education", "baby_child", "health",
              "hygiene", "electronics", "culture_art", "sports", "street_animals"]
STATUSES = ["published", "pending", "rejected", "pledged", "fulfilled"]

# dimension name -> fixed list of values (column name equals dimension name)
NEEDS_DIMS = {
    "category": CATEGORIES,
    "province": [p[1] for p in PROVINCES],
    "status": STATUSES,
}
ALLOWED_GROUPINGS = {
    frozenset(["category"]), frozenset(["province"]), frozenset(["status"]),
    frozenset(["category", "status"]),
}
# dataset id -> table (whitelist; table names are never taken from input)
SOURCES = {7: "needs_synth"}


def cell_count(spec: Spec) -> int:
    n = 1
    for d in spec.group_by:
        n *= len(NEEDS_DIMS[d])
    return n


def validate_needs(spec: Spec, dataset_id: int) -> None:
    if dataset_id not in SOURCES:
        raise ValueError("dataset not available for needs analytics")
    if spec.measure != "count":
        raise ValueError("needs analytics supports counts only")
    if spec.filters:
        raise ValueError("filters are not supported for needs analytics")
    if len(set(spec.group_by)) != len(spec.group_by):
        raise ValueError("repeated dimension")
    if any(d not in NEEDS_DIMS for d in spec.group_by):
        raise ValueError("unknown dimension")
    if frozenset(spec.group_by) not in ALLOWED_GROUPINGS:
        raise ValueError("this grouping is not allowed")
    if cell_count(spec) > MAX_CELLS:
        raise ValueError("too many cells")


def cost(spec: Spec, dataset_id: int) -> float:
    validate_needs(spec, dataset_id)
    return settings.epsilon_per_count


def estimate(spec: Spec, dataset_id: int) -> dict:
    """Public facts only: nothing here depends on the data."""
    eps = cost(spec, dataset_id)
    return {"epsilon": eps, "cells": cell_count(spec), "suppress_below": SUPPRESS_BELOW,
            "cap_per_person": CAP,
            "typical_error": f"about {SENSITIVITY / eps:.0f} needs per cell"}


def true_counts(conn, dataset_id: int, spec: Spec) -> dict:
    """{cell: count} for every cell, including empty ones, after the per-person cap."""
    validate_needs(spec, dataset_id)
    table = SOURCES[dataset_id]
    sel = [f"{d} AS g{i}" for i, d in enumerate(spec.group_by)]
    sql = (f"WITH capped AS (SELECT category, province, status, "
           f"row_number() OVER (PARTITION BY person_id ORDER BY id) AS rn FROM {table}) "
           f"SELECT {', '.join(sel)}, count(*) AS n FROM capped WHERE rn <= {int(CAP)} "
           f"GROUP BY " + ", ".join(f"g{i}" for i in range(len(spec.group_by))))
    cells = {key: 0 for key in product(*[NEEDS_DIMS[d] for d in spec.group_by])}
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


def _spec_params(spec: Spec) -> dict:
    return {"measure": spec.measure, "group_by": list(spec.group_by),
            "filters": [[d, list(v)] for d, v in spec.filters]}


def run_needs_query(conn, user_id: int, dataset_id: int, spec: Spec) -> dict:
    """Validate, spend, compute, audit. Call inside engine.begin(); any error rolls back everything."""
    eps = cost(spec, dataset_id)          # validates before anything is charged
    spend(conn, dataset_id, eps)          # row lock; raises BudgetExhausted if short
    truth = true_counts(conn, dataset_id, spec)
    noisy = noisy_counts(truth, eps)
    cells = [[list(k), noisy[k]] for k in sorted(noisy)]
    digest = hashlib.sha256(json.dumps(cells).encode()).hexdigest()
    spec_json = json.dumps(_spec_params(spec), sort_keys=True)
    audit_id = conn.execute(
        text("INSERT INTO audit_log (user_id, dataset_id, query_type, params, "
             "epsilon_charged, result_hash) VALUES (:u, :d, :q, CAST(:p AS jsonb), :e, :h) RETURNING id"),
        {"u": user_id, "d": dataset_id, "q": "needs_count",
         "p": spec_json, "e": eps, "h": digest},
    ).scalar_one()
    result_id = conn.execute(
        text("INSERT INTO results (user_id, dataset_id, audit_id, spec, cells) "
             "VALUES (:u, :d, :a, CAST(:s AS jsonb), CAST(:c AS jsonb)) RETURNING id"),
        {"u": user_id, "d": dataset_id, "a": audit_id, "s": spec_json,
         "c": json.dumps(cells)},
    ).scalar_one()
    return {"epsilon": eps, "cells": {tuple(k): v for k, v in cells},
            "audit_id": audit_id, "result_id": result_id}