"""Xác định mỗi cảnh NISAR nằm trên những tỉnh nào.
Ưu tiên file static/provinces.geojson (xuất từ QGIS, EPSG:4326). Không có file thì dùng khung gần đúng của 6 tỉnh.
"""
import json
import os
from pathlib import Path

from shapely.geometry import box, shape
from shapely.strtree import STRtree

FILE = Path(__file__).resolve().parent.parent / "static" / "provinces.geojson"
NAME_KEYS = ("name", "NAME", "Name", "ten_tinh", "Ten_Tinh", "TEN_TINH", "TenTinh", "ten", "Ten",
             "NAME_1", "VARNAME_1", "ADM1_NAME", "shapeName", "province", "Province", "tinh", "Tinh")
BOXES = {  # gần đúng (Tây, Nam, Đông, Bắc), chỉ dùng khi chưa có file ranh giới
    "Kon Tum": (107.2, 13.8, 108.5, 15.4), "Gia Lai": (107.6, 12.9, 108.9, 14.6),
    "Đắk Lắk": (107.4, 12.2, 108.9, 13.4), "Đắk Nông": (107.2, 11.7, 108.1, 12.5),
    "Lâm Đồng": (107.3, 11.2, 108.7, 12.3), "Bình Phước": (106.4, 11.3, 107.5, 12.3),
}
_state = {}


def _name(props):
    forced = os.getenv("PROVINCE_NAME_FIELD", "").strip()
    for k in ([forced] if forced else []) + list(NAME_KEYS):
        v = props.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for v in props.values():
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "?"


def _load():
    if _state:
        return _state
    items, approx, warn = [], True, None
    if FILE.exists():
        try:
            gj = json.loads(FILE.read_text(encoding="utf-8"))
            for f in gj.get("features", []):
                g = shape(f["geometry"])
                if not g.is_valid:
                    g = g.buffer(0)
                items.append((_name(f.get("properties") or {}), g.simplify(0.002)))
            xs = [v for _, g in items for v in (g.bounds[0], g.bounds[2])]
            if not items:
                raise ValueError("file không có đối tượng nào")
            if max(abs(v) for v in xs) > 360:
                raise ValueError("file không ở hệ tọa độ độ (EPSG:4326). Hãy xuất lại từ QGIS với CRS EPSG:4326")
            approx = False
            if sum(n == "?" for n, _ in items) > len(items) / 2:
                warn = "không đọc được tên tỉnh, đặt PROVINCE_NAME_FIELD=tên_cột trong .env"
        except Exception as e:
            warn = f"Không dùng được provinces.geojson: {e}. Đang dùng khung gần đúng."
            items, approx = [], True
    if approx:
        items = [(n, box(*b)) for n, b in BOXES.items()]
    _state.update(items=items, approx=approx, warn=warn, tree=STRtree([g for _, g in items]))
    return _state


def info():
    s = _load()
    return {"approx": s["approx"], "count": len(s["items"]), "warn": s["warn"]}


def for_scene(geom):
    """Trả về các tỉnh giao với cảnh: share = phần cảnh nằm trong tỉnh, cover = phần tỉnh được cảnh phủ."""
    s = _load()
    sc = shape(geom)
    if sc.area == 0:
        return []
    out = []
    for i in s["tree"].query(sc, predicate="intersects"):
        name, g = s["items"][int(i)]
        a = sc.intersection(g).area
        share, cover = a / sc.area, (a / g.area if g.area else 0)
        if share >= 0.01 or cover >= 0.01:
            out.append({"name": name, "share": round(share, 3), "cover": round(min(cover, 1.0), 3)})
    return sorted(out, key=lambda x: -x["share"])
