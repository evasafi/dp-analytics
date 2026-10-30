import json

with open("app/static/geo/turkey_provinces.min.geojson", encoding="utf-8") as fh:
    feats = json.load(fh)["features"]
rows = [(f["properties"]["pcode"], f["properties"]["name"], f["properties"]["lat"], f["properties"]["lon"]) for f in feats]
assert len(rows) == 81 and len({r[1] for r in rows}) == 81
lines = ['"""The 81 provinces of Turkiye.',
         '',
         'Names, codes and centre points come from the OCHA COD-AB Turkiye dataset (CC BY-IGO),',
         'source: General Command of Mapping, Turkiye, via OCHA/HDX. Names are plain ASCII.',
         '"""',
         '',
         'PROVINCES = [']
lines += [f"    ({c!r}, {n!r}, {la}, {lo})," for c, n, la, lo in rows]
lines += [']', '', 'PROVINCE_NAMES = {p[1] for p in PROVINCES}', '']
with open("app/provinces.py", "w", encoding="utf-8", newline="\n") as fh:
    fh.write("\n".join(lines))
print("wrote app/provinces.py with", len(rows), "provinces")
