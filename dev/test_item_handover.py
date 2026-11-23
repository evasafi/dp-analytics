"""Part 6: handover of item offers (proof photo or verifier waiver), the requester confirms or disputes,
a verifier settles, who can see a photo, and deletion after the retention period."""
import getpass
import io
import re
import shutil
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import proofs
from app.auth import hash_password
from app.config import settings
from app.districts import DISTRICTS_BY_PROVINCE
from app.main import app

admin = create_engine(make_url(settings.database_url).set(username="postgres", password=getpass.getpass("postgres password: ")))
PW = "Ith-test-pass-1234"
PREFIX = "ith_"
BASE = "http://127.0.0.1:8000"
fails = 0
total = 0


def check(label, ok, info=""):
    global fails, total
    total += 1
    if not ok:
        fails += 1
    print("%s%02d. %s%s" % ("ok   " if ok else "FAIL ", total, label, "" if ok or info == "" else "  [" + str(info) + "]"))


def csrf_from(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def one(sql, **p):
    with admin.connect() as c:
        return c.execute(text(sql), p).first()


def run(sql, **p):
    with admin.begin() as c:
        r = c.execute(text(sql), p)
        return r.scalar_one() if r.returns_rows else None


def cleanup():
    sub = "(SELECT id FROM needs WHERE requester_id IN (SELECT id FROM users WHERE username LIKE :p))"
    with admin.begin() as c:
        c.execute(text("DELETE FROM handover_proofs WHERE claim_id IN (SELECT id FROM item_claims WHERE need_id IN " + sub + ")"), {"p": PREFIX + "%"})
        c.execute(text("DELETE FROM item_claims WHERE need_id IN " + sub), {"p": PREFIX + "%"})
        c.execute(text("DELETE FROM need_events WHERE need_id IN " + sub), {"p": PREFIX + "%"})
        c.execute(text("DELETE FROM needs WHERE requester_id IN (SELECT id FROM users WHERE username LIKE :p)"), {"p": PREFIX + "%"})
        c.execute(text("DELETE FROM login_failures WHERE username LIKE :p"), {"p": PREFIX + "%"})
        c.execute(text("DELETE FROM users WHERE username LIKE :p"), {"p": PREFIX + "%"})


def make_user(u, role):
    return run("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r) RETURNING id", u=u, h=hash_password(PW), r=role)


def client_for(u):
    cl = TestClient(app, base_url=BASE)
    tok = csrf_from(cl.get("/login").text)
    code = cl.post("/login", data={"username": u, "password": PW, "csrf": tok}, follow_redirects=False).status_code
    assert code == 303, "login for %s gave %s" % (u, code)
    return cl


prov = next(iter(DISTRICTS_BY_PROVINCE))
dist = DISTRICTS_BY_PROVINCE[prov][0]
bed_id = one("SELECT id FROM item_types WHERE enabled AND category = 'bedding' ORDER BY id LIMIT 1")[0]


def new_need(requester_id, qty):
    nid = run("INSERT INTO needs (requester_id, need_kind, item_type_id, quantity_needed, used_ok, handover_method, province, "
              "district, household_size, description, contact_method, contact) VALUES (:u, 'item', :t, :q, true, 'public_place', "
              ":p, :d, 3, 'ITH test need for review', 'email', 'ith_req_contact@example.com') RETURNING id",
              u=requester_id, t=bed_id, q=qty, p=prov, d=dist)
    run("UPDATE needs SET status = 'published', public_text = 'Public text for the handover tests' WHERE id = :i", i=nid)
    return nid


def make_claim(nid, donor_id, tag, release=True):
    cid = run("INSERT INTO item_claims (need_id, donor_id, units, ack, contact_method, contact) "
              "VALUES (:n, :d, 1, true, 'email', :c) RETURNING id", n=nid, d=donor_id, c="ith_%s@example.com" % tag)
    if release:
        run("UPDATE item_claims SET released_at = now(), released_by = :v WHERE id = :c", v=VER, c=cid)
    return cid


def claim_row(cid):
    return one("SELECT status, sent_at, confirmed_at FROM item_claims WHERE id = :c", c=cid)


def proof_row(cid):
    return one("SELECT file_ref, waived_reason, delete_after, deleted_at FROM handover_proofs WHERE claim_id = :c", c=cid)


def need_status(nid):
    return one("SELECT status FROM needs WHERE id = :i", i=nid)[0]


def ev(name, cid):
    return one("SELECT count(*) FROM need_events WHERE event = :e AND (note = :a OR starts_with(note, :b))",
               e=name, a="claim %d" % cid, b="claim %d: " % cid)[0]


def jpeg(size=(300, 200), exif=True):
    im = Image.new("RGB", size, (200, 30, 30))
    buf = io.BytesIO()
    if exif:
        ex = Image.Exif()
        ex[0x010F] = "SecretCamera"
        ex[0x0132] = "2026:01:01 10:00:00"
        im.save(buf, "JPEG", exif=ex)
    else:
        im.save(buf, "JPEG")
    return buf.getvalue()


def png_with_text():
    im = Image.new("RGBA", (120, 80), (0, 128, 0, 100))
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "SecretNote")
    buf = io.BytesIO()
    im.save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


def gif():
    buf = io.BytesIO()
    Image.new("P", (10, 10)).save(buf, "GIF")
    return buf.getvalue()


def big_png():
    buf = io.BytesIO()
    Image.new("L", (9000, 9000)).save(buf, "PNG")
    return buf.getvalue()


def url_h(cid):
    return "/support/claims/%d/handover" % cid


def upload(cl, cid, token, data, name="my holiday.jpg", ctype="image/jpeg"):
    return cl.post(url_h(cid), data={"csrf": token}, files={"photo": (name, data, ctype)}, follow_redirects=False)


def files():
    return sorted(p.name for p in tmpdir.iterdir())


tmpdir = Path(tempfile.mkdtemp(prefix="aidbridge_test_proofs_"))
proofs.PROOF_DIR = tmpdir
cleanup()
try:
    VER = make_user("ith_ver", "verifier")
    req = make_user("ith_req", "requester")
    req2 = make_user("ith_req2", "requester")
    d = {i: make_user("ith_d%d" % i, "supporter") for i in (1, 2, 3, 4)}
    N, F, H = new_need(req, 4), new_need(req, 1), new_need(req, 1)
    C1, C2, C3 = make_claim(N, d[1], "c1"), make_claim(N, d[2], "c2"), make_claim(N, d[3], "c3")
    C4 = make_claim(N, d[4], "c4", release=False)
    CF, CH = make_claim(F, d[1], "cf"), make_claim(H, d[2], "ch")
    v, r, r2 = client_for("ith_ver"), client_for("ith_req"), client_for("ith_req2")
    c = {i: client_for("ith_d%d" % i) for i in (1, 2, 3, 4)}
    anon = TestClient(app, base_url=BASE)
    tv, tr, tr2 = (csrf_from(x.get(p).text) for x, p in ((v, "/verify"), (r, "/needs/mine"), (r2, "/needs/mine")))
    td = {i: csrf_from(c[i].get("/support").text) for i in c}

    print("-- the handover page")
    page = c[1].get(url_h(C1))
    check("the form has a photo field, multipart encoding and the privacy notice",
          page.status_code == 200 and 'name="photo"' in page.text and 'enctype="multipart/form-data"' in page.text and "faces" in page.text, page.status_code)
    check("another donor gets 404, a requester 403, a visitor 401",
          c[2].get(url_h(C1)).status_code == 404 and r.get(url_h(C1)).status_code == 403 and anon.get(url_h(C1)).status_code == 401)
    check("an offer whose contact was not released yet gives 409, and a photo for it is refused with nothing stored",
          c[4].get(url_h(C4)).status_code == 409 and upload(c[4], C4, td[4], jpeg()).status_code == 409 and files() == [])
    check("a forged csrf gives 403 and stores nothing", upload(c[1], C1, "forged", jpeg()).status_code == 403 and files() == [])

    print("-- photo checks")
    cases = [("no photo at all", c[1].post(url_h(C1), data={"csrf": td[1]}, follow_redirects=False)),
             ("a text file named .jpg", upload(c[1], C1, td[1], b"this is not an image at all", "a.jpg")),
             ("a GIF", upload(c[1], C1, td[1], gif(), "a.gif", "image/gif")),
             ("a file over 5 MB", upload(c[1], C1, td[1], b"\xff\xd8\xff" + bytes(5 * 1024 * 1024 + 10))),
             ("an image with too many pixels", upload(c[1], C1, td[1], big_png(), "big.png", "image/png"))]
    for label, resp in cases:
        check("rejected with 400: " + label, resp.status_code == 400, resp.status_code)
    check("nothing was stored and the offer is still pledged", files() == [] and claim_row(C1).status == "pledged" and proof_row(C1) is None)

    print("-- a valid photo")
    resp = upload(c[1], C1, td[1], jpeg())
    row = claim_row(C1)
    check("a JPEG with EXIF data: 303 to /support?done=claim_sent, claim sent, sent_at set, event recorded",
          resp.status_code == 303 and resp.headers.get("location") == "/support?done=claim_sent" and row.status == "sent"
          and row.sent_at is not None and ev("claim_sent", C1) == 1, resp.status_code)
    p1 = proof_row(C1)
    stored = files()
    check("one file stored under a random name, the upload name is not used, the database holds only that name",
          len(stored) == 1 and re.fullmatch(r"[0-9a-f]{32}\.jpg", stored[0]) and "holiday" not in stored[0] and p1.file_ref == stored[0]
          and p1.waived_reason is None and p1.delete_after is None, stored)
    raw = (tmpdir / stored[0]).read_bytes()
    im = Image.open(io.BytesIO(raw))
    check("the stored file is a JPEG with no EXIF data and no trace of the camera name", im.format == "JPEG" and len(im.getexif()) == 0 and b"SecretCamera" not in raw)
    check("a second upload for the same offer gives 409 and stores nothing more",
          upload(c[1], C1, td[1], jpeg()).status_code == 409 and files() == stored)
    check("another donor cannot upload for it (404)", upload(c[2], C1, td[2], jpeg()).status_code == 404)
    src = png_with_text()
    resp = upload(c[2], C2, td[2], src, "x.png", "image/png")
    stored2 = [f for f in files() if f not in stored]
    raw2 = (tmpdir / stored2[0]).read_bytes() if stored2 else b""
    check("a PNG with a text chunk and transparency is accepted and re-saved as JPEG without the text",
          resp.status_code == 303 and b"SecretNote" in src and len(stored2) == 1 and b"SecretNote" not in raw2
          and Image.open(io.BytesIO(raw2)).format == "JPEG", resp.status_code)
    resp = upload(c[1], CF, td[1], jpeg(size=(3200, 2000), exif=False))
    reff = proof_row(CF).file_ref
    check("a large photo is scaled down to 1600 pixels on its longest side",
          resp.status_code == 303 and max(Image.open(tmpdir / reff).size) == 1600)

    print("-- waiver")
    check("a short waiver reason gives 400 and records nothing",
          v.post("/verify/claims/%d/waive" % C3, data={"csrf": tv, "reason": "short"}, follow_redirects=False).status_code == 400 and proof_row(C3) is None)
    check("a waiver for an offer that was not released gives 409",
          v.post("/verify/claims/%d/waive" % C4, data={"csrf": tv, "reason": "Photo is not possible here"}, follow_redirects=False).status_code == 409 and proof_row(C4) is None)
    check("a requester and a supporter cannot waive (403)",
          r.post("/verify/claims/%d/waive" % C3, data={"csrf": tr, "reason": "Photo is not possible here"}, follow_redirects=False).status_code == 403
          and c[3].post("/verify/claims/%d/waive" % C3, data={"csrf": td[3], "reason": "Photo is not possible here"}, follow_redirects=False).status_code == 403)
    resp = v.post("/verify/claims/%d/waive" % C3, data={"csrf": tv, "reason": "Donor cannot take photos of the items"}, follow_redirects=False)
    pw = proof_row(C3)
    check("a good reason: 303, waiver stored with no file, event recorded",
          resp.status_code == 303 and resp.headers.get("location") == "/verify?done=waived" and pw.waived_reason is not None and pw.file_ref is None and ev("claim_photo_waived", C3) == 1, resp.status_code)
    check("waiving again gives 409",
          v.post("/verify/claims/%d/waive" % C3, data={"csrf": tv, "reason": "Donor cannot take photos of the items"}, follow_redirects=False).status_code == 409)
    wp = c[3].get(url_h(C3))
    check("the donor's page now has no photo field and says it was waived", wp.status_code == 200 and 'name="photo"' not in wp.text and "waived" in wp.text)
    before = files()
    resp = c[3].post(url_h(C3), data={"csrf": td[3]}, follow_redirects=False)
    check("the donor marks it handed over without a photo: 303, sent, no file stored",
          resp.status_code == 303 and claim_row(C3).status == "sent" and files() == before, resp.status_code)

    print("-- who can see a photo")
    mine = r.get("/needs/mine").text
    check("the requester sees photo links for the two photo offers and the waiver note for the third",
          "/needs/offers/%d/photo" % C1 in mine and "/needs/offers/%d/photo" % C2 in mine and "/needs/offers/%d/photo" % C3 not in mine and "waived" in mine)
    check("the requester sees the donor's contact while the offer is handed over", "ith_c1@example.com" in mine and "ith_c4@example.com" not in mine)
    ph = r.get("/needs/offers/%d/photo" % C1)
    check("the requester gets the photo as an uncached JPEG",
          ph.status_code == 200 and ph.headers.get("content-type") == "image/jpeg" and "no-store" in ph.headers.get("cache-control", "")
          and ph.headers.get("x-content-type-options") == "nosniff" and Image.open(io.BytesIO(ph.content)).format == "JPEG")
    check("another requester gets 404, the donor 403, a visitor 401",
          r2.get("/needs/offers/%d/photo" % C1).status_code == 404 and c[1].get("/needs/offers/%d/photo" % C1).status_code == 403
          and anon.get("/needs/offers/%d/photo" % C1).status_code == 401)
    vp = v.get("/verify/claims/%d/photo" % C1)
    check("a verifier sees the photo; requesters, supporters and visitors get 403, 403, 401",
          vp.status_code == 200 and vp.headers.get("content-type") == "image/jpeg" and r.get("/verify/claims/%d/photo" % C1).status_code == 403
          and c[1].get("/verify/claims/%d/photo" % C1).status_code == 403 and anon.get("/verify/claims/%d/photo" % C1).status_code == 401)
    check("a waived offer has no photo to serve", r.get("/needs/offers/%d/photo" % C3).status_code == 404 and v.get("/verify/claims/%d/photo" % C3).status_code == 404)
    ref1 = proof_row(C1).file_ref
    run("UPDATE handover_proofs SET file_ref = '../secret.jpg' WHERE claim_id = :c", c=C1)
    trav = r.get("/needs/offers/%d/photo" % C1).status_code
    run("UPDATE handover_proofs SET file_ref = :f WHERE claim_id = :c", f=ref1, c=C1)
    check("a file name that is not one of ours is never opened (404)", trav == 404, trav)

    print("-- the requester answers")
    check("another requester cannot confirm (404) and a donor cannot (403)",
          r2.post("/needs/offers/%d/confirm" % C1, data={"csrf": tr2}, follow_redirects=False).status_code == 404
          and c[1].post("/needs/offers/%d/confirm" % C1, data={"csrf": td[1]}, follow_redirects=False).status_code == 403)
    check("a forged csrf gives 403 and the offer stays sent",
          r.post("/needs/offers/%d/confirm" % C1, data={"csrf": "forged"}, follow_redirects=False).status_code == 403 and claim_row(C1).status == "sent")
    resp = r.post("/needs/offers/%d/confirm" % C1, data={"csrf": tr}, follow_redirects=False)
    keep = one("SELECT delete_after > now() + interval '29 days' AND delete_after < now() + interval '31 days' FROM handover_proofs WHERE claim_id = :c", c=C1)[0]
    mine = r.get("/needs/mine").text
    check("confirm: 303, confirmed, event recorded, the photo's 30-day clock starts, the donor's contact is hidden",
          resp.status_code == 303 and claim_row(C1).status == "confirmed" and ev("claim_confirmed", C1) == 1 and keep and "ith_c1@example.com" not in mine, resp.status_code)
    check("confirming again gives 409", r.post("/needs/offers/%d/confirm" % C1, data={"csrf": tr}, follow_redirects=False).status_code == 409)
    check("the need stays published while only part of it is confirmed", need_status(N) == "published")
    check("a short dispute reason gives 400 and the offer stays sent",
          r.post("/needs/offers/%d/dispute" % C2, data={"csrf": tr, "reason": "no"}, follow_redirects=False).status_code == 400 and claim_row(C2).status == "sent")
    resp = r.post("/needs/offers/%d/dispute" % C2, data={"csrf": tr, "reason": "Nothing arrived at the agreed place"}, follow_redirects=False)
    check("a dispute: 303, disputed, event with the reason, the photo is kept (no deletion date)",
          resp.status_code == 303 and claim_row(C2).status == "disputed" and ev("claim_disputed", C2) == 1 and proof_row(C2).delete_after is None, resp.status_code)
    check("the donor sees that it is disputed", "Disputed" in c[2].get("/support").text)
    resp = r.post("/needs/offers/%d/confirm" % C3, data={"csrf": tr}, follow_redirects=False)
    check("an offer handed over with a waiver can be confirmed too", resp.status_code == 303 and claim_row(C3).status == "confirmed")

    print("-- the verifier settles")
    q = v.get("/verify").text
    check("the queue lists the disputed handover", 'data-dispute="%d"' % C2 in q)
    kp = v.get("/verify/claims/%d" % C2)
    check("the claim page shows the requester's reason, the photo link and the settle form",
          kp.status_code == 200 and "Nothing arrived at the agreed place" in kp.text and "/verify/claims/%d/photo" % C2 in kp.text and "/verify/claims/%d/settle" % C2 in kp.text)
    check("a short reason gives 400, an unknown outcome gives 400, a requester gets 403, a forged csrf 403",
          v.post("/verify/claims/%d/settle" % C2, data={"csrf": tv, "outcome": "cancel", "reason": "no"}, follow_redirects=False).status_code == 400
          and v.post("/verify/claims/%d/settle" % C2, data={"csrf": tv, "outcome": "maybe", "reason": "A good enough reason"}, follow_redirects=False).status_code == 400
          and r.post("/verify/claims/%d/settle" % C2, data={"csrf": tr, "outcome": "cancel", "reason": "A good enough reason"}, follow_redirects=False).status_code == 403
          and v.post("/verify/claims/%d/settle" % C2, data={"csrf": "forged", "outcome": "cancel", "reason": "A good enough reason"}, follow_redirects=False).status_code == 403
          and claim_row(C2).status == "disputed")
    resp = v.post("/verify/claims/%d/settle" % C2, data={"csrf": tv, "outcome": "cancel", "reason": "The donor could not show the handover"}, follow_redirects=False)
    check("settle against the donor: 303, cancelled, event recorded, the photo's 30-day clock starts",
          resp.status_code == 303 and resp.headers.get("location") == "/verify?done=settled" and claim_row(C2).status == "cancelled"
          and ev("claim_settled", C2) == 1 and proof_row(C2).delete_after is not None, resp.status_code)
    check("settling again gives 409",
          v.post("/verify/claims/%d/settle" % C2, data={"csrf": tv, "outcome": "cancel", "reason": "The donor could not show the handover"}, follow_redirects=False).status_code == 409)
    check("the donor sees it as cancelled", "Cancelled" in c[2].get("/support").text)

    print("-- fulfilment")
    upload(c[1], CF, td[1], jpeg())  # already sent above; a repeat must not change anything
    check("a repeat upload for an offer that was already handed over changed nothing", claim_row(CF).status == "sent")
    r.post("/needs/offers/%d/confirm" % CF, data={"csrf": tr}, follow_redirects=False)
    check("confirming the last unit of a need fulfils the need", claim_row(CF).status == "confirmed" and need_status(F) == "fulfilled", need_status(F))
    check("a donor hands over, the requester disputes, the verifier settles for the donor: the need is fulfilled",
          upload(c[2], CH, td[2], jpeg(exif=False)).status_code == 303
          and r.post("/needs/offers/%d/dispute" % CH, data={"csrf": tr, "reason": "I think it did not arrive"}, follow_redirects=False).status_code == 303
          and v.post("/verify/claims/%d/settle" % CH, data={"csrf": tv, "outcome": "confirm", "reason": "The donor proved the handover"}, follow_redirects=False).status_code == 303
          and claim_row(CH).status == "confirmed" and need_status(H) == "fulfilled")

    print("-- deletion")
    reff_keep = proof_row(CF).file_ref
    run("UPDATE handover_proofs SET delete_after = now() - interval '1 hour' WHERE claim_id = :c", c=C1)
    v.get("/verify")
    pr = proof_row(C1)
    check("an expired photo is deleted from the disk and the database marks it deleted",
          not (tmpdir / ref1).exists() and pr.file_ref is None and pr.deleted_at is not None)
    check("a photo that has not expired is untouched", (tmpdir / reff_keep).exists())
    check("a deleted photo gives 404 to the requester and the verifier page says so",
          r.get("/needs/offers/%d/photo" % C1).status_code == 404 and "deleted after the retention period" in v.get("/verify/claims/%d" % C1).text)

    print("-- messages")
    check("the donor, queue and settle messages show",
          "Marked as handed over" in c[1].get("/support?done=claim_sent").text and "Photo requirement waived" in v.get("/verify?done=waived").text
          and "Handover dispute settled" in v.get("/verify?done=settled").text)
finally:
    cleanup()
    shutil.rmtree(tmpdir, ignore_errors=True)

print("\n%d of %d checks passed" % (total - fails, total))
sys.exit(1 if fails else 0)
