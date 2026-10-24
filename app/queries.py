import hashlib
import json

from sqlalchemy import text

from app.budget import spend
from app.config import settings
from app.noise import GroupTooSmall, noisy_count, noisy_histogram, noisy_mean

class Refused:
    """Returned instead of a number when a query is refused. Still charged."""
    def __init__(self, reason):
        self.reason = reason


REGIONS = [f"region_{i}" for i in range(1, 9)]
HOUSEHOLD_SIZES = list(range(1, 13))
TRANSFER_LO, TRANSFER_HI = 0, 500


def _audit(conn, user_id, dataset_id, query_type, params, eps, result):
    digest = hashlib.sha256(
        json.dumps(result, sort_keys=True).encode()
    ).hexdigest()
    conn.execute(
        text(
            "INSERT INTO audit_log (user_id, dataset_id, query_type, params, "
            "epsilon_charged, result_hash) VALUES "
            "(:u, :d, :q, CAST(:p AS jsonb), :e, :h)"
        ),
        {"u": user_id, "d": dataset_id, "q": query_type,
         "p": json.dumps(params, sort_keys=True), "e": eps, "h": digest},
    )


def count_by_region(conn, user_id, dataset_id, region):
    if region not in REGIONS:
        raise ValueError("unknown region")
    eps = settings.epsilon_per_count
    spend(conn, dataset_id, eps)
    true = conn.execute(
        text("SELECT count(*) FROM beneficiaries "
             "WHERE dataset_id = :d AND region = :r"),
        {"d": dataset_id, "r": region},
    ).scalar_one()
    result = noisy_count(true)
    _audit(conn, user_id, dataset_id, "count", {"region": region}, eps, result)
    return result


def household_histogram(conn, user_id, dataset_id):
    eps = settings.epsilon_per_histogram
    spend(conn, dataset_id, eps)
    rows = conn.execute(
        text("SELECT household_size, count(*) FROM beneficiaries "
             "WHERE dataset_id = :d GROUP BY household_size"),
        {"d": dataset_id},
    ).all()
    found = {size: n for size, n in rows}
    true = [found.get(s, 0) for s in HOUSEHOLD_SIZES]
    result = noisy_histogram(true)
    _audit(conn, user_id, dataset_id, "histogram", {}, eps, result)
    return dict(zip(HOUSEHOLD_SIZES, result))


def mean_transfer(conn, user_id, dataset_id, region):
    if region not in REGIONS:
        raise ValueError("unknown region")
    eps = settings.epsilon_per_mean
    spend(conn, dataset_id, eps)  # charged even if the mean is refused
    row = conn.execute(
        text("SELECT count(*) AS n, "
             "COALESCE(SUM(LEAST(GREATEST(monthly_transfer, :lo), :hi)), 0) AS s "
             "FROM beneficiaries WHERE dataset_id = :d AND region = :r"),
        {"d": dataset_id, "r": region, "lo": TRANSFER_LO, "hi": TRANSFER_HI},
    ).one()
    try:
        result = noisy_mean(float(row.s), int(row.n), TRANSFER_LO, TRANSFER_HI)
    except GroupTooSmall:
        _audit(conn, user_id, dataset_id, "mean", {"region": region}, eps, "refused")
        return Refused("group too small for a meaningful mean")
    _audit(conn, user_id, dataset_id, "mean", {"region": region}, eps, result)
    return result

