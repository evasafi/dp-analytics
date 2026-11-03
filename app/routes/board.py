"""The public board: open needs with their funding, one page per need, and the map data.

No sign-in and no cookies. It reads only the public_needs view, so a private column cannot reach these pages.
A limit of 60 requests a minute per address is kept in memory.
"""
import threading
import time
from collections import deque
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from app.db import engine
from app.provinces import PROVINCE_NAMES, PROVINCES
from app.reference import EVENTS, PURPOSE_LABELS, PURPOSES, money, need_label
from app.web import client_ip, templates

router = APIRouter()
PAGE_SIZE = 20
BOARD_LIMIT, BOARD_WINDOW = 60, 60  # requests per window (seconds), per address, in memory
_RATE: dict = {}
_RATE_LOCK = threading.Lock()
SORTS = {"new": "id DESC", "least": "confirmed / goal ASC, id DESC", "near": "promised / goal DESC, id DESC"}
SORT_TEXT = {"new": "Newest", "least": "Least funded", "near": "Closest to the goal"}
EVENT_LABELS = dict(EVENTS)
COLUMNS = ("id, to_char(month, 'YYYY-MM') AS month, purpose, medical_location, event, province, district, currency, goal, "
           "public_text, promised, confirmed, donors")


def _board_rate_ok(ip: str) -> bool:
    now = time.monotonic()
    with _RATE_LOCK:
        if len(_RATE) > 5000:
            for k in [k for k, d in _RATE.items() if not d or now - d[-1] > BOARD_WINDOW]:
                del _RATE[k]
        d = _RATE.setdefault(ip, deque())
        while d and now - d[0] > BOARD_WINDOW:
            d.popleft()
        if len(d) >= BOARD_LIMIT:
            return False
        d.append(now)
        return True


def _too_many():
    return Response("Too many requests. Try again in a minute.", status_code=429, media_type="text/plain")


def _bad_filter(purpose: str, province: str) -> bool:
    return (bool(purpose) and purpose not in PURPOSE_LABELS) or (bool(province) and province not in PROVINCE_NAMES)


def _expire():
    """Pledges nobody marked as sent in time stop counting. Idempotent, and a no-op when nothing is overdue."""
    with engine.begin() as c:
        c.execute(text("SELECT expire_pledges()"))


def _card(r):
    goal = float(r.goal)
    conf = min(100.0, float(r.confirmed) / goal * 100)
    prom = min(100.0, float(r.promised) / goal * 100)
    parts = []
    if r.donors:
        parts.append("Confirmed " + money(r.confirmed, r.currency))
        if r.promised - r.confirmed > 0:
            parts.append("promised %s more" % money(r.promised - r.confirmed, r.currency))
        parts.append("%d donor%s" % (r.donors, "" if r.donors == 1 else "s"))
    return {"id": r.id, "month": r.month, "label": need_label(r.purpose, r.medical_location),
            "event": EVENT_LABELS.get(r.event), "place": f"{r.district}, {r.province}", "goal": money(r.goal, r.currency),
            "euro": r.currency == "EUR", "text": r.public_text, "conf_pct": conf, "more_pct": max(0.0, prom - conf),
            "funding": (", ".join(parts) + ".") if parts else "No pledges yet."}


@router.get("/board")
def board(request: Request, purpose: str = "", province: str = "", sort: str = "new",
          page: int = Query(1, ge=1, le=1000)):
    if not _board_rate_ok(client_ip(request)):
        return _too_many()
    if _bad_filter(purpose, province) or sort not in SORTS:
        return Response("Unknown filter.", status_code=400, media_type="text/plain")
    _expire()
    where, params = ["TRUE"], {}
    if purpose:
        where.append("purpose = :pu")
        params["pu"] = purpose
    if province:
        where.append("province = :pv")
        params["pv"] = province
    w = " AND ".join(where)
    with engine.connect() as c:
        total = c.execute(text(f"SELECT count(*) FROM public_needs WHERE {w}"), params).scalar_one()
        rows = c.execute(text(f"SELECT {COLUMNS} FROM public_needs WHERE {w} ORDER BY {SORTS[sort]} LIMIT :lim OFFSET :off"),
                         {**params, "lim": PAGE_SIZE, "off": (page - 1) * PAGE_SIZE}).all()

    def link(n):
        q = {k: v for k, v in (("purpose", purpose), ("province", province), ("sort", sort if sort != "new" else "")) if v}
        if n > 1:
            q["page"] = n
        return "/board" + ("?" + urlencode(q) if q else "")

    return templates.TemplateResponse(request, "board.html", {
        "cards": [_card(r) for r in rows], "total": total, "purpose": purpose, "province": province, "sort": sort,
        "purposes": PURPOSES, "provinces": PROVINCES, "sorts": SORT_TEXT,
        "prev_url": link(page - 1) if page > 1 else None,
        "next_url": link(page + 1) if page * PAGE_SIZE < total else None})


@router.get("/board/map-data")
def board_map_data(request: Request, purpose: str = ""):
    if not _board_rate_ok(client_ip(request)):
        return _too_many()
    if _bad_filter(purpose, ""):
        return Response("Unknown filter.", status_code=400, media_type="text/plain")
    sql = "SELECT province, count(*) FROM public_needs"
    params = {}
    if purpose:
        sql += " WHERE purpose = :pu"
        params["pu"] = purpose
    sql += " GROUP BY province"
    with engine.connect() as c:
        counts = dict(c.execute(text(sql), params).all())
    data = [{"name": n, "pcode": code, "lat": la, "lon": lo, "count": int(counts.get(n, 0))} for code, n, la, lo in PROVINCES]
    return JSONResponse(data, headers={"Cache-Control": "public, max-age=60"})


@router.get("/board/{nid}")
def board_need(request: Request, nid: int):
    if not _board_rate_ok(client_ip(request)):
        return _too_many()
    _expire()
    with engine.connect() as c:
        r = c.execute(text(f"SELECT {COLUMNS} FROM public_needs WHERE id = :i"), {"i": nid}).one_or_none()
        s = c.execute(text("SELECT eur_try_rate, pledge_floor, pledge_expiry_days FROM settings")).one()
    if r is None:
        raise HTTPException(status_code=404, detail="not found")
    return templates.TemplateResponse(request, "board_need.html", {
        "c": _card(r), "rate": f"{s.eur_try_rate:.0f}", "goal_try": f"{r.goal * s.eur_try_rate:,.0f}",
        "floor": s.pledge_floor, "days": s.pledge_expiry_days})
