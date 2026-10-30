import getpass
import json
import re
from urllib.parse import quote

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.districts import DISTRICTS, DISTRICTS_BY_PROVINCE
from app.main import app
from app.provinces import PROVINCE_NAMES

owner = getpass.getpass("postgres password: ")
admin = create_engine(f"postgresql+psycopg://postgres:{quote(owner, safe='')}@localhost:5432/dp_analytics")


def cleanup(c):
    u = "(SELECT id FROM users WHERE username = 'dt_req')"
    n = f"(SELECT id FROM needs WHERE requester_id IN {u})"
    c.execute(text(f"DELETE FROM need_events WHERE need_id IN {n}"))
    c.execute(text(f"DELETE FROM needs WHERE requester_id IN {u}"))
    c.execute(text("DELETE FROM users WHERE username = 'dt_req'"))
    c.execute(text("DELETE FROM login_failures WHERE username = '__register__'"))


fails = []


def check(label, ok, extra=""):
    print(("OK   " if ok else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        fails.append(label)


def stored():
    with admin.connect() as c:
        return c.execute(text("SELECT province, district FROM needs WHERE requester_id = "
                              "(SELECT id FROM users WHERE username = 'dt_req') ORDER BY id")).all()


IST, ANK = DISTRICTS_BY_PROVINCE["Istanbul"], DISTRICTS_BY_PROVINCE["Ankara"]
owners = {}
for p, d, _ in DISTRICTS:
    owners.setdefault(d, []).append(p)
shared = next(d for d, ps in sorted(owners.items()) if len(ps) >= 2)
shared_in, shared_out = owners[shared][0], next(p for p in sorted(PROVINCE_NAMES) if p not in owners[shared])

with admin.begin() as c:
    cleanup(c)
try:
    check("973 districts, unique pairs and codes, 81 provinces covered, names 2 to 40 characters",
          len(DISTRICTS) == 973 and len({(p, d) for p, d, _ in DISTRICTS}) == 973 and len({c for _, _, c in DISTRICTS}) == 973
          and set(DISTRICTS_BY_PROVINCE) == PROVINCE_NAMES and all(2 <= len(d) <= 40 for _, d, _ in DISTRICTS))
    check("Istanbul has 39 districts, including Uskudar", len(IST) == 39 and "Uskudar" in IST, str(len(IST)))
    print(f"INFO shared name {shared!r} is valid in {len(owners[shared])} provinces; refusal check uses {shared_out}")

    cl = TestClient(app, base_url="http://127.0.0.1:8000")
    tok = re.search(r'name="csrf" value="([^"]+)"', cl.get("/register").text).group(1)
    check("register a requester", cl.post("/register", data={"username": "dt_req", "password": "throwaway-password-1",
          "role": "requester", "csrf": tok}, follow_redirects=False).status_code == 303)

    page = cl.get("/needs/new").text
    m = re.search(r'<script type="application/json" id="districts-data">(.*?)</script>', page, re.S)
    data = json.loads(m.group(1)) if m else {}
    check("form embeds the full district list for every province", data == {k: list(v) for k, v in DISTRICTS_BY_PROVINCE.items()})
    check("district is a dropdown, not free text", '<select name="district"' in page and 'name="district" maxlength' not in page)

    def post(province, district):
        t = re.search(r'name="csrf" value="([^"]+)"', cl.get("/needs/new").text).group(1)
        return cl.post("/needs/new", data={"category": "health", "province": province, "district": district,
                       "description": "needs a wheelchair for a family member", "contact": "via verifier",
                       "csrf": t}, follow_redirects=False).status_code

    wrong = next(d for d in IST if d not in ANK)
    codes = [post("Ankara", wrong), post("Istanbul", "Atlantis"), post("Istanbul", IST[0].lower()),
             post("Istanbul", IST[0].upper()), post("Istanbul", "Üsküdar"), post("Istanbul", IST[0] + "; DROP TABLE needs"),
             post(shared_out, shared), post("Istanbul", "")]
    check("wrong pair, unknown, wrongly cased, accented, hostile, wrong-province and empty districts refused: " + str(codes),
          codes == [400, 400, 400, 400, 400, 400, 400, 422])
    check("refused posts saved nothing", stored() == [])

    r1 = post("Istanbul", IST[0])
    r2 = post(shared_in, shared)
    r3 = post("Istanbul", "  " + IST[1] + "  ")
    check("a valid pair, a shared name in its own province, and a padded district are accepted: " + str([r1, r2, r3]),
          [r1, r2, r3] == [303, 303, 303])
    check("accepted values are stored trimmed and as chosen",
          stored() == [("Istanbul", IST[0]), (shared_in, shared), ("Istanbul", IST[1])], str(stored()))
    check("the pending limit still applies: a 4th need is refused (429)", post("Istanbul", IST[2]) == 429)
finally:
    with admin.begin() as c:
        cleanup(c)
    print("throwaway account and needs removed")

assert not fails, f"failed: {fails}"
print("PASS")
