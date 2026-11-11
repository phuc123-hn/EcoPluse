"""Thời tiết cho bản đồ: lượng mưa, nhiệt độ, độ ẩm không khí trên lưới điểm phủ Tây Nguyên + Bình Phước.

Nguồn: Open-Meteo (https://open-meteo.com), miễn phí, không cần API key (dùng phi thương mại;
dùng thương mại cần gói trả phí của Open-Meteo). Dữ liệu là phân tích/dự báo mô hình, cập nhật theo giờ.
Backend gọi một lần rồi lưu đệm CACHE_TTL giây, nên nhiều người xem cũng chỉ tốn vài lượt gọi mỗi giờ.
"""
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException

from . import hydro

router = APIRouter()

AOI = (105.8, 10.3, 109.6, 15.7)      # W, S, E, N: phủ trọn 5 tỉnh sau sáp nhập (rộng hơn AOI của NISAR)
NX, NY = 11, 15                       # 11 x 15 = 165 điểm, khoảng 0,38 độ (~40 km) một ô
CACHE_TTL = 60 * 60                   # 1 giờ
CHUNK = 40                            # số điểm mỗi lần gọi Open-Meteo
URL = "https://api.open-meteo.com/v1/forecast"
_cache = {"t": 0.0, "data": None}


def _grid():
    w, s, e, n = AOI
    return [(round(s + (n - s) * j / (NY - 1), 3), round(w + (e - w) * i / (NX - 1), 3))
            for j in range(NY) for i in range(NX)]


def _fetch():
    pts, out = _grid(), []
    with httpx.Client(timeout=25) as cl:
        for k in range(0, len(pts), CHUNK):
            part = pts[k:k + CHUNK]
            r = cl.get(URL, params={
                "latitude": ",".join(str(p[0]) for p in part),
                "longitude": ",".join(str(p[1]) for p in part),
                "current": "temperature_2m,relative_humidity_2m",
                "daily": "precipitation_sum",
                "past_days": 1, "forecast_days": 2,
                "timezone": "Asia/Bangkok",
            })
            r.raise_for_status()
            js = r.json()
            js = js if isinstance(js, list) else [js]
            for (la, lo), it in zip(part, js):
                cur, day = it.get("current") or {}, it.get("daily") or {}
                out.append({"lat": la, "lon": lo, "t": cur.get("temperature_2m"),
                            "rh": cur.get("relative_humidity_2m"),
                            "r": day.get("precipitation_sum")})   # [hôm qua, hôm nay, ngày mai] (mm)
    day = (js[0].get("daily") or {}).get("time") if js else None
    obs = (js[0].get("current") or {}).get("time") if js else None   # giờ địa phương (Asia/Bangkok) của dữ liệu mô hình
    return {"updated": time.time(), "obs_time": obs, "days": day, "bbox": AOI, "points": out}


@router.get("/api/weather/grid", dependencies=[Depends(hydro.full)])
def weather_grid(refresh: int = 0):
    now = time.time()
    fresh = _cache["data"] and now - _cache["t"] < CACHE_TTL
    if fresh and not (refresh and now - _cache["t"] > 300):   # làm mới tay tối đa 5 phút một lần
        return _cache["data"]
    try:
        _cache["data"], _cache["t"] = _fetch(), now
    except Exception as e:
        if _cache["data"]:
            return {**_cache["data"], "stale": True}
        raise HTTPException(502, f"Không lấy được dữ liệu thời tiết: {e}")
    return _cache["data"]
