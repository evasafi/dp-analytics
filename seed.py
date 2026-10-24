import secrets

import numpy as np
from sqlalchemy import text

from app.db import engine

N = 5000
DATASET_ID = 1
REGIONS = [f"region_{i}" for i in range(1, 9)]
REGION_WEIGHTS = [0.25, 0.20, 0.15, 0.12, 0.10, 0.08, 0.06, 0.04]

rng = np.random.default_rng(2026)

with engine.begin() as conn:
    existing = conn.execute(
        text("SELECT count(*) FROM beneficiaries WHERE dataset_id = :d"),
        {"d": DATASET_ID},
    ).scalar_one()
    if existing:
        raise SystemExit(f"already seeded ({existing} rows); not touching it")

    regions = rng.choice(REGIONS, size=N, p=REGION_WEIGHTS)
    sizes = np.clip(rng.poisson(3.5, size=N) + 1, 1, 12)
    transfers = np.clip(40 * sizes + rng.normal(0, 60, size=N), 0, 500).round(2)

    rows = [
        {"d": DATASET_ID, "b": secrets.token_hex(8), "r": str(regions[i]),
         "h": int(sizes[i]), "t": float(transfers[i])}
        for i in range(N)
    ]
    conn.execute(
        text("INSERT INTO beneficiaries "
             "(dataset_id, beneficiary_id, region, household_size, monthly_transfer) "
             "VALUES (:d, :b, :r, :h, :t)"),
        rows,
    )

with engine.connect() as conn:
    for r in conn.execute(text(
        "SELECT region, count(*) FROM beneficiaries "
        "WHERE dataset_id = 1 GROUP BY region ORDER BY region")):
        print(r[0], r[1])
