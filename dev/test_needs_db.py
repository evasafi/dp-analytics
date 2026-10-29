import getpass
from urllib.parse import quote

from sqlalchemy import create_engine, text

from app.db import engine

pw = getpass.getpass("postgres password: ")
admin = create_engine(f"postgresql+psycopg://postgres:{quote(pw, safe='')}@localhost:5432/dp_analytics")
ROLES = {"nd_req": "requester", "nd_ver": "verifier", "nd_sup1": "supporter", "nd_sup2": "supporter"}
NAMES = list(ROLES)


def cleanup(c):
    users = "(SELECT id FROM users WHERE username = ANY(:n))"
    needs = f"(SELECT id FROM needs WHERE requester_id IN {users})"
    c.execute(text(f"DELETE FROM need_events WHERE need_id IN {needs}"), {"n": NAMES})
    c.execute(text(f"DELETE FROM pledges WHERE need_id IN {needs}"), {"n": NAMES})
    c.execute(text(f"DELETE FROM needs WHERE requester_id IN {users}"), {"n": NAMES})
    c.execute(text("DELETE FROM users WHERE username = ANY(:n)"), {"n": NAMES})


with admin.begin() as c:
    cleanup(c)
    for u, role in ROLES.items():
        c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, 'x', :r)"),
                  {"u": u, "r": role})
    ids = dict(c.execute(text("SELECT username, id FROM users WHERE username = ANY(:n)"), {"n": NAMES}).all())

fails = []


def run(sql, **p):
    """Run as dp_app. Returns (succeeded, first line of the error)."""
    try:
        with engine.begin() as c:
            c.execute(text(sql), p)
        return True, ""
    except Exception as e:
        return False, str(getattr(e, "orig", e)).splitlines()[0]


def check(label, result, expect):
    good = result[0] == expect
    print(("OK   " if good else "FAIL ") + label + ("" if expect else f"  [{result[1]}]"))
    if not good:
        fails.append(label)


def new_need(**over):
    v = {"r": ids["nd_req"], "c": "health", "p": "Istanbul", "d": "Uskudar",
         "t": "Needs a wheelchair for a family member", "k": "via verifier"}
    v.update(over)
    with engine.begin() as c:
        return c.execute(text("INSERT INTO needs (requester_id, category, province, district, description, contact) "
                              "VALUES (:r, :c, :p, :d, :t, :k) RETURNING id"), v).scalar_one()


def status(i, new, pub=None):
    if pub:
        return run("UPDATE needs SET status = :s, public_text = :t WHERE id = :i", s=new, t=pub, i=i)
    return run("UPDATE needs SET status = :s WHERE id = :i", s=new, i=i)


try:
    n1 = new_need()
    check("new need starts as pending", run("SELECT 1 FROM needs WHERE id = :i AND status = 'pending'", i=n1), True)
    check("pending -> fulfilled refused", status(n1, "fulfilled"), False)
    check("pending -> pledged refused", status(n1, "pledged"), False)
    check("pending -> published without public text refused", status(n1, "published"), False)
    check("pending -> published with public text allowed", status(n1, "published", "Wheelchair needed, Uskudar"), True)
    check("published -> fulfilled refused", status(n1, "fulfilled"), False)
    check("published -> pledged allowed", status(n1, "pledged"), True)
    check("first pledge allowed", run("INSERT INTO pledges (need_id, supporter_id) VALUES (:n, :s)",
                                      n=n1, s=ids["nd_sup1"]), True)
    check("second active pledge on same need refused", run("INSERT INTO pledges (need_id, supporter_id) VALUES (:n, :s)",
                                                           n=n1, s=ids["nd_sup2"]), False)
    check("cancel first pledge allowed", run("UPDATE pledges SET status = 'cancelled' WHERE need_id = :n AND supporter_id = :s",
                                             n=n1, s=ids["nd_sup1"]), True)
    check("pledged -> published (released) allowed", status(n1, "published"), True)
    check("new pledge after a cancel allowed", run("INSERT INTO pledges (need_id, supporter_id) VALUES (:n, :s)",
                                                   n=n1, s=ids["nd_sup2"]), True)
    check("published -> pledged again allowed", status(n1, "pledged"), True)
    check("pledged -> fulfilled allowed", status(n1, "fulfilled"), True)
    check("fulfilled -> published refused", status(n1, "published"), False)
    check("fulfilled -> pledged refused", status(n1, "pledged"), False)
    check("fulfilled -> pending refused", status(n1, "pending"), False)
    check("same-status update allowed", status(n1, "fulfilled"), True)

    n2 = new_need()
    check("pending -> rejected allowed", status(n2, "rejected"), True)
    check("rejected -> published refused", status(n2, "published", "should not work"), False)
    check("rejected -> pending refused", status(n2, "pending"), False)

    check("unknown category refused", run("INSERT INTO needs (requester_id, category, province, district, description, contact) "
                                          "VALUES (:r, 'weapons', 'Istanbul', 'Uskudar', 'long enough text', 'via verifier')", r=ids["nd_req"]), False)
    check("too-short description refused", run("INSERT INTO needs (requester_id, category, province, district, description, contact) "
                                               "VALUES (:r, 'health', 'Istanbul', 'Uskudar', 'short', 'via verifier')", r=ids["nd_req"]), False)
    check("too-short province refused", run("INSERT INTO needs (requester_id, category, province, district, description, contact) "
                                            "VALUES (:r, 'health', 'I', 'Uskudar', 'long enough text', 'via verifier')", r=ids["nd_req"]), False)
finally:
    with admin.begin() as c:
        cleanup(c)
    print("throwaway users, needs, pledges and events removed")

assert not fails, f"failed: {fails}"
print("PASS")
