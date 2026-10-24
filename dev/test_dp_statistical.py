import numpy as np
from diffprivlib.mechanisms import Laplace

N = 400_000
MIN_COUNT = 400
Z = 4.0  # flag a violation only if the log ratio exceeds eps by more than Z standard errors


def samples(true_value, eps, sens):
    m = Laplace(epsilon=eps, sensitivity=sens)
    return np.array([m.randomise(true_value) for _ in range(N)])


def excess(a, b, eps, width):
    """Largest (|log ratio| - eps) in standard errors, over bins with enough samples."""
    lo, hi = min(a.min(), b.min()), max(a.max(), b.max())
    edges = np.arange(np.floor(lo), np.ceil(hi) + width, width)
    ha, _ = np.histogram(a, edges)
    hb, _ = np.histogram(b, edges)
    ok = (ha >= MIN_COUNT) & (hb >= MIN_COUNT)
    if ok.sum() == 0:
        return float("inf"), float("nan"), 0  # distributions do not overlap
    lr = np.abs(np.log(ha[ok] / hb[ok]))
    se = np.sqrt(1 / ha[ok] + 1 / hb[ok])
    z = (lr - eps) / se
    i = int(np.argmax(z))
    return float(z[i]), float(lr[i]), int(ok.sum())


def check(label, v0, v1, eps, sens, width, should_violate):
    a, b = samples(v0, eps, sens), samples(v1, eps, sens)
    z, lr, bins = excess(a, b, eps, width)
    violated = z > Z
    good = violated == should_violate
    print(f"{label}: worst |log ratio| = {lr:.3f}, eps = {eps}, excess = {z:.1f} SE, bins = {bins}, "
          f"violation detected = {violated} -> {'OK' if good else 'UNEXPECTED'}")
    return good


r = [
    check("count 193 vs 194, eps 0.1, sens 1", 193, 194, 0.1, 1, 1.0, False),
    check("mean sum 0 vs 500, eps 0.25, sens 500", 0, 500, 0.25, 500, 100.0, False),
    check("CONTROL count 193 vs 203, claims sens 1", 193, 203, 0.1, 1, 1.0, True),
    check("CONTROL sum 0 vs 500, claims sens 100", 0, 500, 0.25, 100, 100.0, True),
]
assert all(r), "a check behaved unexpectedly"
print("All checks behaved as expected.")
