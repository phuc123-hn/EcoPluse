"""Chỉ số nước trong đất vùng rễ (SWI) tính từ chuỗi độ ẩm đất mặt NISAR (SSM) đã xử lý trong cache.

  SWI_n = SWI_(n-1) + K_n * [ SSM(t_n) - SWI_(n-1) ]
  K_n   = K_(n-1) / ( K_(n-1) + exp( -(t_n - t_(n-1)) / T ) )          K_1 = 1,  SWI_1 = SSM_1,  T = 30 ngày (mặc định)

t_n là thời điểm chụp (zeroDopplerStartTime, ghi UTC trong tên file) đổi sang giờ Việt Nam UTC+7. Mỗi điểm ảnh có t_(n-1) riêng
vì các cảnh phủ những vùng khác nhau. Chỉ đọc cache bản đồ, không chạm dữ liệu người dùng.
"""
import hashlib
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from PIL import Image

from . import hydro, render

router = APIRouter()
ICT = timezone(timedelta(hours=7))          # giờ Việt Nam
_lock = threading.Lock()


_sc = {"t": 0.0, "v": None}


def scenes():
    """Các cảnh độ ẩm đất đã xử lý, xếp theo thời gian chụp (giờ UTC+7). Nhớ 30 giây: hàm này bị gọi nhiều lần trong một yêu cầu
    (tính SWI, phiên bản lớp, lớp dữ liệu...) và mỗi lần phải đọc JSON của mọi cảnh."""
    if _sc["v"] is not None and time.time() - _sc["t"] < 30:
        return _sc["v"]
    out = []
    for j in render.CACHE.glob("*__soil.json"):
        m = hydro.SCENE_T.search(j.name)
        if not m or not (render.CACHE / f"{j.stem}.npy").exists():
            continue
        try:
            meta = json.loads(j.read_text())
        except Exception:
            continue
        if meta.get("status") == "done":
            t = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).astimezone(ICT)
            out.append((t, j.stem, meta["bounds"]))
    out = sorted(out, key=lambda x: x[0])
    _sc.update(t=time.time(), v=out)
    return out


def update(S, K, Tl, sub, day, T):
    """Một bước của công thức, sửa trực tiếp S (SWI), K, Tl (ngày của lần quan sát trước) tại các điểm có dữ liệu."""
    ok = np.isfinite(sub)
    first = ok & np.isnan(S)
    nxt = ok & ~first
    S[first], K[first], Tl[first] = sub[first], 1.0, day                          # K_1 = 1, SWI_1 = SSM_1
    if nxt.any():
        dt = day - Tl[nxt]
        k = K[nxt] / (K[nxt] + np.exp(-dt / T))
        K[nxt] = k
        S[nxt] = S[nxt] + k * (sub[nxt] - S[nxt])
        Tl[nxt] = day
    return ok


def compute(T=30.0):
    sc = scenes()
    if not sc:
        raise HTTPException(404, "Chưa có cảnh độ ẩm đất nào được xử lý trong cache")
    key = hashlib.md5((f"{T}|" + "|".join(s[1] for s in sc)).encode()).hexdigest()[:12]
    jf = render.CACHE / f"swi_{key}.json"
    if jf.exists():
        return json.loads(jf.read_text())
    with _lock:
        if jf.exists():
            return json.loads(jf.read_text())
        R = render.RES
        w, s_, e, n = min(b[0] for *_, b in sc), min(b[1] for *_, b in sc), max(b[2] for *_, b in sc), max(b[3] for *_, b in sc)
        W, H = int(round((e - w) / R)), int(round((n - s_) / R))
        swi, K, Tl = np.full((H, W), np.nan, np.float32), np.ones((H, W), np.float32), np.zeros((H, W), np.float64)
        n_obs, t0 = np.zeros((H, W), np.uint8), sc[0][0]
        for t, stem, b in sc:
            a = np.load(render.CACHE / f"{stem}.npy", mmap_mode="r")
            r0, c0 = int(round((n - b[3]) / R)), int(round((b[0] - w) / R))
            r1, c1 = min(H, r0 + a.shape[0]), min(W, c0 + a.shape[1])
            ok = update(swi[r0:r1, c0:c1], K[r0:r1, c0:c1], Tl[r0:r1, c0:c1], np.asarray(a[:r1 - r0, :c1 - c0]), (t - t0).total_seconds() / 86400, T)
            n_obs[r0:r1, c0:c1] += ok
        np.save(render.CACHE / f"swi_{key}.npy", swi)
        cls, ok = render.SOIL_CLS, np.isfinite(swi)
        idx, img = render._cls_idx(swi, cls), np.zeros((H, W, 4), np.uint8)
        for i, c in enumerate(cls):
            img[idx == i, :3] = [int(c["color"][j:j + 2], 16) for j in (1, 3, 5)]
        img[..., 3] = np.where(ok, 215, 0)
        Image.fromarray(img, "RGBA").save(render.CACHE / f"swi_{key}.png")
        last = sc[-1][0]
        meta = {"status": "done", "key": key, "T": T, "bounds": [w, s_, e, n], "png": f"/tiles/swi_{key}.png", "scenes": len(sc),
                "first_ict": sc[0][0].isoformat(timespec="minutes"), "last_ict": last.isoformat(timespec="minutes"),
                "age_days": round((datetime.now(ICT) - last).total_seconds() / 86400, 1), "mean": float(np.nanmean(swi)) if ok.any() else None,
                "mean_obs": float(n_obs[ok].mean()) if ok.any() else 0, "hist": [float(np.mean(idx[ok] == i)) for i in range(len(cls))] if ok.any() else None,
                "legend": {"title": "Chỉ số nước trong đất vùng rễ (SWI)", "cls": cls, "scale": 100, "unit": "%", "digits": 0,
                           "note": f"Tính từ chuỗi độ ẩm đất mặt NISAR bằng bộ lọc mũ, T = {T:g} ngày, mốc thời gian theo giờ Việt Nam (UTC+7). Điểm càng có nhiều lần quan sát thì càng tin cậy."}}
        jf.write_text(json.dumps(meta))
        return meta


@router.get("/api/swi", dependencies=[Depends(hydro.user)])
def api_swi(T: float = 30.0):
    if not 5 <= T <= 120:
        raise HTTPException(400, "T phải từ 5 đến 120 ngày")
    return compute(T)


@router.get("/api/swi/point", dependencies=[Depends(hydro.user)])
def api_swi_point(lng: float, lat: float, T: float = 30.0):
    """Chuỗi SSM và SWI tại một điểm (từng lần quan sát), để xem công thức chạy thế nào."""
    if not (101 <= lng <= 110 and 8 <= lat <= 24 and 5 <= T <= 120):
        raise HTTPException(400, "Tọa độ hoặc T không hợp lệ")
    rows, swi, k, tl = [], None, 1.0, None
    sc = scenes()
    for t, stem, _ in sc:
        v = render.value(stem, lng, lat)
        if v is None:
            continue
        day = (t - sc[0][0]).total_seconds() / 86400
        if swi is None:
            swi, k = v, 1.0
        else:
            k = k / (k + float(np.exp(-(day - tl) / T)))
            swi = swi + k * (v - swi)
        tl = day
        rows.append({"time_ict": t.isoformat(timespec="minutes"), "ssm_pct": round(v * 100, 1), "swi_pct": round(swi * 100, 1), "k": round(k, 3)})
    return {"T": T, "points": rows, "swi_pct": rows[-1]["swi_pct"] if rows else None}
