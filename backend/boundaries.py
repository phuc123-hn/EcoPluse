"""Ranh giới các tỉnh HIỆN TẠI (sau sáp nhập 2025) quanh Tây Nguyên + Bình Phước, cho nút "Hiện đường ranh giới".

Tỉnh mới được GỘP từ ranh giới tỉnh cũ:
  Quảng Ngãi = Quảng Ngãi + Kon Tum          Gia Lai    = Gia Lai + Bình Định      Đắk Lắk  = Đắk Lắk + Phú Yên
  Lâm Đồng   = Lâm Đồng + Đắk Nông + Bình Thuận                                    Đồng Nai = Đồng Nai + Bình Phước

Thứ tự tìm dữ liệu (dùng cái đầu tiên có):
  1. static/provinces.geojson hoặc static/ranh-gioi.geojson  (xuất từ QGIS, EPSG:4326; cả bản 63 tỉnh cũ lẫn bản tỉnh mới đều được)
  2. cache/ranh-gioi-2025.geojson                            (kết quả đã gộp từ lần trước)
  3. Tải 1 lần ranh giới 63 tỉnh cũ từ geoBoundaries / GADM rồi gộp, lưu vào cache
Muốn đổi dữ liệu: xóa cache/ranh-gioi-2025.geojson rồi khởi động lại server. (File cache/ranh-gioi.geojson cũ không còn dùng.)
"""
import io
import json
import re
import threading
import unicodedata
import zipfile
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException

ROOT = Path(__file__).resolve().parent.parent
LOCAL = [ROOT / "static" / "provinces.geojson", ROOT / "static" / "ranh-gioi.geojson"]
CACHED = ROOT / "cache" / "ranh-gioi-2025.geojson"
REGION = {   # tỉnh MỚI -> các tỉnh CŨ gộp vào (đối chiếu danh sách chính thức nếu cần)
    "Quảng Ngãi": ["Quảng Ngãi", "Kon Tum"],
    "Gia Lai": ["Gia Lai", "Bình Định"],
    "Đắk Lắk": ["Đắk Lắk", "Phú Yên"],
    "Lâm Đồng": ["Lâm Đồng", "Đắk Nông", "Bình Thuận"],
    "Đồng Nai": ["Đồng Nai", "Bình Phước"],
}
ALIAS = {"daclac": "daklak", "dacnong": "daknong"}   # cách viết khác của Đắk Lắk, Đắk Nông
MIN_FOUND = len(REGION)   # đủ cả 5 tỉnh mới thì mới dùng
CLOSE = 0.002             # khép khe hở giữa các tỉnh liền kề khi gộp (~220 m)
URLS = [        # đều là ranh giới 63 tỉnh CŨ, sẽ được gộp thành tỉnh mới
    "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/gbOpen/VNM/ADM1/geoBoundaries-VNM-ADM1_simplified.geojson",
    "https://raw.githubusercontent.com/wmgeolab/geoBoundaries/9469f09/releaseData/gbOpen/VNM/ADM1/geoBoundaries-VNM-ADM1_simplified.geojson",
    "https://geodata.ucdavis.edu/gadm/gadm4.1/json/gadm41_VNM_1.json.zip",
    "https://github.com/wmgeolab/geoBoundaries/raw/main/releaseData/gbOpen/VNM/ADM1/geoBoundaries-VNM-ADM1_simplified.geojson",
]

router = APIRouter()
_lock = threading.Lock()
_mem = None


def _key(s):
    """'Đắk Lắk', 'ĐắkLắk', 'Dak Lak', 'Tỉnh Đăk Lăk' -> 'daklak'."""
    k = unicodedata.normalize("NFD", str(s).lower())
    k = "".join(c for c in k if not unicodedata.combining(c)).replace("đ", "d")
    k = re.sub(r"[^a-z]", "", k)
    if k.startswith("tinh"):
        k = k[4:]
    k = re.sub(r"(province|city)$", "", k)
    return ALIAS.get(k, k)


def _round(c, n=5):
    return round(c, n) if isinstance(c, (int, float)) else [_round(x, n) for x in c]


def _slim(g):
    """Làm nhẹ hình (đơn giản hóa ~80 m, làm tròn 5 số lẻ) để file gửi về trình duyệt nhỏ."""
    try:
        from shapely.geometry import mapping, shape
        s = shape(g)
        if not s.is_valid:
            s = s.buffer(0)
        m = mapping(s.simplify(0.0007, preserve_topology=True))
        if m["type"] in ("Polygon", "MultiPolygon"):
            g = m
    except ImportError:
        pass
    return {"type": g["type"], "coordinates": _round(g["coordinates"])}


PRIMARY = {_key(n) for n in REGION}                                   # tên trùng với tên tỉnh mới
SECONDARY = {_key(o) for olds in REGION.values() for o in olds[1:]}   # tỉnh cũ đã bị gộp (Kon Tum, Bình Định...)


def _build(gj):
    """Tìm các tỉnh trong một FeatureCollection bất kỳ rồi gộp thành 5 tỉnh mới. Thiếu dữ liệu thì trả None."""
    try:
        from shapely.geometry import mapping, shape
        from shapely.ops import unary_union
    except ImportError:
        raise HTTPException(503, "Thiếu thư viện shapely: chạy  python -m pip install shapely")
    got = {}
    for f in (gj.get("features") or []) if isinstance(gj, dict) else []:
        if not f.get("geometry"):
            continue
        for v in (f.get("properties") or {}).values():
            if isinstance(v, str) and _key(v) in PRIMARY | SECONDARY:
                got.setdefault(_key(v), f)
                break
    already_new = not (SECONDARY & got.keys())   # file không còn Kon Tum, Bình Định... tức đã là bản sau sáp nhập
    feats = []
    for new, olds in REGION.items():
        parts = [(o, got[_key(o)]) for o in olds if _key(o) in got]
        if not parts or (not already_new and len(parts) < len(olds)):
            return None   # thiếu tỉnh thành phần: nguồn này chưa đủ
        geoms = []
        for _, f in parts:
            g = shape(f["geometry"])
            geoms.append(g if g.is_valid else g.buffer(0))
        g = unary_union(geoms)
        if len(geoms) > 1:
            g = g.buffer(CLOSE).buffer(-CLOSE)
        feats.append({"type": "Feature", "properties": {"label": new, "old": [o for o, _ in parts]},
                      "geometry": _slim(mapping(g))})
    return {"type": "FeatureCollection", "features": feats}


def _from_files():
    for p in LOCAL:
        if p.exists():
            try:
                out = _build(json.loads(p.read_text(encoding="utf-8")))
                if out:
                    return out
            except Exception:
                pass
    return None


def _download():
    errs = []
    with httpx.Client(timeout=120, follow_redirects=True) as c:
        for u in URLS:
            host = u.split("/")[2]
            try:
                r = c.get(u)
                r.raise_for_status()
                data = r.content
                if u.endswith(".zip"):
                    with zipfile.ZipFile(io.BytesIO(data)) as z:
                        data = z.read(next(n for n in z.namelist() if n.lower().endswith(".json")))
                out = _build(json.loads(data))
                if out:
                    return out, errs
                errs.append(f"{host}: không đủ tỉnh")
            except Exception as e:
                errs.append(f"{host}: {type(e).__name__}")
    return None, errs


@router.get("/api/boundaries")
def boundaries():
    global _mem
    with _lock:
        if _mem:
            return _mem
        out = _from_files()
        if not out and CACHED.exists():
            try:
                c = json.loads(CACHED.read_text(encoding="utf-8"))
                out = c if len(c.get("features", [])) >= MIN_FOUND else None
            except Exception:
                out = None
        if not out:
            out, errs = _download()
            if not out:
                raise HTTPException(503, "Server không tải được ranh giới (" + "; ".join(errs) + "). "
                                    "Hãy xuất ranh giới các tỉnh từ QGIS (EPSG:4326) rồi lưu thành static/provinces.geojson.")
            CACHED.parent.mkdir(exist_ok=True)
            CACHED.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        _mem = out
        return out
