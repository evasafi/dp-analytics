import numpy as np

from app.config import settings
from app.engine import Spec
from app.engine_noise import noisy_cells

N = 200_000
MIN_COUNT = 400
Z = 4.0
EPS = settings.epsilon_per_count
SPEC = Spec("count", ("region",))


def run(table):
    a, b = [], []
    for _ in range(N):
        out = noisy_cells({k: (v, 0.0) for k, v in table.items()}, SPEC)
        a.append(out[("a",)])
        b.append(out[("b",)])
    a = np.array([x for x in a if x is not None], dtype=float)
    b = np.array([x for x in b if x is not None], dtype=float)
    return a, b


def excess(x, y):
    lo, hi = min(x.min(), y.min()), max(x.max(), y.max())
    edges = np.arange(np.floor(lo), np.ceil(hi) + 2, 1.0)
    hx, _ = np.histogram(x, edges)
    hy, _ = np.histogram(y, edges)
    ok = (hx >= MIN_COUNT) & (hy >= MIN_COUNT)
    if ok.sum() == 0:
        return float("inf"), float("nan"), 0
    lr = np.abs(np.log(hx[ok] / hy[ok]))
    se = np.sqrt(1 / hx[ok] + 1 / hy[ok])
    z = (lr - EPS) / se
    i = int(np.argmax(z))
    return float(z[i]), float(lr[i]), int(ok.sum())


base_a, base_b = run({("a",): 193, ("b",): 500})
plus_a, plus_b = run({("a",): 194, ("b",): 500})
far_a, _ = run({("a",): 203, ("b",): 500})

ok_all = True
for label, x, y, should_violate in [
    ("changed cell, +1 person", base_a, plus_a, False),
    ("unchanged cell, same table otherwise", base_b, plus_b, False),
    ("CONTROL cell differs by 10, claims sens 1", base_a, far_a, True),
]:
    z, lr, bins = excess(x, y)
    violated = z > Z
    good = violated == should_violate
    ok_all &= good
    print(f"{label}: worst |log ratio| = {lr:.3f}, eps = {EPS}, excess = {z:.1f} SE, bins = {bins}, "
          f"violation = {violated} -> {'OK' if good else 'UNEXPECTED'}")

# independence of noise between cells (same call, same table)
m = min(len(base_a), len(base_b))
corr = float(np.corrcoef(base_a[:m] - 193, base_b[:m] - 500)[0, 1])
limit = 4 / np.sqrt(m)
good = abs(corr) < limit
ok_all &= good
print(f"noise correlation between cells: {corr:.4f}, limit {limit:.4f} -> {'OK' if good else 'UNEXPECTED'}")

assert ok_all, "a check behaved unexpectedly"
print("All checks behaved as expected.")
