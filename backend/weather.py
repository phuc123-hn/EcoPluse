"""Thời tiết cho bản đồ: lượng mưa, nhiệt độ, độ ẩm không khí, gió trên lưới điểm phủ Tây Nguyên + Bình Phước,
và hải lưu (dòng chảy mặt biển) ở Biển Đông.

Nguồn: Open-Meteo (https://open-meteo.com), miễn phí, không cần API key (dùng phi thương mại;
dùng thương mại cần gói trả phí của Open-Meteo). Dữ liệu là phân tích/dự báo mô hình.
 - Thời tiết: Forecast API, lưới khoảng 28 km, đệm 2 giờ.
 - Hải lưu: Marine API (ocean_current_velocity, ocean_current_direction; mô hình SMOC của Météo-France,
   ~8 km, cập nhật mỗi ngày), lưới 0,4 độ, đệm 6 giờ. Điểm nằm trên đất liền không có giá trị (None).
Không có số liệu nào được tự sinh thêm: nơi nào Open-Meteo không trả về thì để trống.
Mỗi điểm lưới tính một lượt gọi (giới hạn miễn phí 10.000 lượt mỗi ngày), nên đệm và nghỉ giữa các nhóm gọi.
"""
import math
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException

from . import hydro

router = APIRouter()

AOI = (105.8, 10.3, 109.6, 15.7)      # W, S, E, N: phủ trọn 5 tỉnh sau sáp nhập (rộng hơn AOI của NISAR)
NX, NY = 16, 22                       # 16 x 22 = 352 điểm, khoảng 0,25 độ (~28 km) một ô
CACHE_TTL = 2 * 60 * 60               # 2 giờ (352 điểm x 12 lần/ngày, nằm trong hạn mức miễn phí)
CHUNK = 40                            # số điểm mỗi lần gọi Open-Meteo
PAUSE = 0.25                          # nghỉ giữa các nhóm gọi để không dồn dập
URL = "https://api.open-meteo.com/v1/forecast"
_cache = {"t": 0.0, "data": None}

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
SEA_AOI = (105.0, 8.0, 113.0, 16.4)   # W, S, E, N: Biển Đông phía đông Tây Nguyên và Nam Trung Bộ
SEA_NX, SEA_NY = 21, 22               # bước 0,4 độ, 462 điểm
SEA_TTL = 6 * 60 * 60                 # dữ liệu hải lưu chỉ cập nhật mỗi ngày
SEA_FALLBACK_BUDGET = 120             # số lượt gọi lẻ tối đa khi một nhóm bị lỗi (do có điểm trên đất liền)
_sea = {"t": 0.0, "data": None, "fail": 0.0}


def _grid(aoi, nx, ny):
    w, s, e, n = aoi
    return [(round(s + (n - s) * j / (ny - 1), 3), round(w + (e - w) * i / (nx - 1), 3))
            for j in range(ny) for i in range(nx)]


def _fetch():
    pts, out = _grid(AOI, NX, NY), []
    with httpx.Client(timeout=25) as cl:
        for k in range(0, len(pts), CHUNK):
            part = pts[k:k + CHUNK]
            r = cl.get(URL, params={
                "latitude": ",".join(str(p[0]) for p in part),
                "longitude": ",".join(str(p[1]) for p in part),
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,weather_code",
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
                            "rh": cur.get("relative_humidity_2m"), "ws": cur.get("wind_speed_10m"), "wd": cur.get("wind_direction_10m"), "wc": cur.get("weather_code"),
                            "r": day.get("precipitation_sum")})   # [hôm qua, hôm nay, ngày mai] (mm)
            time.sleep(PAUSE)
    day = (js[0].get("daily") or {}).get("time") if js else None
    obs = (js[0].get("current") or {}).get("time") if js else None   # giờ địa phương (Asia/Bangkok) của dữ liệu mô hình
    return {"updated": time.time(), "obs_time": obs, "days": day, "bbox": AOI, "nx": NX, "ny": NY, "points": out}


@router.get("/api/weather/grid", dependencies=[Depends(hydro.user)])
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


def _marine(cl, part):
    r = cl.get(MARINE_URL, params={
        "latitude": ",".join(str(p[0]) for p in part),
        "longitude": ",".join(str(p[1]) for p in part),
        "current": "ocean_current_velocity,ocean_current_direction",
        "timezone": "Asia/Bangkok",
    })
    r.raise_for_status()
    js = r.json()
    return js if isinstance(js, list) else [js]


def _fetch_sea():
    pts, out, obs, budget = _grid(SEA_AOI, SEA_NX, SEA_NY), [], None, SEA_FALLBACK_BUDGET
    with httpx.Client(timeout=25) as cl:
        for k in range(0, len(pts), CHUNK):
            part = pts[k:k + CHUNK]
            try:
                js = _marine(cl, part)
            except Exception:
                js = []          # có thể do điểm trên đất liền làm lỗi cả nhóm: thử từng điểm (có giới hạn)
                for p in part:
                    if budget <= 0:
                        js.append({})
                        continue
                    budget -= 1
                    try:
                        js += _marine(cl, [p])[:1]
                    except Exception:
                        js.append({})
            for (la, lo), it in zip(part, js):
                cur = it.get("current") or {}
                v, d = cur.get("ocean_current_velocity"), cur.get("ocean_current_direction")
                obs = obs or cur.get("time")
                if v is None or d is None:
                    out.append({"lat": la, "lon": lo, "u": None, "v": None})
                else:   # d là hướng dòng chảy tới (0 = về Bắc, 90 = về Đông); u hướng Đông, v hướng Bắc, đơn vị km/h
                    rad = math.radians(d)
                    out.append({"lat": la, "lon": lo, "u": round(v * math.sin(rad), 3), "v": round(v * math.cos(rad), 3)})
            time.sleep(PAUSE)
    if not any(p["u"] is not None for p in out):
        raise RuntimeError("Marine API không trả giá trị hải lưu nào")
    return {"updated": time.time(), "obs_time": obs, "bbox": SEA_AOI, "nx": SEA_NX, "ny": SEA_NY, "unit": "km/h", "points": out}


@router.get("/api/weather/sea", dependencies=[Depends(hydro.user)])
def weather_sea(refresh: int = 0):
    now = time.time()
    fresh = _sea["data"] and now - _sea["t"] < SEA_TTL
    if fresh and not (refresh and now - _sea["t"] > 900):     # làm mới tay tối đa 15 phút một lần
        return _sea["data"]
    if not _sea["data"] and now - _sea["fail"] < 60:          # vừa lỗi thì đợi, tránh gọi dồn
        raise HTTPException(502, "Hải lưu: nguồn vừa lỗi, thử lại sau ít phút")
    try:
        _sea["data"], _sea["t"] = _fetch_sea(), now
    except Exception as e:
        _sea["fail"] = now
        if _sea["data"]:
            return {**_sea["data"], "stale": True}
        raise HTTPException(502, f"Không lấy được dữ liệu hải lưu: {e}")
    return _sea["data"]
