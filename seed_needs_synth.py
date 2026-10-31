"""Fill needs_synth with SYNTHETIC needs. The distributions below are assumptions, not real data."""
import secrets
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import text

from app.db import engine
from app.provinces import PROVINCES

CATEGORIES = ["household", "clothing", "education", "baby_child", "health",
              "hygiene", "electronics", "culture_art", "sports", "street_animals"]
CATEGORY_WEIGHTS = [0.18, 0.16, 0.14, 0.14, 0.12, 0.08, 0.06, 0.04, 0.04, 0.04]
STATUSES = ["published", "pending", "rejected", "pledged", "fulfilled"]
STATUS_WEIGHTS = [0.20, 0.08, 0.12, 0.12, 0.48]
BIG = ["Istanbul", "Ankara", "Izmir", "Bursa", "Antalya", "Adana", "Konya", "Gaziantep", "Sanliurfa", "Kocaeli"]
N_PERSONS = 12000

rng = np.random.default_rng(2026)
names = [p[1] for p in PROVINCES]
rest = [n for n in names if n not in BIG]
rng.shuffle(rest)
order = [n for n in BIG if n in names] + rest          # assumed size order, biggest first
w = 1.0 / (np.arange(len(order)) + 1) ** 0.9
w /= w.sum()

with engine.begin() as conn:
    if conn.execute(text("SELECT count(*) FROM needs_synth")).scalar_one():
        raise SystemExit("needs_synth already has rows; not touching it")
    per_person = np.minimum(rng.geometric(0.6, size=N_PERSONS), 8)
    provs = rng.choice(order, size=N_PERSONS, p=w)
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(N_PERSONS):
        pid = secrets.token_hex(6)
        for _ in range(int(per_person[i])):
            rows.append({"p": pid, "c": str(rng.choice(CATEGORIES, p=CATEGORY_WEIGHTS)), "v": str(provs[i]),
                         "s": str(rng.choice(STATUSES, p=STATUS_WEIGHTS)),
                         "t": now - timedelta(days=int(rng.integers(0, 365)))})
    conn.execute(text("INSERT INTO needs_synth (person_id, category, province, status, created_at) "
                      "VALUES (:p, :c, :v, :s, :t)"), rows)

with engine.connect() as c:
    q = lambda sql: c.execute(text(sql)).all()
    print("needs:", q("SELECT count(*) FROM needs_synth")[0][0], "| people:", q("SELECT count(DISTINCT person_id) FROM needs_synth")[0][0])
    print("most needs by one person:", q("SELECT max(n) FROM (SELECT count(*) n FROM needs_synth GROUP BY person_id) t")[0][0],
          "| people with more than 3:", q("SELECT count(*) FROM (SELECT person_id FROM needs_synth GROUP BY person_id HAVING count(*) > 3) t")[0][0])
    print("by status:", [tuple(r) for r in q("SELECT status, count(*) FROM needs_synth GROUP BY 1 ORDER BY 2 DESC")])
    pc = q("SELECT province, count(*) FROM needs_synth GROUP BY 1 ORDER BY 2 DESC")
    print("provinces with needs:", len(pc), "| largest:", tuple(pc[0]), "| smallest:", tuple(pc[-1]))
    print("provinces with fewer than 30 needs:", sum(1 for r in pc if r[1] < 30))