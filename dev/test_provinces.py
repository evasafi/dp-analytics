import getpass
import re
from urllib.parse import quote

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import app
from app.districts import DISTRICTS_BY_PROVINCE
from app.provinces import PROVINCES

owner = getpass.getpass("postgres password: ")
admin = create_engine(f"postgresql+psycopg://postgres:{quote(owner, safe='')}@localhost:5432/dp_analytics")


def cleanup(c):
    u = "(SELECT id FROM users WHERE username = 'pv_req')"
    n = f"(SELECT id FROM needs WHERE requester_id IN {u})"
    c.execute(text(f"DELETE FROM need_events WHERE need_id IN {n}"))
    c.execute(text(f"DELETE FROM needs WHERE requester_id IN {u}"))
    c.execute(text("DELETE FROM users WHERE username = 'pv_req'"))
    c.execute(text("DELETE FROM login_failures WHERE username = '__register__'"))


fails = []


def check(label, ok, extra=""):
    print(("OK   " if ok else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        fails.append(label)


with admin.begin() as c:
    cleanup(c)
try:
    names = [p[1] for p in PROVINCES]
    check("81 provinces, 81 unique names and codes", len(PROVINCES) == 81 and len(set(names)) == 81 and len({p[0] for p in PROVINCES}) == 81)
    check("Istanbul, Sanliurfa and Canakkale are on the list", {"Istanbul", "Sanliurfa", "Canakkale"} <= set(names))

    cl = TestClient(app, base_url="http://127.0.0.1:8000")
    tok = re.search(r'name="csrf" value="([^"]+)"', cl.get("/register").text).group(1)
    r = cl.post("/register", data={"username": "pv_req", "password": "throwaway-password-1", "role": "requester", "csrf": tok}, follow_redirects=False)
    check("register a requester", r.status_code == 303)

    page = cl.get("/needs/new")
    prov_select = re.search(r'<select name="province"[^>]*>(.*?)</select>', page.text, re.S).group(1)
options = re.findall(r'<option value="([^"]+)"', prov_select)
    check("form shows all 81 provinces as a dropdown", set(names) <= set(options) and 'name="province" maxlength' not in page.text, str(len(options)))

    def post(province):
        t = re.search(r'name="csrf" value="([^"]+)"', cl.get("/needs/new").text).group(1)
        return cl.post("/needs/new", data={"category": "health", "province": province, "district": DISTRICTS_BY_PROVINCE.get(province.strip(), ["Uskudar"])[0],
                                           "description": "needs a wheelchair for a family member", "contact": "via verifier",
                                           "csrf": t}, follow_redirects=False).status_code

    codes = [post("Atlantis"), post("istanbul"), post("ISTANBUL"), post(""), post("Istanbul; DROP TABLE needs")]
    check("unknown, wrongly cased, empty and hostile provinces refused (400, or 422 for empty): " + str(codes),
          codes == [400, 400, 400, 422, 400])
    with admin.connect() as c:
        n = c.execute(text("SELECT count(*) FROM needs WHERE requester_id = (SELECT id FROM users WHERE username='pv_req')")).scalar_one()
    check("refused posts saved nothing", n == 0)
    padded = post("Istanbul ")
    with admin.connect() as c:
        stored = c.execute(text("SELECT province FROM needs WHERE requester_id = (SELECT id FROM users WHERE username='pv_req')")).scalars().all()
    check("a padded province is accepted and stored trimmed", padded == 303 and stored == ["Istanbul"], str(stored))
    check("a listed province is accepted", post("Istanbul") == 303)
    check("another listed province is accepted", post("Sanliurfa") == 303)
finally:
    with admin.begin() as c:
        cleanup(c)
    print("throwaway account and needs removed")

assert not fails, f"failed: {fails}"
print("PASS")
