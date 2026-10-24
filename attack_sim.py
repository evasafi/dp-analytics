import numpy as np
import matplotlib.pyplot as plt

rng = np.random.default_rng(42)

TRUE_COUNT = 500          # the secret value the analyst is trying to learn
SENSITIVITY = 1           # a count changes by at most 1 per person
EPSILONS = [0.1, 0.5, 1.0]
N_QUERIES = 10_000        # how many times the analyst repeats the same query
TRIALS = 200              # repeat the whole attack to smooth the curves
CHECKPOINTS = [1, 10, 100, 1_000, 10_000]

plt.figure(figsize=(8, 5))

for eps in EPSILONS:
    scale = SENSITIVITY / eps
    # Each row is one attack: N_QUERIES independent noisy answers
    answers = TRUE_COUNT + rng.laplace(0, scale, size=(TRIALS, N_QUERIES))
    # Running average of the answers after 1, 2, 3, ... queries
    running_mean = np.cumsum(answers, axis=1) / np.arange(1, N_QUERIES + 1)
    # Mean absolute error of the attacker's estimate, averaged over trials
    err = np.abs(running_mean - TRUE_COUNT).mean(axis=0)

    print(f"\nepsilon per query = {eps}")
    print(f"{'queries':>8} {'attacker error':>16} {'total epsilon spent':>22}")
    for n in CHECKPOINTS:
        print(f"{n:>8} {err[n-1]:>16.3f} {n * eps:>22.1f}")

    plt.loglog(np.arange(1, N_QUERIES + 1), err, label=f"eps = {eps} per query")

plt.xlabel("Number of repeated queries")
plt.ylabel("Attacker's mean absolute error")
plt.title("Averaging attack on a Laplace-noised count (no budget)")
plt.legend()
plt.grid(True, which="both", alpha=0.3)
plt.tight_layout()
plt.savefig("averaging_attack.png", dpi=150)
plt.show()