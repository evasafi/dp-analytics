import statistics

from app.db import engine
from app.engine import Spec, true_aggregates
from app.engine_noise import SUPPRESS_BELOW, cost, estimate, noisy_cells

TRIALS = 2000

# 1. cost does not depend on the number of cells
c1 = cost(Spec("count")), cost(Spec("count", ("region",))), cost(Spec("count", ("region", "household_size")))
print("1. count costs (0, 1, 2 group-bys):", c1, "(expect all equal)")
assert len(set(c1)) == 1
m1 = cost(Spec("mean")), cost(Spec("mean", ("region", "transfer_band")))
assert len(set(m1)) == 1 and m1[0] > c1[0]
print("   mean costs:", m1, "(equal, and above the count cost)")

# 2. every cell is returned, with plain Python types
spec96 = Spec("count", ("region", "household_size"))
from app.engine import DIMS
from itertools import product
cells = {k: (100, 18000.0) for k in product(*[DIMS[d][1] for d in spec96.group_by])}
res = noisy_cells(cells, spec96)
print("2. cells returned:", len(res), "(expect 96); types ok:",
      all(v is None or type(v) is int for v in res.values()), "(expect True)")
assert len(res) == 96

# 3. suppression: big cells survive, empty cells are mostly suppressed
spec = Spec("count", ("region",))
big = [noisy_cells({("r",): (1000, 0.0)}, spec)[("r",)] for _ in range(TRIALS)]
empty = [noisy_cells({("r",): (0, 0.0)}, spec)[("r",)] for _ in range(TRIALS)]
share_big = sum(v is None for v in big) / TRIALS
share_empty = sum(v is None for v in empty) / TRIALS
print(f"3. suppressed share, true n=1000: {share_big:.3f} (expect 0); true n=0: {share_empty:.3f} "
      f"(expect about 0.93, noise above {SUPPRESS_BELOW} happens about 7% of the time)")
assert share_big == 0 and share_empty > 0.85

# 4. mean cells: typical value near the truth, empty cells suppressed, no crash
mspec = Spec("mean", ("region",))
vals = [noisy_cells({("r",): (200, 36000.0)}, mspec)[("r",)] for _ in range(TRIALS)]
vals = [v for v in vals if v is not None]
med = statistics.median(vals)
print("4. mean cell, true 180: median", round(med, 1), "(expect within 15 of 180); kept", len(vals), "of", TRIALS)
assert abs(med - 180) < 15
zero = [noisy_cells({("r",): (0, 0.0)}, mspec)[("r",)] for _ in range(200)]
print("   empty mean cells suppressed:", sum(v is None for v in zero), "of 200 (expect most)")
assert sum(v is None for v in zero) > 150

# 5. estimate uses public facts only
print("5. estimate:", estimate(Spec("count", ("region",))))
print("   estimate:", estimate(Spec("mean", ("region",))))

# 6. real data, read only: one noisy region table next to the truth
with engine.connect() as c:
    truth = true_aggregates(c, 1, Spec("count", ("region",)))
noisy = noisy_cells(truth, Spec("count", ("region",)))
print("6. region      true   noisy")
for k in sorted(truth):
    print(f"   {k[0]:<9}{truth[k][0]:>6}   {noisy[k]}")
print("PASS")
