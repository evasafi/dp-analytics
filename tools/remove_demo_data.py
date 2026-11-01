"""Remove the matching-side demo data: one requester, one supporter, and everything tied to them.

Default is a rehearsal: the real DELETE statements run inside a transaction that is rolled back.
With --apply it asks you to type DELETE before committing. It never touches audit_log, results
or the budgets, and it refuses to run if anything unexpected is attached to these two users.
Run as the database owner (hidden password prompt).
"""
import argparse
import getpass
from urllib.parse import quote

from sqlalchemy import create_engine, text

ap = argparse.ArgumentParser()
ap.add_argument("--requester", default="Tugba")
ap.add_argument("--supporter", default="Diane")
ap.add_argument("--expect", default="needs=5,pledges=4,events=17,users=2")
ap.add_argument("--apply", action="store_true")
args = ap.parse_args()
want = {k: int(v) for k, v in (kv.split("=") for kv in args.expect.split(","))}

pw = getpass.getpass("postgres password: ")
admin = create_engine(f"postgresql+psycopg://postgres:{quote(pw, safe='')}@localhost:5432/dp_analytics")


def refuse(msg):
    raise SystemExit("REFUSED, nothing changed: " + msg)


with admin.connect() as conn:
    tx = conn.begin()
    q = lambda sql, **p: conn.execute(text(sql), p).all()
    users = q("SELECT id, username, role FROM users WHERE username IN (:a, :b)", a=args.requester, b=args.supporter)
    if args.requester == args.supporter or len(users) != 2:
        refuse("expected exactly two different users")
    by_name = {u.username: u for u in users}
    r, s = by_name[args.requester], by_name[args.supporter]
    if r.role != "requester" or s.role != "supporter":
        refuse(f"roles are {r.role} and {s.role}")
    uids = [r.id, s.id]
    users_before = q("SELECT count(*) FROM users")[0][0]

    needs = q("SELECT id, status FROM needs WHERE requester_id = :r ORDER BY id", r=r.id)
    need_ids = [n.id for n in needs]
    pledges = q("SELECT id, need_id, supporter_id, status FROM pledges "
                "WHERE supporter_id = :s OR need_id = ANY(CAST(:n AS bigint[])) ORDER BY id", s=s.id, n=need_ids)
    if any(pl.supporter_id != s.id or pl.need_id not in need_ids for pl in pledges):
        refuse("a pledge links these users to someone else's data")
    events = q("SELECT id, need_id FROM need_events WHERE actor_id = ANY(CAST(:u AS bigint[])) "
               "OR need_id = ANY(CAST(:n AS bigint[]))", u=uids, n=need_ids)
    if any(e.need_id not in need_ids for e in events):
        refuse("an event by these users sits on someone else's need")
    if q("SELECT count(*) FROM needs WHERE reviewed_by = ANY(CAST(:u AS bigint[])) AND requester_id <> :r",
         u=uids, r=r.id)[0][0]:
        refuse("these users reviewed someone else's need")
    used = q("SELECT (SELECT count(*) FROM audit_log WHERE user_id = ANY(CAST(:u AS bigint[]))) + "
             "(SELECT count(*) FROM results WHERE user_id = ANY(CAST(:u AS bigint[])))", u=uids)[0][0]
    if used:
        refuse("audit_log or results rows exist for these users and are never deleted")
    have = {"needs": len(needs), "pledges": len(pledges), "events": len(events), "users": 2}
    print("requester", r.username, "id", r.id, "| supporter", s.username, "id", s.id)
    print("needs:", [(n.id, n.status) for n in needs])
    print("pledges:", [(pl.id, pl.need_id, pl.status) for pl in pledges])
    print("events:", len(events))
    if have != want:
        refuse(f"counts {have} differ from expected {want}")

    def delete(label, sql, ids):
        n = conn.execute(text(sql), {"i": ids}).rowcount
        if n != len(ids):
            raise RuntimeError(f"{label}: deleted {n}, expected {len(ids)}")
        print("deleted", label, n)

    try:
        delete("need_events", "DELETE FROM need_events WHERE id = ANY(CAST(:i AS bigint[]))", [e.id for e in events])
        delete("pledges", "DELETE FROM pledges WHERE id = ANY(CAST(:i AS bigint[]))", [pl.id for pl in pledges])
        delete("needs", "DELETE FROM needs WHERE id = ANY(CAST(:i AS bigint[]))", need_ids)
        delete("users", "DELETE FROM users WHERE id = ANY(CAST(:i AS bigint[]))", uids)
        left = q("SELECT count(*) FROM users")[0][0]
        if left != users_before - 2:
            raise RuntimeError("user count changed by more than 2")
    except Exception:
        tx.rollback()
        raise
    if not args.apply:
        tx.rollback()
        print("REHEARSAL OK: everything above ran and was rolled back; nothing changed")
    elif input("Type DELETE to commit: ") == "DELETE":
        tx.commit()
        print("COMMITTED")
    else:
        tx.rollback()
        print("rolled back; nothing changed")