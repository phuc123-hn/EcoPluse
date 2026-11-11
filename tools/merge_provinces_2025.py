"""Gộp ranh giới các tỉnh CŨ thành tỉnh/thành MỚI sau sáp nhập 2025 (chỉ các nhóm quanh Tây Nguyên).

Cách dùng (đứng ở thư mục nisar-map):
    python tools/merge_provinces_2025.py vao.geojson static/provinces.geojson
    (thêm --field TênCột nếu tên tỉnh nằm ở cột lạ)

Đầu vào: file GeoJSON ranh giới tỉnh CŨ, hệ tọa độ EPSG:4326 (xuất từ QGIS).
Đầu ra : GeoJSON với các tỉnh đã gộp, mỗi đối tượng có "name" (tên mới) và "merged_from" (các tỉnh cũ đã gộp).
Tỉnh không thuộc nhóm nào dưới đây được giữ nguyên.
"""
import argparse
import json
import re
import sys
import unicodedata

from shapely.geometry import mapping, shape
from shapely.ops import unary_union

# Tên mới -> các tỉnh cũ gộp vào (viết từ hiểu biết về đợt sáp nhập 2025, bạn nên đối chiếu danh sách chính thức)
MERGES = {
    "Quảng Ngãi": ["Quảng Ngãi", "Kon Tum"],
    "Gia Lai": ["Gia Lai", "Bình Định"],
    "Đắk Lắk": ["Đắk Lắk", "Phú Yên"],
    "Lâm Đồng": ["Lâm Đồng", "Đắk Nông", "Bình Thuận"],
    "Đồng Nai": ["Đồng Nai", "Bình Phước"],
    "Khánh Hòa": ["Khánh Hòa", "Ninh Thuận"],
    "Thành phố Hồ Chí Minh": ["Hồ Chí Minh", "Bình Dương", "Bà Rịa - Vũng Tàu"],
    "Tây Ninh": ["Tây Ninh", "Long An"],
    "Đà Nẵng": ["Đà Nẵng", "Quảng Nam"],
}
NAME_KEYS = ("name", "NAME", "Name", "ten_tinh", "Ten_Tinh", "TEN_TINH", "TenTinh", "ten", "Ten",
             "NAME_1", "VARNAME_1", "ADM1_NAME", "shapeName", "province", "Province", "tinh", "Tinh")


def norm(s):
    s = unicodedata.normalize("NFD", str(s).lower().replace("đ", "d"))
    s = re.sub(r"[\u0300-\u036f]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"^(tinh|thanh pho|tp)\s+", "", s)
    return re.sub(r"\s+(province|city)$", "", s).strip()


OLD2NEW = {norm(o): new for new, olds in MERGES.items() for o in olds}


def get_name(props, field):
    for k in ([field] if field else []) + list(NAME_KEYS):
        v = props.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return next((v for v in props.values() if isinstance(v, str) and v.strip()), "?")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vao"); ap.add_argument("ra"); ap.add_argument("--field", default="")
    a = ap.parse_args()
    gj = json.load(open(a.vao, encoding="utf-8"))
    groups, keep = {}, []
    for f in gj["features"]:
        g = shape(f["geometry"])
        if not g.is_valid:
            g = g.buffer(0)
        name = get_name(f.get("properties") or {}, a.field)
        new = OLD2NEW.get(norm(name))
        (groups.setdefault(new, []) if new else keep).append((name, g, f.get("properties") or {}))
    xs = [v for items in list(groups.values()) + [keep] for _, g, _ in items for v in (g.bounds[0], g.bounds[2])]
    if xs and max(abs(v) for v in xs) > 360:
        sys.exit("File không ở hệ tọa độ độ (EPSG:4326). Xuất lại từ QGIS với CRS EPSG:4326.")
    if not groups:
        print("Không thấy tỉnh cũ nào cần gộp (Kon Tum, Bình Phước, Đắk Nông...). Có thể file đã là bản sau sáp nhập, hoặc tên cột khác: thử --field.")
    out = []
    for new, items in groups.items():
        found = [n for n, _, _ in items]
        want = MERGES[new]
        missing = [o for o in want if norm(o) not in {norm(n) for n in found}]
        geom = unary_union([g for _, g, _ in items])
        if len(items) > 1:  # khép các khe hở rất nhỏ giữa các tỉnh liền kề (~90 m)
            geom = geom.buffer(0.0008).buffer(-0.0008)
        flag = f"  THIẾU: {', '.join(missing)}" if missing else ""
        print(f"{new:24s} <- {', '.join(found)}{flag}")
        out.append({"type": "Feature", "properties": {"name": new, "ten_tinh": new, "merged_from": found, "thieu": missing},
                    "geometry": mapping(geom)})
    for name, g, props in keep:
        out.append({"type": "Feature", "properties": {"name": name, "ten_tinh": name, "merged_from": [name], "thieu": []}, "geometry": mapping(g)})
    json.dump({"type": "FeatureCollection", "features": out}, open(a.ra, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"\nĐã ghi {len(out)} đối tượng vào {a.ra} ({len(groups)} nhóm đã gộp, {len(keep)} giữ nguyên).")


if __name__ == "__main__":
    main()
