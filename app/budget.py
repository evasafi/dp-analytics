from decimal import Decimal

from sqlalchemy import text


class BudgetExhausted(Exception):
    pass


def spend(conn, dataset_id: int, epsilon: float) -> Decimal:
    """Atomically charge epsilon to a dataset's global budget.

    Must be called inside a transaction (engine.begin()). The row lock
    from FOR UPDATE is held until that transaction commits or rolls back,
    so a concurrent spend waits and then sees the updated total.
    Returns the new epsilon_spent.
    """
    eps = Decimal(str(epsilon))
    if eps <= 0:
        raise ValueError("epsilon must be positive")

    row = conn.execute(
        text("SELECT epsilon_total, epsilon_spent FROM datasets "
             "WHERE id = :id FOR UPDATE"),
        {"id": dataset_id},
    ).one()

    new_spent = row.epsilon_spent + eps
    if new_spent > row.epsilon_total:
        raise BudgetExhausted(
            f"need {eps}, remaining {row.epsilon_total - row.epsilon_spent}"
        )

    conn.execute(
        text("UPDATE datasets SET epsilon_spent = :s WHERE id = :id"),
        {"s": new_spent, "id": dataset_id},
    )
    return new_spent
