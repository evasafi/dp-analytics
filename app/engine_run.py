import hashlib
import json

from sqlalchemy import text

from app.budget import spend
from app.engine import Spec, true_aggregates
from app.engine_noise import cost, noisy_cells


def _spec_params(spec: Spec) -> dict:
    return {"measure": spec.measure, "group_by": list(spec.group_by),
            "filters": [[d, list(v)] for d, v in spec.filters]}


def run_query(conn, user_id: int, dataset_id: int, spec: Spec) -> dict:
    """Validate, spend, compute, audit. Call inside engine.begin(); any error rolls back everything."""
    eps = cost(spec)                      # validates the spec before anything is charged
    spend(conn, dataset_id, eps)          # row lock; raises BudgetExhausted if short
    truth = true_aggregates(conn, dataset_id, spec)
    noisy = noisy_cells(truth, spec)
    cells = [[list(k), noisy[k]] for k in sorted(noisy)]
    digest = hashlib.sha256(json.dumps(cells).encode()).hexdigest()
    spec_json = json.dumps(_spec_params(spec), sort_keys=True)
    audit_id = conn.execute(
        text("INSERT INTO audit_log (user_id, dataset_id, query_type, params, "
             "epsilon_charged, result_hash) VALUES (:u, :d, :q, CAST(:p AS jsonb), :e, :h) RETURNING id"),
        {"u": user_id, "d": dataset_id, "q": "engine_" + spec.measure,
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
