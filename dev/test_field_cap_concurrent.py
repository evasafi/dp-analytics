import getpass
import threading
from urllib.parse import quote

from sqlalchemy import create_engine, text

from app.auth import hash_password
from app.db import engine

pw = getpass.getpass("postgres password: ")
admin = create_engine(
    f"postgresql+psycopg://postgres:{quote(pw, safe='')}@localhost:5432/dp_analytics"
)
with admin.begin() as c:
    c.execute(text("DELETE FROM audit_log WHERE dataset_id IN (SELECT id FROM datasets WHERE name='cap_test')"))
    c.execute(text("DELETE FROM beneficiaries WHERE dataset_id IN (SELECT id FROM datasets WHERE name='cap_test')"))
    c.execute(text("DELETE FROM datasets WHERE name='cap_test'"))
    c.execute(text("DELETE FROM audit_log WHERE user_id IN (SELECT id FROM users WHERE username='cap_agent')"))
    c.execute(text("DELETE FROM users WHERE username='cap_agent'"))
    c.execute(text("INSERT INTO datasets (name, epsilon_total) VALUES ('cap_test', 1.0)"))
    c.execute(text("INSERT INTO users (username, password_hash, role) VALUES ('cap_agent', :h, 'field_agent')"),
              {"h": hash_password("throwaway-password-1")})
    ds = c.execute(text("SELECT id FROM datasets WHERE name='cap_test'")).scalar_one()
    uid = c.execute(text("SELECT id FROM users WHERE username='cap_agent'")).scalar_one()

CAP, THREADS = 10, 20
results, lock, barrier = [], threading.Lock(), threading.Barrier(THREADS)


def submit():
    barrier.wait()
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": 7000000 + uid})
        used = conn.execute(text("SELECT count(*) FROM audit_log WHERE user_id=:u AND query_type='submit' "
                                 "AND ts > now() - interval '24 hours'"), {"u": uid}).scalar_one()
        if used >= CAP:
            outcome = "refused"
        else:
            conn.execute(text("INSERT INTO audit_log (user_id, dataset_id, query_type, params, "
                              "epsilon_charged, result_hash) VALUES (:u, :d, 'submit', '{}'::jsonb, 0, 'n/a')"),
                         {"u": uid, "d": ds})
            outcome = "ok"
    with lock:
        results.append(outcome)


ts = [threading.Thread(target=submit) for _ in range(THREADS)]
[t.start() for t in ts]
[t.join() for t in ts]
with engine.connect() as c:
    n = c.execute(text("SELECT count(*) FROM audit_log WHERE user_id=:u AND query_type='submit'"), {"u": uid}).scalar_one()
ok = results.count("ok")
print("succeeded:", ok, " refused:", results.count("refused"), "(expect 10 and 10)")
print("audit rows:", n, "(expect 10)")
assert ok == CAP and n == CAP, "the lock failed: the cap was exceeded"
print("PASS: the cap holds under concurrency")
