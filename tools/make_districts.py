import json, sys

with open(sys.argv[1], encoding="utf-8") as fh:
    feats = json.load(fh)["features"]
rows = sorted((f["properties"]["adm1_name"], f["properties"]["adm2_name"], f["properties"]["adm2_pcode"]) for f in feats)
assert len(rows) == 973
assert len({r[2] for r in rows}) == 973
assert len({(r[0], r[1]) for r in rows}) == 973
assert all(2 <= len(r[1]) <= 40 and r[1].isascii() for r in rows)
lines = ['"""The 973 districts of Turkiye, grouped by province.',
         '',
         'Names and codes come from the OCHA COD-AB Turkiye dataset (CC BY-IGO),',
         'source: General Command of Mapping, Turkiye, via OCHA/HDX. Names are plain ASCII.',
         '"""',
         '',
         'DISTRICTS = [']
lines += [f"    ({p!r}, {d!r}, {c!r})," for p, d, c in rows]
lines += [']', '', 'DISTRICTS_BY_PROVINCE: dict = {}',
          'for _p, _d, _c in DISTRICTS:',
          '    DISTRICTS_BY_PROVINCE.setdefault(_p, []).append(_d)', '']
with open("app/districts.py", "w", encoding="utf-8", newline="\n") as fh:
    fh.write("\n".join(lines))
print("wrote app/districts.py with", len(rows), "districts")
