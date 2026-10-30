import json, os, sys

SRC, OUT = sys.argv[1], sys.argv[2]
EPS = 0.01      # simplification tolerance in degrees, about 1 km
DECIMALS = 3    # about 110 m


def rdp(points, eps):
    n = len(points)
    if n < 3:
        return points
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        s, e = stack.pop()
        (x1, y1), (x2, y2) = points[s], points[e]
        dx, dy = x2 - x1, y2 - y1
        norm = (dx * dx + dy * dy) ** 0.5
        dmax, idx = 0.0, -1
        for i in range(s + 1, e):
            x, y = points[i]
            d = abs(dy * (x - x1) - dx * (y - y1)) / norm if norm else ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
            if d > dmax:
                dmax, idx = d, i
        if dmax > eps and idx != -1:
            keep[idx] = True
            stack.append((s, idx))
            stack.append((idx, e))
    return [p for p, k in zip(points, keep) if k]


def ring(coords):
    pts = [[round(x, DECIMALS), round(y, DECIMALS)] for x, y in coords]
    out = rdp(pts, EPS)
    return out if len(out) >= 4 else None


def polygon(rings):
    outer = ring(rings[0])
    if outer is None:
        return None
    holes = [r for r in (ring(h) for h in rings[1:]) if r]
    return [outer] + holes


with open(SRC, encoding="utf-8") as fh:
    g = json.load(fh)

feats, before, after = [], 0, 0
for f in g["features"]:
    geom = f["geometry"]
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    before += sum(len(r) for p in polys for r in p)
    kept = [q for q in (polygon(p) for p in polys) if q]
    if not kept:
        raise SystemExit("a province lost all its geometry: " + str(f["properties"]["adm1_pcode"]))
    after += sum(len(r) for p in kept for r in p)
    pr = f["properties"]
    feats.append({"type": "Feature",
                  "properties": {"pcode": pr["adm1_pcode"], "name": pr["adm1_name"],
                                 "lat": round(pr["center_lat"], 3), "lon": round(pr["center_lon"], 3)},
                  "geometry": {"type": "MultiPolygon", "coordinates": kept}})
feats.sort(key=lambda x: x["properties"]["pcode"])

with open(OUT, "w", encoding="utf-8") as fh:
    json.dump({"type": "FeatureCollection", "features": feats}, fh, separators=(",", ":"), ensure_ascii=True)

codes = [f["properties"]["pcode"] for f in feats]
names = [f["properties"]["name"] for f in feats]
print("features:", len(feats), "| unique pcodes:", len(set(codes)), "| unique names:", len(set(names)))
print("points before:", before, "after:", after)
print("output KB:", os.path.getsize(OUT) // 1024)
xs = [x for f in feats for p in f["geometry"]["coordinates"] for r in p for x, _ in r]
ys = [y for f in feats for p in f["geometry"]["coordinates"] for r in p for _, y in r]
print("longitude range:", min(xs), max(xs), "| latitude range:", min(ys), max(ys))
