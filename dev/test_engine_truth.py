from sqlalchemy import text

from app.db import engine
from app.engine import Spec, true_aggregates

DS = 1


def total(cells):
    return sum(n for n, _ in cells.values())


with engine.connect() as c:
    by_region = true_aggregates(c, DS, Spec("count", ("region",)))
    print("1. region cells:", len(by_region), "(expect 8); region_8:", by_region[("region_8",)][0],
          "(expect 193); total:", total(by_region), "(expect 5000)")

    two_way = true_aggregates(c, DS, Spec("count", ("region", "household_size")))
    print("2. region x household cells:", len(two_way), "(expect 96); total:", total(two_way), "(expect 5000)")

    bands = true_aggregates(c, DS, Spec("count", ("transfer_band",)))
    print("3. band cells:", len(bands), "(expect 10); total:", total(bands), "(expect 5000)")

    filt = true_aggregates(c, DS, Spec("count", ("household_size",), (("region", ("region_8",)),)))
    print("4. region_8 by household, total:", total(filt), "(expect 193); cells:", len(filt), "(expect 12)")

    n, s = true_aggregates(c, DS, Spec("mean", (), (("region", ("region_8",)),)))[()]
    print("5. region_8 mean:", round(s / n, 1), "(expect 179.8)")

    direct = c.execute(text("SELECT count(*) FROM beneficiaries WHERE dataset_id = 1 AND region = 'region_8' "
                            "AND household_size BETWEEN 3 AND 4")).scalar_one()
    multi = true_aggregates(c, DS, Spec("count", (), (("region", ("region_8",)), ("household_size", (3, 4)))))[()][0]
    print("6. filtered count matches direct SQL:", multi == direct, "(expect True)")

bad = [
    Spec("median"), Spec("count", ("region", "region", )), Spec("count", ("region", "household_size", "transfer_band")),
    Spec("count", ("password_hash",)), Spec("count", (), (("region", ("region_99",)),)),
    Spec("count", (), (("region", ("region_1",)), ("region", ("region_2",)))),
]
refused = 0
for s_ in bad:
    try:
        with engine.connect() as c:
            true_aggregates(c, DS, s_)
    except ValueError:
        refused += 1
print("7. invalid specs refused:", refused, "of", len(bad), "(expect 6 of 6)")
