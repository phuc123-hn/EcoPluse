"""Tạo static/rivers.geojson từ shapefile sông suối (OSM waterway) để nút "Hiện mạch nước" dùng.

Chạy:   python scripts/build_rivers.py  [đường/dẫn/waterway-river.shp]      (mặc định: data/waterway-river.shp)
Cần:    pip install geopandas   (chỉ để chuyển đổi; server chạy không cần geopandas)
Kết quả giữ sông, suối, kênh nằm trong khung Tây Nguyên + Bình Phước, bỏ cống và đường ngầm, làm nhẹ hình (~30 m).
"""
import json
import sys
from pathlib import Path

import geopandas as gpd
from shapely.geometry import box

AOI = (106.0, 11.0, 109.0, 15.6)      # khớp AOI trong backend/main.py
SIMPLIFY = 0.0003                      # độ, ~30 m
TYPES = ("river", "stream", "canal")
SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "data/waterway-river.shp")
OUT = Path(__file__).resolve().parent.parent / "static" / "rivers.geojson"


def first(row, cols):
    for c in cols:
        v = row.get(c)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def rnd(c):
    return [round(c[0], 5), round(c[1], 5)] if isinstance(c[0], (int, float)) else [rnd(x) for x in c]


g = gpd.read_file(SRC)
g = g.set_crs(4326) if g.crs is None else g.to_crs(4326)
n0 = len(g)
g = g[g["waterway"].isin(TYPES)]
if "tunnel" in g:
    g = g[~g["tunnel"].isin(["yes", "culvert", "building"])]   # cống, đường ngầm: không phải mạch nước nhìn thấy
g = g.clip(box(*AOI)).explode(index_parts=False)
g = g[~g.geometry.is_empty & (g.geom_type == "LineString")]
g["geometry"] = g.geometry.simplify(SIMPLIFY, preserve_topology=True)
feats = []
for _, r in g.iterrows():
    if r.geometry.is_empty or len(r.geometry.coords) < 2:
        continue
    p = {"t": r["waterway"]}
    n = first(r, ("name", "name_vi", "alt_name_v"))
    if n:
        p["n"] = n
    en = first(r, ("name_en",))
    if en:
        p["e"] = en
    if r.get("intermitte") == "yes":
        p["i"] = 1
    feats.append({"type": "Feature", "properties": p, "geometry": {"type": "LineString", "coordinates": rnd([list(c) for c in r.geometry.coords])}})
OUT.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
from collections import Counter
print(f"đọc {n0} đối tượng -> giữ {len(feats)} đoạn trong khung AOI | {Counter(f['properties']['t'] for f in feats)} | có tên: {sum('n' in f['properties'] for f in feats)} | {OUT.stat().st_size/1e6:.2f} MB -> {OUT}")
