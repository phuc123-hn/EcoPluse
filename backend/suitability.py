"""Bản đồ đơn vị đất đai và chỉ số thích hợp cho cà phê vối, cao su, sầu riêng.

Phương pháp (GIS + AHP):  Si = Σ Wi · Xi   (Xi: điểm 3–9 theo nhóm giá trị của từng tiêu chí, Wi: trọng số AHP)
Phân hạng: 7–9 = S1, 6–7 = S2, 5–6 = S3, dưới 5 = N.  Cấu hình (nhóm giá trị, điểm, trọng số) nằm ở suitability_config.json.

Mỗi lớp dữ liệu là một mảng float32 NaN = không có dữ liệu, cùng một lưới (AOI, RES) tại  <thư mục dữ liệu>/layers/<id>.npy
(tạo bằng scripts/import_layer.py). Lớp "swi" lấy thẳng từ chuỗi độ ẩm đất NISAR trong cache (swi.py, giờ UTC+7).
Chỉ đọc lớp bản đồ và cấu hình, không chạm dữ liệu người dùng.
"""
import hashlib
import json
import os
from pathlib import Path

import numpy as np
from fastapi import APIRouter, Depends, HTTPException

from . import hydro, render, swi as swi_mod

router = APIRouter()
AOI, RES = (106.0, 11.0, 109.0, 15.6), 0.0015          # trùng với render.py
W, H = int(round((AOI[2] - AOI[0]) / RES)), int(round((AOI[3] - AOI[1]) / RES))
LAYERS = hydro.DB.parent / "layers"
LAYERS.mkdir(parents=True, exist_ok=True)
CFG_FILE = Path(os.getenv("SUIT_CONFIG") or Path(__file__).with_name("suitability_config.json"))
RAMP = [(0, "#2c7bb6"), (.25, "#abd9e9"), (.5, "#ffffbf"), (.75, "#fdae61"), (1, "#d7191c")]
_cfg, _swi = {"m": 0, "d": None}, {"k": None, "g": None}


def cfg():
    m = CFG_FILE.stat().st_mtime
    if _cfg["m"] != m:
        _cfg.update(m=m, d=json.loads(CFG_FILE.read_text(encoding="utf-8")))
    return _cfg["d"]


def swi_grid():
    meta = swi_mod.compute(30.0)
    if _swi["k"] == meta["key"]:
        return _swi["g"]
    a, b = np.load(render.CACHE / f"swi_{meta['key']}.npy", mmap_mode="r"), meta["bounds"]
    g = np.full((H, W), np.nan, np.float32)
    r0, c0 = int(round((AOI[3] - b[3]) / RES)), int(round((b[0] - AOI[0]) / RES))
    rs, cs, re, ce = max(0, r0), max(0, c0), min(H, r0 + a.shape[0]), min(W, c0 + a.shape[1])
    if re > rs and ce > cs:
        g[rs:re, cs:ce] = a[rs - r0:re - r0, cs - c0:ce - c0]
    _swi.update(k=meta["key"], g=g)
    return g


def layer_array(lid):
    if lid == "swi":
        try:
            return swi_grid()
        except HTTPException:
            return None
    f = LAYERS / f"{lid}.npy"
    if not f.exists():
        return None
    a = np.load(f, mmap_mode="r")
    if a.shape != (H, W):
        raise HTTPException(500, f"Lớp {lid} có kích thước {a.shape}, cần {(H, W)}. Hãy tạo lại bằng scripts/import_layer.py")
    return a


def layer_version(lid):
    if lid == "swi":
        try:
            return swi_mod.compute(30.0)["key"]
        except HTTPException:
            return "-"
    f = LAYERS / f"{lid}.npy"
    return str(int(f.stat().st_mtime)) if f.exists() else "-"


def in_class(crit, c, a):
    """Mặt nạ các ô thuộc nhóm giá trị c của tiêu chí."""
    if crit["type"] == "category":
        return a == c["code"]
    m = np.ones(a.shape, bool)
    if c.get("min") is not None:
        m &= (a >= c["min"]) if c.get("min_incl", 1) else (a > c["min"])
    if c.get("max") is not None:
        m &= (a <= c["max"]) if c.get("max_incl", 0) else (a < c["max"])
    return m


def score_of(c, crop):
    return (c["scores"].get(crop) or {}).get("score")


def score_raster(crit, crop, a):
    """Đổi giá trị lớp thành điểm Xi (3–9) theo bảng điểm của cây. None nếu tiêu chí không áp dụng cho cây."""
    a, out, ok = np.asarray(a, dtype=np.float32), None, False
    out = np.full(a.shape, np.nan, np.float32)
    for c in crit["classes"]:
        s = score_of(c, crop)
        if s is not None:
            ok = True
            out[in_class(crit, c, a)] = s
    return out if ok else None


def weights_for(crop, ids):
    w = (cfg().get("trong_so") or {}).get(crop) or {}
    got = {i: float(w[i]) for i in ids if w.get(i)}
    status = "ahp" if len(got) == len(ids) else "tam"          # thiếu trọng số AHP thì dùng trọng số bằng nhau (tạm)
    if status == "tam":
        got = {i: 1.0 for i in ids}
    t = sum(got.values())
    return {i: v / t for i, v in got.items()}, status


def plan(crop, sel):
    """Chia các tiêu chí được chọn thành: dùng được, thiếu lớp dữ liệu, chưa có bảng điểm."""
    C = cfg()
    if crop not in C["cay"]:
        raise HTTPException(400, "Cây trồng không hợp lệ")
    crit = C["tieu_chi"]
    use, missing, no_table = [], [], []
    for i in [i for i in (sel or list(crit)) if i in crit]:
        cr = crit[i]
        if not cr["classes"]:
            no_table.append(i)
        elif any(score_of(c, crop) is not None for c in cr["classes"]):       # còn lại: không áp dụng cho cây này
            (use if layer_array(cr["layer"]) is not None else missing).append(i)
    return use, missing, no_table


def classify(si):
    k = np.full(si.shape, 255, np.uint8)
    v = np.isfinite(si)
    k[v & (si < 5)] = 3
    k[v & (si >= 5) & (si < 6)] = 2
    k[v & (si >= 6) & (si < 7)] = 1
    k[v & (si >= 7)] = 0
    return k


def hex_rgb(h):
    return [int(h[i:i + 2], 16) for i in (1, 3, 5)]


def compute(crop, sel=None, min_frac=0.5):
    from PIL import Image
    C = cfg()
    use, missing, no_table = plan(crop, sel)
    if not use:
        raise HTTPException(404, "Chưa có lớp dữ liệu nào để tính. Thiếu lớp: " + (", ".join(missing) or "-") + "; chưa có bảng điểm: " + (", ".join(no_table) or "-"))
    w, wstat = weights_for(crop, use)
    key = hashlib.md5(json.dumps([crop, use, w, [layer_version(C["tieu_chi"][i]["layer"]) for i in use], _cfg["m"]]).encode()).hexdigest()[:12]
    jf = render.CACHE / f"land_{key}.json"
    if jf.exists():
        return json.loads(jf.read_text())
    num, den = np.zeros((H, W), np.float32), np.zeros((H, W), np.float32)
    for i in use:
        cr = C["tieu_chi"][i]
        x = score_raster(cr, crop, layer_array(cr["layer"]))
        m = np.isfinite(x)
        num[m] += x[m] * w[i]
        den[m] += w[i]
        del x, m
    si = np.where(den >= min_frac, num / np.maximum(den, 1e-9), np.nan).astype(np.float32)
    k = classify(si)
    img = np.zeros((H, W, 4), np.uint8)
    for j, p in enumerate(C["phan_hang"]):
        img[k == j, :3] = hex_rgb(p["mau"])
    img[..., 3] = np.where(k < 255, 215, 0)
    Image.fromarray(img, "RGBA").save(render.CACHE / f"land_{key}.png")
    lat = AOI[3] - (np.arange(H) + .5) * RES
    cell = (RES * 111.32) ** 2 * np.cos(np.radians(lat))                  # km² mỗi ô theo từng hàng
    area = [float(((k == j).sum(axis=1) * cell).sum()) for j in range(4)]
    tot = sum(area) or 1.0
    meta = {"status": "done", "key": key, "crop": crop, "bounds": list(AOI), "png": f"/tiles/land_{key}.png", "area_km2": [round(a, 1) for a in area],
            "frac": [round(a / tot, 4) for a in area], "mean_si": float(np.nanmean(si)) if np.isfinite(si).any() else None, "weights_status": wstat,
            "used": [{"id": i, "name": C["tieu_chi"][i]["name"], "w": round(w[i], 4)} for i in use],
            "missing_layers": [{"id": i, "name": C["tieu_chi"][i]["name"]} for i in missing], "no_table": [{"id": i, "name": C["tieu_chi"][i]["name"]} for i in no_table],
            "draft": sorted({i for i in use for c in C["tieu_chi"][i]["classes"] if (c["scores"].get(crop) or {}).get("status") == "nhap"}),
            "legend": C["phan_hang"]}
    jf.write_text(json.dumps(meta))
    return meta


def point(crop, lng, lat, sel=None):
    C = cfg()
    r, c = int((AOI[3] - lat) / RES), int((lng - AOI[0]) / RES)
    if not (0 <= r < H and 0 <= c < W):
        raise HTTPException(404, "Điểm nằm ngoài vùng nghiên cứu")
    use, _, _ = plan(crop, sel)
    w, wstat = weights_for(crop, use)
    rows, num, den = [], 0.0, 0.0
    for i in use:
        cr = C["tieu_chi"][i]
        v = float(layer_array(cr["layer"])[r, c])
        row = {"id": i, "name": cr["name"], "unit": cr["unit"], "w": round(w[i], 4), "value": None, "group": None, "score": None, "status": None}
        if np.isfinite(v):
            row["value"] = round(v, 3)
            for g in cr["classes"]:
                if score_of(g, crop) is not None and in_class(cr, g, np.array([v], np.float32))[0]:
                    row.update(group=g["label"], score=score_of(g, crop), status=g["scores"][crop].get("status"), contribution=round(score_of(g, crop) * w[i], 3))
                    num += score_of(g, crop) * w[i]
                    den += w[i]
                    break
        rows.append(row)
    si = num / den if den >= 0.5 else None
    cls = None if si is None else C["phan_hang"][int(classify(np.array([si]))[0])]
    return {"si": None if si is None else round(si, 2), "hang": cls and {"ma": cls["ma"], "ten": cls["ten"]}, "weights_status": wstat, "rows": rows}


def layer_png(lid, crop=None):
    """Ảnh phủ riêng của một lớp + chú giải + bảng điểm (mục lục riêng của từng đặc tính)."""
    from PIL import Image
    C = cfg()
    cr = C["tieu_chi"].get(lid)
    if not cr:
        raise HTTPException(404, "Không có lớp này")
    out = {"id": lid, "name": cr["name"], "unit": cr["unit"], "type": cr["type"], "note": cr.get("note"),
           "groups": [{"label": g["label"], "score": score_of(g, crop) if crop else None, "status": (g["scores"].get(crop) or {}).get("status") if crop else None} for g in cr["classes"]]}
    if lid == "swi":
        m = swi_mod.compute(30.0)
        return {**out, "png": m["png"], "bounds": m["bounds"], "legend": m["legend"], "info": f"Cập nhật: {m['last_ict']} (UTC+7), {m['scenes']} cảnh"}
    a = layer_array(lid)
    if a is None:
        raise HTTPException(404, "Chưa có dữ liệu cho lớp này")
    ver = layer_version(lid)
    png = render.CACHE / f"layer_{lid}_{ver}.png"
    img = np.zeros((H, W, 4), np.uint8)
    ok = np.isfinite(a)
    if cr["type"] == "category":
        cats = C.get("lop_nhom", {}).get(lid, [])
        for c in cats:
            img[np.asarray(a) == c["code"], :3] = hex_rgb(c["color"])
        out["legend"] = {"type": "category", "items": cats}
    else:
        s = np.asarray(a[::10, ::10])
        lo, hi = (np.nanpercentile(s, [2, 98]) if np.isfinite(s).any() else (0, 1))
        t = np.clip((np.nan_to_num(np.asarray(a)) - lo) / max(hi - lo, 1e-9), 0, 1)
        xs = [p[0] for p in RAMP]
        for ch in range(3):
            img[..., ch] = np.interp(t, xs, [hex_rgb(p[1])[ch] for p in RAMP])
        out["legend"] = {"type": "numeric", "min": float(lo), "max": float(hi), "stops": [p[1] for p in RAMP]}
    img[..., 3] = np.where(ok, 205, 0)
    if not png.exists():
        Image.fromarray(img, "RGBA").save(png)
    return {**out, "png": f"/tiles/{png.name}", "bounds": list(AOI)}


def ids(s):
    return [x for x in (s or "").split(",") if x] or None


@router.get("/api/land/config", dependencies=[Depends(hydro.user)])
def api_config():
    C = cfg()
    crit = []
    for i, cr in C["tieu_chi"].items():
        crops = [k for k in C["cay"] if any(score_of(g, k) is not None for g in cr["classes"])]
        crit.append({"id": i, "name": cr["name"], "unit": cr["unit"], "type": cr["type"], "note": cr.get("note"), "has_table": bool(cr["classes"]),
                     "has_data": layer_array(cr["layer"]) is not None, "crops": crops,
                     "groups": [{"label": g["label"], "scores": {k: g["scores"].get(k) for k in C["cay"]}} for g in cr["classes"]]})
    return {"cay": C["cay"], "phan_hang": C["phan_hang"], "phuong_phap": C["phuong_phap"], "tieu_chi": crit}


@router.get("/api/land/layer", dependencies=[Depends(hydro.user)])
def api_layer(id: str, crop: str = ""):
    return layer_png(id, crop or None)


@router.get("/api/land/suitability", dependencies=[Depends(hydro.user)])
def api_suit(crop: str, criteria: str = ""):
    return compute(crop, ids(criteria))


@router.get("/api/land/point", dependencies=[Depends(hydro.user)])
def api_point(crop: str, lng: float, lat: float, criteria: str = ""):
    return point(crop, lng, lat, ids(criteria))
