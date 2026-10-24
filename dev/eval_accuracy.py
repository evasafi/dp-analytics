import csv

import matplotlib
matplotlib.use("Agg")  # save the image, no window to close
import matplotlib.pyplot as plt
import numpy as np
from sqlalchemy import text

from app.config import settings
from app.db import engine
from app.noise import GroupTooSmall, noisy_count, noisy_mean

TRIALS = 1000
COUNT_EPS = [0.05, 0.1, 0.2, 0.5, 1.0]
MEAN_EPS = [0.1, 0.2, 0.5, 1.0, 2.0]
LO, HI = 0, 500
TOTAL = 5.0

with engine.connect() as c:
    rows = c.execute(
        text("SELECT region, count(*) AS n, "
             "SUM(LEAST(GREATEST(monthly_transfer, :lo), :hi)) AS s "
             "FROM beneficiaries WHERE dataset_id = 1 "
             "GROUP BY region ORDER BY region"),
        {"lo": LO, "hi": HI},
    ).all()
truth = [(r.region, int(r.n), float(r.s)) for r in rows]

results = []  # (query, region, n, epsilon, error_metric, relative_pct, refusal_rate)

# ---- counts: mean absolute error ----
print("COUNT: mean absolute error, and as % of the true count")
print("(queries available from a budget of 5.0 = 5.0 / epsilon)")
print(f"{'region':<10}{'n':>6}  " + "".join(f"eps={e:<14}" for e in COUNT_EPS))
count_cells = {}
for e in COUNT_EPS:
    settings.epsilon_per_count = e
    for region, n, s in truth:
        errs = [abs(noisy_count(n) - n) for _ in range(TRIALS)]
        mae = float(np.mean(errs))
        count_cells[(region, e)] = (mae, 100 * mae / n)
        results.append(("count", region, n, e, mae, 100 * mae / n, 0.0))
for region, n, s in truth:
    cells = "".join(
        f"{count_cells[(region, e)][0]:>5.1f} ({count_cells[(region, e)][1]:>4.1f}%)  "
        for e in COUNT_EPS
    )
    print(f"{region:<10}{n:>6}  {cells}")
print("queries available: " + "  ".join(f"eps={e}: {TOTAL / e:.0f}" for e in COUNT_EPS))

# ---- means: median absolute error (the ratio of two noisy values has heavy tails) ----
print()
print("MEAN: median absolute error in dollars, as % of the true mean, and refusal rate")
print(f"{'region':<10}{'n':>6}{'true':>7}  " + "".join(f"eps={e:<18}" for e in MEAN_EPS))
mean_cells = {}
for e in MEAN_EPS:
    settings.epsilon_per_mean = e
    for region, n, s in truth:
        true_mean = s / n
        vals, refused = [], 0
        for _ in range(TRIALS):
            try:
                vals.append(noisy_mean(s, n, LO, HI))
            except GroupTooSmall:
                refused += 1
        med = float(np.median(np.abs(np.array(vals) - true_mean))) if vals else float("nan")
        rel = 100 * med / true_mean
        rr = refused / TRIALS
        mean_cells[(region, e)] = (med, rel, rr)
        results.append(("mean", region, n, e, med, rel, rr))
for region, n, s in truth:
    cells = "".join(
        f"{mean_cells[(region, e)][0]:>5.1f} ({mean_cells[(region, e)][1]:>4.1f}%) r={mean_cells[(region, e)][2]:.2f}  "
        for e in MEAN_EPS
    )
    print(f"{region:<10}{n:>6}{s / n:>7.1f}  {cells}")
print("queries available: " + "  ".join(f"eps={e}: {TOTAL / e:.0f}" for e in MEAN_EPS))

with open("eval_accuracy.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["query", "region", "n", "epsilon", "error", "relative_pct", "refusal_rate"])
    w.writerows(results)

fig, axes = plt.subplots(1, 2, figsize=(12, 5))
for region, n, s in truth:
    axes[0].plot(COUNT_EPS, [count_cells[(region, e)][1] for e in COUNT_EPS],
                 marker="o", label=f"{region} (n={n})")
    axes[1].plot(MEAN_EPS, [mean_cells[(region, e)][1] for e in MEAN_EPS],
                 marker="o", label=f"{region} (n={n})")
axes[0].set_title("Count: mean abs error as % of true count")
axes[0].set_xlabel("epsilon per count query")
axes[1].set_title("Mean transfer: median abs error as % of true mean")
axes[1].set_xlabel("epsilon per mean query")
for ax in axes:
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_ylabel("relative error (%)")
    ax.grid(True, which="both", alpha=0.3)
axes[1].legend(fontsize=8)
plt.tight_layout()
plt.savefig("eval_accuracy.png", dpi=150)
print("\nsaved eval_accuracy.csv and eval_accuracy.png")
