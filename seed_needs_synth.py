"""Synthetic needs for the analytics demos. ASSUMED distributions, not real data.

Everything here is a guess made by the author: how many needs a person posts, which provinces are
big, how goals are spread, the purpose and status mix. Results computed from it say nothing about
real needs. Goal ceilings are read from the database so the seed always respects the schema.
Rows are written to needs_synth (insert-only for the app role) and read back through synth_analytics.
"""
import numpy as np
from sqlalchemy import text

from app.db import engine
from app.provinces import PROVINCES

N_PERSONS = 12000
PURPOSES = ["rent_housing", "utilities", "food", "medical", "education", "disaster_recovery", "debt", "other"]
PURPOSE_W = [0.24, 0.14, 0.17, 0.11, 0.12, 0.09, 0.10, 0.03]
STATUSES = ["published", "fulfilled"]
STATUS_W = [0.37, 0.63]
EVENTS = ["earthquake", "flood", "fire", "other_disaster"]
EVENT_W = [0.70, 0.10, 0.15, 0.05]
HOUSEHOLD_W = [0.07, 0.13, 0.18, 0.24, 0.18, 0.10, 0.05, 0.025, 0.01, 0.007, 0.005, 0.003]
ABROAD_SHARE = 0.03            # share of medical needs that are treatment abroad (euro goals)
# lognormal goals: (median, sigma) per limit key; medical_abroad is in euros
GOAL = {"rent_housing": (30000, 0.7), "utilities": (4000, 0.7), "food": (3000, 0.6), "medical_turkey": (60000, 1.2),
        "medical_abroad": (60000, 0.9), "education": (9000, 0.8), "disaster_recovery": (120000, 1.0),
        "debt": (25000, 0.9), "other": (8000, 1.0)}
BIG = ["Istanbul", "Ankara", "Izmir", "Bursa", "Antalya", "Adana", "Konya", "Gaziantep", "Sanliurfa", "Kocaeli"]

rng = np.random.default_rng(2026)
names = [p[1] for p in PROVINCES]
rest = [n for n in names if n not in BIG]
rng.shuffle(rest)
order = [n for n in BIG if n in names] + rest              # assumed size order, biggest first
w = 1.0 / (np.arange(len(order)) + 1) ** 0.9
w /= w.sum()

with engine.begin() as conn:
    if conn.execute(text("SELECT count(*) FROM synth_analytics")).scalar_one():
        raise SystemExit("needs_synth already has rows; not touching it")
    ceiling = dict(conn.execute(text("SELECT key, ceiling FROM purpose_limits")).all())
    per_person = np.minimum(rng.geometric(0.6, size=N_PERSONS), 8)
    person_ids = [f"{x:012x}" for x in rng.integers(0, 2 ** 47, size=N_PERSONS)]
    assert len(set(person_ids)) == N_PERSONS
    prov_of = rng.choice(order, size=N_PERSONS, p=w)
    hh_of = rng.choice(np.arange(1, 13), size=N_PERSONS, p=HOUSEHOLD_W)
    idx = np.repeat(np.arange(N_PERSONS), per_person)       # which person each need belongs to
    n = len(idx)
    purpose = rng.choice(PURPOSES, size=n, p=PURPOSE_W)
    abroad = (purpose == "medical") & (rng.random(n) < ABROAD_SHARE)
    status = rng.choice(STATUSES, size=n, p=STATUS_W)
    event = rng.choice(EVENTS, size=n, p=EVENT_W)
    days = rng.integers(0, 365, size=n)
    z = rng.standard_normal(n)
    rows = []
    for k in range(n):
        p = str(purpose[k])
        key = ("medical_abroad" if abroad[k] else "medical_turkey") if p == "medical" else p
        med, sig = GOAL[key]
        goal = med * float(np.exp(sig * z[k]))
        step = 1000 if key == "medical_abroad" else 100
        goal = int(min(max(round(goal / step) * step, 100), ceiling[key]))
        rows.append({"p": person_ids[idx[k]], "pu": p,
                     "loc": (("abroad" if abroad[k] else "turkey") if p == "medical" else None),
                     "ev": (str(event[k]) if p == "disaster_recovery" else None),
                     "pv": str(prov_of[idx[k]]), "hh": int(hh_of[idx[k]]),
                     "cur": "EUR" if abroad[k] else "TRY", "g": goal, "s": str(status[k]), "d": int(days[k])})
    conn.execute(text("INSERT INTO needs_synth (person_id, purpose, medical_location, event, province, household_size, "
                      "currency, goal, status, created_at) VALUES (:p, :pu, :loc, :ev, :pv, :hh, :cur, :g, :s, "
                      "now() - make_interval(days => :d))"), rows)

with engine.connect() as c:
    q = lambda sql: c.execute(text(sql)).all()
    print("needs:", q("SELECT count(*) FROM synth_analytics")[0][0], "| people:", q("SELECT count(DISTINCT person_id) FROM synth_analytics")[0][0])
    print("most needs by one person:", q("SELECT max(n) FROM (SELECT count(*) n FROM synth_analytics GROUP BY person_id) t")[0][0],
          "| people with more than 3:", q("SELECT count(*) FROM (SELECT person_id FROM synth_analytics GROUP BY person_id HAVING count(*) > 3) t")[0][0])
    print("by status:", [tuple(r) for r in q("SELECT status, count(*) FROM synth_analytics GROUP BY 1 ORDER BY 2 DESC")])
    print("by purpose:", [tuple(r) for r in q("SELECT purpose, count(*) FROM synth_analytics GROUP BY 1 ORDER BY 2 DESC")])
    print("euro needs (medical abroad):", q("SELECT count(*) FROM synth_analytics WHERE currency = 'EUR'")[0][0])
    print("by band (lira needs):", [tuple(r) for r in q("SELECT band, count(*) FROM synth_analytics WHERE band IS NOT NULL GROUP BY 1 ORDER BY 1")])
    pc = q("SELECT province, count(*) FROM synth_analytics GROUP BY 1 ORDER BY 2 DESC")
    print("provinces with needs:", len(pc), "| largest:", tuple(pc[0]), "| smallest:", tuple(pc[-1]))
