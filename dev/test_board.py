import getpass
from urllib.parse import quote

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import app

owner = getpass.getpass("postgres password: ")
admin = create_engine(f"postgresql+psycopg://postgres:{quote(owner, safe='')}@localhost:5432/dp_analytics")
PRIVATE = ["PRIVATE-DESC", "PRIVATE-CONTACT", "REJECTED-NOTE-MARKER"]


def cleanup(c):
    c.execute(text("DELETE FROM needs WHERE requester_id IN (SELECT id FROM users WHERE username = 'bd_req')"))
    c.execute(text("DELETE FROM users WHERE username = 'bd_req'"))


def add(c, uid, cat, prov, dist, desc, contact, public, status):
    first = "published" if status in ("published", "pledged", "fulfilled") else "pending"
    nid = c.execute(text(
        "INSERT INTO needs (requester_id, category, province, district, description, contact, public_text, status) "
        "VALUES (:u, :c, :p, :d, :t, :k, :pt, :s) RETURNING id"),
        {"u": uid, "c": cat, "p": prov, "d": dist, "t": desc, "k": contact,
         "pt": public if first == "published" else None, "s": first}).scalar_one()
    for s in {"pledged": ["pledged"], "fulfilled": ["pledged", "fulfilled"], "rejected": ["rejected"]}.get(status, []):
        c.execute(text("UPDATE needs SET status = :s WHERE id = :i"), {"s": s, "i": nid})
    if status == "rejected":
        c.execute(text("UPDATE needs SET review_note = 'REJECTED-NOTE-MARKER' WHERE id = :i"), {"i": nid})
    return nid


def q(sql, **p):
    with admin.connect() as c:
        return c.execute(text(sql), p).all()


fails = []


def check(label, ok, extra=""):
    print(("OK   " if ok else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        fails.append(label)


with admin.begin() as c:
    cleanup(c)
    uid = c.execute(text("INSERT INTO users (username, password_hash, role) VALUES ('bd_req', 'x', 'requester') RETURNING id")).scalar_one()
    a = add(c, uid, "health", "Istanbul", "Uskudar", "PRIVATE-DESC-ALPHA family details", "PRIVATE-CONTACT-ALPHA",
            "Board test alpha: a wheelchair is needed.", "published")
    add(c, uid, "education", "Ankara", "Cankaya", "PRIVATE-DESC-BETA family details", "PRIVATE-CONTACT-BETA",
        "Board test beta: school books are needed.", "published")
    add(c, uid, "household", "Izmir", "Bornova", "PRIVATE-DESC-PENDING family details", "PRIVATE-CONTACT-PENDING", None, "pending")
    add(c, uid, "clothing", "Izmir", "Bornova", "PRIVATE-DESC-REJECTED family details", "PRIVATE-CONTACT-REJECTED", None, "rejected")
    add(c, uid, "household", "Izmir", "Konak", "PRIVATE-DESC-GAMMA family details", "PRIVATE-CONTACT-GAMMA",
        "Board test gamma: a stove is needed.", "pledged")
    add(c, uid, "baby_child", "Izmir", "Konak", "PRIVATE-DESC-DELTA family details", "PRIVATE-CONTACT-DELTA",
        "Board test delta: a cot is needed.", "fulfilled")
    add(c, uid, "sports", "Izmir", "Karsiyaka", "PRIVATE-DESC-EPS family details", "PRIVATE-CONTACT-EPS",
        "<script>alert(1)</script> Board test epsilon", "published")
month = q("SELECT to_char(created_at, 'YYYY-MM') FROM needs WHERE id = :i", i=a)[0][0]

try:
    anon = TestClient(app, base_url="http://127.0.0.1:8000")
    r = anon.get("/board")
    page = r.text
    check("anonymous visitor can open the board (200)", r.status_code == 200)
    check("the board sets no cookies", "set-cookie" not in r.headers and not anon.cookies)
    check("published needs are shown with province, district and month",
          all(s in page for s in ("Board test alpha", "Board test beta", "Istanbul", "Uskudar", "Ankara", "Cankaya", month)))
    check("private descriptions, contacts and review notes never appear", not any(s in page for s in PRIVATE))
    check("pledged and fulfilled needs are not shown", "Board test gamma" not in page and "Board test delta" not in page)
    check("hostile public text is escaped, not executed",
          "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page)

    h = anon.get("/board?category=health").text
    check("category filter: health shows alpha, not beta", "Board test alpha" in h and "Board test beta" not in h)
    p = anon.get("/board?province=Ankara").text
    check("province filter: Ankara shows beta, not alpha", "Board test beta" in p and "Board test alpha" not in p)
    both = anon.get("/board?category=health&province=Ankara").text
    check("both filters together show neither", "Board test alpha" not in both and "Board test beta" not in both)

    codes = [anon.get("/board?category=weapons").status_code, anon.get("/board?province=Atlantis").status_code,
             anon.get("/board?province=Istanbul;DROP TABLE needs").status_code]
    check("unknown category and province refused (all 400): " + str(codes), all(c == 400 for c in codes))
    codes = [anon.get("/board?page=abc").status_code, anon.get("/board?page=0").status_code, anon.get("/board?page=1001").status_code]
    check("bad page numbers refused (all 422): " + str(codes), all(c == 422 for c in codes))
    far = anon.get("/board?page=1000")
    check("a very high page is empty, not an error", far.status_code == 200 and "Board test alpha" not in far.text)

    mr = anon.get("/board/map-data")
    data = mr.json()
    by = {d["name"]: d["count"] for d in data}
    db = dict(q("SELECT province, count(*) FROM needs WHERE status = 'published' GROUP BY province"))
    check("map data: 81 provinces with exactly the expected fields",
          mr.status_code == 200 and len(data) == 81 and all(set(d) == {"name", "pcode", "lat", "lon", "count"} for d in data))
    check("map counts equal the published needs per province in the database, and the total matches",
          all(by[n] == db.get(n, 0) for n in by) and sum(by.values()) == sum(db.values()))
    check("map data contains no private text", not any(s in mr.text for s in PRIVATE + ["gamma", "delta", "alpha"]))
    mh = anon.get("/board/map-data?category=health").json()
    dbh = dict(q("SELECT province, count(*) FROM needs WHERE status = 'published' AND category = 'health' GROUP BY province"))
    check("map data respects the category filter", all(d["count"] == dbh.get(d["name"], 0) for d in mh))
    check("map data refuses an unknown category (400)", anon.get("/board/map-data?category=weapons").status_code == 400)

    s = anon.get("/static/geo/turkey_provinces.min.geojson")
    print("INFO boundary file served:", s.status_code == 200, "(the board works without it)")

    codes = [anon.get("/board").status_code for _ in range(70)]
    check("rate limit: 200s first, then 429 (60 per minute)", codes[0] == 200 and codes[-1] == 429 and 429 in codes, f"{codes.count(200)} ok, {codes.count(429)} refused")
    check("the limit is shared with the map data endpoint", anon.get("/board/map-data").status_code == 429)
finally:
    with admin.begin() as c:
        cleanup(c)
    print("throwaway requester and needs removed")

assert not fails, f"failed: {fails}"
print("PASS")
