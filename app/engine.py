from dataclasses import dataclass
from itertools import product

from sqlalchemy import text

REGIONS = [f"region_{i}" for i in range(1, 9)]
HOUSEHOLD = list(range(1, 13))
BANDS = list(range(10))  # band k covers [50k, 50k+50); 450-500 is band 9
MAX_CELLS = 150

# column expression (from this whitelist only) and the fixed list of values
DIMS = {
    "region": ("region", REGIONS),
    "household_size": ("household_size", HOUSEHOLD),
    "transfer_band": (
        "CAST(LEAST(FLOOR(LEAST(GREATEST(monthly_transfer, 0), 500) / 50), 9) AS integer)",
        BANDS,
    ),
}


@dataclass(frozen=True)
class Spec:
    measure: str                 # "count" or "mean"
    group_by: tuple = ()         # up to two dimension names
    filters: tuple = ()          # tuples of (dimension, (allowed values,))


def cell_count(spec: Spec) -> int:
    n = 1
    for d in spec.group_by:
        n *= len(DIMS[d][1])
    return n


def validate(spec: Spec) -> None:
    if spec.measure not in ("count", "mean"):
        raise ValueError("unknown measure")
    if len(spec.group_by) > 2 or len(set(spec.group_by)) != len(spec.group_by):
        raise ValueError("group by at most two different dimensions")
    for d in spec.group_by:
        if d not in DIMS:
            raise ValueError("unknown dimension")
    seen = set()
    for dim, values in spec.filters:
        if dim not in DIMS or dim in seen:
            raise ValueError("bad filter dimension")
        seen.add(dim)
        if not values or not set(values) <= set(DIMS[dim][1]):
            raise ValueError("filter value not allowed")
    if cell_count(spec) > MAX_CELLS:
        raise ValueError("too many cells")


def _where(spec: Spec, dataset_id: int):
    clauses, params = ["dataset_id = :d"], {"d": dataset_id}
    for i, (dim, values) in enumerate(spec.filters):
        names = []
        for j, v in enumerate(values):
            k = f"f{i}_{j}"
            params[k] = v
            names.append(f":{k}")
        clauses.append(f"{DIMS[dim][0]} IN ({', '.join(names)})")
    return " AND ".join(clauses), params


def true_aggregates(conn, dataset_id: int, spec: Spec) -> dict:
    """Return {cell: (count, clipped_sum)} for every cell, including empty ones."""
    validate(spec)
    where, params = _where(spec, dataset_id)
    sel = [f"{DIMS[d][0]} AS g{i}" for i, d in enumerate(spec.group_by)]
    sel += ["count(*) AS n",
            "COALESCE(SUM(LEAST(GREATEST(monthly_transfer, 0), 500)), 0) AS s"]
    sql = f"SELECT {', '.join(sel)} FROM beneficiaries WHERE {where}"
    if spec.group_by:
        sql += " GROUP BY " + ", ".join(f"g{i}" for i in range(len(spec.group_by)))
    cells = {key: (0, 0.0) for key in product(*[DIMS[d][1] for d in spec.group_by])}
    for r in conn.execute(text(sql), params):
        key = tuple(r[i] for i in range(len(spec.group_by)))
        cells[key] = (int(r.n), float(r.s))
    return cells
