"""The public board: open needs with their funding, one page per need, and the map data.

Sign-in is optional. A visitor with a valid session sees their name in the header; the board never sets a cookie.
It reads only the public_needs and public_stats views, so a private column cannot reach these pages.
A limit of 60 requests a minute per address is kept in memory.
"""
import threading
import time
from collections import deque
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from app import items
from app.auth import current_user
from app.db import engine
from app.provinces import PROVINCE_NAMES, PROVINCES
from app.reference import EVENTS, PURPOSE_LABELS, PURPOSES, money, need_label
from app.web import client_ip, templates

router = APIRouter()
PAGE_SIZE = 20
BOARD_LIMIT, BOARD_WINDOW = 60, 60  # requests per window (seconds), per address, in memory
_RATE: dict = {}
_RATE_LOCK = threading.Lock()
CONF = "CASE WHEN need_kind = 'item' THEN confirmed_units::numeric / quantity_needed ELSE confirmed / goal END"
PROM = "CASE WHEN need_kind = 'item' THEN promised_units::numeric / quantity_needed ELSE promised / goal END"
SORTS = {"new": "id DESC", "least": CONF + " ASC, id DESC", "near": PROM + " DESC, id DESC"}
SORT_TEXT = {"new": "Newest", "least": "Least funded", "near": "Closest to the goal"}
EVENT_LABELS = dict(EVENTS)
COLUMNS = ("id, to_char(month, 'YYYY-MM') AS month, purpose, medical_location, event, province, district, currency, goal, "
           "public_text, promised, confirmed, donors, need_kind, item_category, item_name, quantity_needed, used_ok, "
           "handover_method, promised_units, confirmed_units, claimants")


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


def _viewer(request: Request):
    """The signed-in user, or None. A missing, expired or invalid session just means a visitor."""
    try:
        return current_user(request)
    except HTTPException:
        return None


def _too_many():
    return Response("Too many requests. Try again in a minute.", status_code=429, media_type="text/plain")


def _bad_filter(purpose: str, province: str, kind: str = "", category: str = "") -> bool:
    return ((bool(purpose) and purpose not in PURPOSE_LABELS) or (bool(province) and province not in PROVINCE_NAMES)
            or kind not in ("", "money", "item") or (bool(category) and category not in items.CATEGORY_LABELS))


def _expire():
    """Pledges nobody marked as sent in time stop counting. Idempotent, and a no-op when nothing is overdue."""
    with engine.begin() as c:
        c.execute(text("SELECT expire_pledges()"))


def _item_card(r):
    q = int(r.quantity_needed)
    conf_u, prom_u = int(r.confirmed_units), int(r.promised_units)
    parts = []
    if r.claimants:
        parts.append(f"{conf_u} of {q} units confirmed" if conf_u else "Nothing confirmed yet")
        if prom_u - conf_u > 0:
            parts.append(f"{prom_u - conf_u} more promised" if conf_u else f"{prom_u} promised")
        parts.append("%d donor%s" % (r.claimants, "" if r.claimants == 1 else "s"))
    conf = min(100.0, conf_u / q * 100)
    prom = min(100.0, prom_u / q * 100)
    handover = dict(items.HANDOVER_METHODS).get(r.handover_method, "").replace(" (recommended)", "")
    return {"id": r.id, "month": r.month, "label": r.item_name, "event": None, "place": f"{r.district}, {r.province}",
            "goal": f"{q} unit" + ("" if q == 1 else "s"), "item": True, "euro": False, "text": r.public_text,
            "conf_pct": conf, "more_pct": max(0.0, prom - conf),
            "funding": (", ".join(parts) + ".") if parts else "No pledges yet.",
            "used_text": "A used item is fine" if r.used_ok else "New items only", "handover_label": handover}


def _card(r):
    if r.need_kind == "item":
        return _item_card(r)
    goal = float(r.goal)
    conf = min(100.0, float(r.confirmed) / goal * 100)
    prom = min(100.0, float(r.promised) / goal * 100)
    parts = []
    if r.donors:
        parts.append("Confirmed " + money(r.confirmed, r.currency) if r.confirmed else "Nothing confirmed yet")
        if r.promised - r.confirmed > 0:
            parts.append(("promised %s more" if r.confirmed else "%s promised") % money(r.promised - r.confirmed, r.currency))
        parts.append("%d donor%s" % (r.donors, "" if r.donors == 1 else "s"))
    return {"id": r.id, "month": r.month, "label": need_label(r.purpose, r.medical_location),
            "event": EVENT_LABELS.get(r.event), "place": f"{r.district}, {r.province}", "goal": money(r.goal, r.currency),
            "euro": r.currency == "EUR", "text": r.public_text, "conf_pct": conf, "more_pct": max(0.0, prom - conf),
            "funding": (", ".join(parts) + ".") if parts else "No pledges yet."}


def _stats_card(r):
    return {"fulfilled": r.fulfilled_needs, "confirmed": money(r.confirmed_try, "TRY"), "donors": r.donors}


@router.get("/board")
def board(request: Request, purpose: str = "", province: str = "", kind: str = "", category: str = "", sort: str = "new",
          page: int = Query(1, ge=1, le=1000)):
    if not _board_rate_ok(client_ip(request)):
        return _too_many()
    if _bad_filter(purpose, province, kind, category) or sort not in SORTS:
        return Response("Unknown filter.", status_code=400, media_type="text/plain")
    _expire()
    where, params = ["TRUE"], {}
    if purpose:
        where.append("purpose = :pu")
        params["pu"] = purpose
    if province:
        where.append("province = :pv")
        params["pv"] = province
    if kind:
        where.append("need_kind = :kd")
        params["kd"] = kind
    if category:
        where.append("item_category = :ct")
        params["ct"] = category
    w = " AND ".join(where)
    with engine.connect() as c:
        total = c.execute(text(f"SELECT count(*) FROM public_needs WHERE {w}"), params).scalar_one()
        rows = c.execute(text(f"SELECT {COLUMNS} FROM public_needs WHERE {w} ORDER BY {SORTS[sort]} LIMIT :lim OFFSET :off"),
                         {**params, "lim": PAGE_SIZE, "off": (page - 1) * PAGE_SIZE}).all()
        stats = _stats_card(c.execute(text("SELECT fulfilled_needs, confirmed_try, donors FROM public_stats")).one())

    def link(n):
        q = {k: v for k, v in (("purpose", purpose), ("province", province), ("kind", kind), ("category", category),
                              ("sort", sort if sort != "new" else "")) if v}
        if n > 1:
            q["page"] = n
        return "/board" + ("?" + urlencode(q) if q else "")

    return templates.TemplateResponse(request, "board.html", {
        "viewer": _viewer(request), "stats": stats, "cards": [_card(r) for r in rows], "total": total, "purpose": purpose, "province": province, "sort": sort,
        "kind": kind, "category": category, "categories": list(items.CATEGORY_LABELS.items()),
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
        "viewer": _viewer(request), "c": _card(r), "rate": f"{s.eur_try_rate:.0f}", "goal_try": ("" if r.need_kind == "item" else f"{r.goal * s.eur_try_rate:,.0f}"),
        "floor": s.pledge_floor, "days": s.pledge_expiry_days})
