"""Handover proof photos: check and re-encode an upload, store it on disk under a random name, serve it, delete it.

The original upload is never stored. Every photo is decoded and re-encoded as a plain JPEG, which drops EXIF
(location, camera, time), colour profiles, comments and anything appended to the file. The database keeps only the
random file name. The files live outside the database and outside the static folder, so nothing serves them
except the two routes that check who is asking.
"""
import io
import os
import re
import secrets
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from PIL import Image, ImageOps
from sqlalchemy import text

MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_SIDE = 1600
ALLOWED_FORMATS = ("JPEG", "PNG")
NAME = re.compile(r"[0-9a-f]{32}\.jpg")
PROOF_DIR = Path(os.environ.get("AIDBRIDGE_PROOF_DIR") or Path(__file__).resolve().parent.parent / "proofs")

# Starts the retention clock for a claim's photo. Used when a claim is confirmed or a dispute is settled.
KEEP_SQL = ("UPDATE handover_proofs SET delete_after = now() + make_interval(days => (SELECT photo_retention_days FROM settings)) "
            "WHERE claim_id = :c AND delete_after IS NULL")


class ProofError(Exception):
    pass


def clean_image(data: bytes) -> bytes:
    """Return a metadata-free JPEG made from the upload, or raise ProofError with a message for the donor."""
    if not data:
        raise ProofError("Choose a photo to upload.")
    if len(data) > MAX_BYTES:
        raise ProofError("The photo is larger than %d MB." % (MAX_BYTES // (1024 * 1024)))
    try:
        img = Image.open(io.BytesIO(data))
        fmt = img.format
        width, height = img.size
    except Exception:
        raise ProofError("That file is not a readable JPEG or PNG image.") from None
    if fmt not in ALLOWED_FORMATS:
        raise ProofError("Only JPEG and PNG photos are accepted.")
    if width * height > MAX_PIXELS:
        raise ProofError("That image has too many pixels. Use a normal phone photo.")
    try:
        img.load()
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.getchannel("A"))
        else:
            flat = img.convert("RGB")
        flat.thumbnail((MAX_SIDE, MAX_SIDE))
        clean = Image.new("RGB", flat.size)   # a fresh image carries no metadata from the upload
        clean.paste(flat)
        out = io.BytesIO()
        clean.save(out, format="JPEG", quality=85, optimize=True)
    except Exception:
        raise ProofError("That image could not be processed. Try another photo.") from None
    return out.getvalue()


def save(data: bytes) -> str:
    PROOF_DIR.mkdir(parents=True, exist_ok=True)
    name = secrets.token_hex(16) + ".jpg"
    with open(PROOF_DIR / name, "xb") as f:
        f.write(data)
    return name


def remove(name: str) -> None:
    if name and NAME.fullmatch(name):
        (PROOF_DIR / name).unlink(missing_ok=True)


def photo_response(file_ref: str):
    if not NAME.fullmatch(file_ref or ""):
        raise HTTPException(status_code=404, detail="not found")
    path = PROOF_DIR / file_ref
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                                 "Content-Disposition": "inline; filename=handover.jpg"})


def purge_expired(conn, limit: int = 20) -> int:
    """Delete photos whose retention has run out. A file that cannot be deleted is left for the next call."""
    rows = conn.execute(text(
        "SELECT id, file_ref FROM handover_proofs WHERE file_ref IS NOT NULL AND deleted_at IS NULL "
        "AND delete_after IS NOT NULL AND delete_after < now() ORDER BY id LIMIT :n"), {"n": limit}).all()
    done = 0
    for r in rows:
        try:
            remove(r.file_ref)
        except OSError:
            continue
        conn.execute(text("UPDATE handover_proofs SET file_ref = NULL, deleted_at = now() WHERE id = :i"), {"i": r.id})
        done += 1
    return done
