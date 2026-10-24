from app.config import settings
from app.engine import Spec, cell_count, validate
from app.noise import _laplace

SUPPRESS_BELOW = 20
LO, HI = 0, 500


def cost(spec: Spec) -> float:
    """Epsilon charged for one query. Cells are disjoint, so it does not depend on their number."""
    validate(spec)
    return settings.epsilon_per_count if spec.measure == "count" else settings.epsilon_per_mean


def estimate(spec: Spec) -> dict:
    """Public facts only: nothing here depends on the data."""
    eps = cost(spec)
    out = {"epsilon": eps, "cells": cell_count(spec), "suppress_below": SUPPRESS_BELOW}
    if spec.measure == "count":
        out["typical_error"] = f"about {1 / eps:.0f} people per cell"
    else:
        out["typical_error"] = (f"about {(HI / (eps / 2)):.0f} divided by the group size, in dollars "
                                f"(for example about {HI / (eps / 2) / 200:.0f} for a group of 200)")
    return out


def noisy_cells(true_cells: dict, spec: Spec) -> dict:
    """Return {cell: value or None}. None means suppressed. Every input cell is returned."""
    validate(spec)
    out = {}
    if spec.measure == "count":
        eps = settings.epsilon_per_count
        for key, (n, _s) in true_cells.items():
            v = max(0, round(_laplace(n, eps, 1)))
            out[key] = None if v < SUPPRESS_BELOW else int(v)
    else:
        half = settings.epsilon_per_mean / 2
        for key, (n, s) in true_cells.items():
            noisy_s = _laplace(s, half, HI)   # values clipped to [0, 500], so sensitivity 500
            noisy_n = _laplace(n, half, 1)
            if noisy_n < SUPPRESS_BELOW:
                out[key] = None
            else:
                out[key] = float(min(max(noisy_s / noisy_n, LO), HI))
    return out
