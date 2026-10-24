from diffprivlib.mechanisms import Laplace

from app.config import settings


class GroupTooSmall(Exception):
    """Noisy count too small for a meaningful mean."""


def _laplace(value: float, epsilon: float, sensitivity: float) -> float:
    return float(Laplace(epsilon=epsilon, sensitivity=sensitivity).randomise(value))


def noisy_count(true_count: int) -> int:
    # One person changes a count by at most 1.
    v = _laplace(true_count, settings.epsilon_per_count, 1)
    return max(0, round(v))  # clamp + round: post-processing, no extra epsilon


def noisy_histogram(true_counts: list[int]) -> list[int]:
    # Bins are disjoint and each person is in exactly one, so the whole
    # histogram costs epsilon_per_histogram. Pass ALL bins, including empty
    # ones, or the set of bins present leaks information.
    return [
        max(0, round(_laplace(c, settings.epsilon_per_histogram, 1)))
        for c in true_counts
    ]


def noisy_mean(true_sum: float, true_count: int, lo: float, hi: float) -> float:
    # true_sum must be over values already clipped to [lo, hi].
    # Budget splits evenly: epsilon_per_mean covers sum + count together.
    half = settings.epsilon_per_mean / 2
    sensitivity = max(abs(lo), abs(hi))
    s = _laplace(true_sum, half, sensitivity)
    n = _laplace(true_count, half, 1)
    if n <= 1:
        raise GroupTooSmall("group too small for a meaningful mean")
    return min(max(s / n, lo), hi)
