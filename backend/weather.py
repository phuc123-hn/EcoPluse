"""Thời tiết cho bản đồ: lượng mưa, nhiệt độ, độ ẩm không khí, gió trên lưới điểm phủ Tây Nguyên + Bình Phước,
hải lưu (dòng chảy mặt biển) ở Biển Đông, và bảng thời tiết chi tiết cho MỘT điểm (chọn nhóm/biến để xem).

Nguồn: Open-Meteo (https://open-meteo.com), miễn phí, không cần API key (dùng phi thương mại;
dùng thương mại cần gói trả phí của Open-Meteo). Dữ liệu là phân tích/dự báo mô hình.
 - Thời tiết: Forecast API, lưới khoảng 28 km, đệm 2 giờ.
 - Hải lưu: Marine API (ocean_current_velocity, ocean_current_direction; mô hình SMOC của Météo-France,
   ~8 km, cập nhật mỗi ngày), lưới 0,4 độ, đệm 6 giờ. Điểm nằm trên đất liền không có giá trị (None).
 - Điểm chi tiết: cùng Forecast API nhưng nhiều biến (theo ngày + theo giờ), đệm 1 giờ cho mỗi điểm.
Không có số liệu nào được tự sinh thêm: nơi nào Open-Meteo không trả về thì để trống.

Hạn mức miễn phí của Open-Meteo: 600 lượt/phút, 5.000 lượt/giờ, 10.000 lượt/ngày. Mỗi điểm lưới tính một lượt;
gọi nhiều hơn 10 biến hoặc hơn 7 ngày thì tính thành nhiều lượt. Vì vậy mọi lần gọi đều đi qua _get():
giãn nhịp theo phút, chỉ một luồng gọi ra ngoài một lúc, gặp 429 thì chờ thử lại, và nghỉ một lúc sau khi lỗi.
"""
import json
import math
import os
import re
import threading
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException

from . import hydro

router = APIRouter()


def _env(k, d):
    try:
        return int(os.environ.get(k, d))
    except ValueError:
        return d


AOI = (105.8, 10.3, 109.6, 15.7)      # W, S, E, N: phủ trọn 5 tỉnh sau sáp nhập (rộng hơn AOI của NISAR)
NX, NY = _env("WEATHER_NX", 16), _env("WEATHER_NY", 22)   # mặc định 16 x 22 = 352 điểm (~0,25 độ, ~28 km); 8 x 11 chỉ còn 88 điểm
CACHE_TTL = 2 * 60 * 60               # 2 giờ (352 điểm x 12 lần/ngày, nằm trong hạn mức miễn phí)
CHUNK = 40                            # số điểm mỗi lần gọi Open-Meteo
PAUSE = 0.25                          # nghỉ giữa các nhóm gọi để không dồn dập
URL = "https://api.open-meteo.com/v1/forecast"
_cache = {"t": 0.0, "data": None, "fail": 0.0, "wait": 0, "file": "grid"}

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
SEA_AOI = (105.0, 8.0, 113.0, 16.4)   # W, S, E, N: Biển Đông phía đông Tây Nguyên và Nam Trung Bộ
SEA_NX, SEA_NY = _env("SEA_NX", 21), _env("SEA_NY", 22)   # mặc định bước 0,4 độ, 462 điểm; 11 x 11 chỉ còn 121 điểm
SEA_TTL = 6 * 60 * 60                 # dữ liệu hải lưu chỉ cập nhật mỗi ngày
SEA_FALLBACK_BUDGET = 120             # số lượt gọi lẻ tối đa khi một nhóm bị lỗi (do có điểm trên đất liền)
_sea = {"t": 0.0, "data": None, "fail": 0.0, "wait": 0, "file": "sea"}

# ---------------------------------------------------------------- giới hạn lượt gọi dùng chung
PER_MIN = 400                         # tự giữ dưới hạn mức 600 lượt/phút của Open-Meteo
DAILY_CAP = 8000                      # ngân sách nội bộ mỗi ngày (UTC), chừa chỗ dưới hạn mức 10.000 lượt của Open-Meteo
FAIL_COOLDOWN = 120                   # sau lỗi thường, không gọi lại ngay (giây)
LIMIT_COOLDOWN = 30 * 60              # sau khi hết hạn mức giờ/ngày: nghỉ 30 phút rồi tự thử lại
CACHE_DIR = os.environ.get("WEATHER_CACHE_DIR", "/tmp")   # nơi lưu bản sao dữ liệu cuối cùng (xem _save)
_lock = threading.Lock()              # chỉ một luồng gọi Open-Meteo tại một thời điểm
_win = []                             # [(thời điểm, số lượt)] trong 60 giây gần nhất
_used = {"day": "", "n": 0}           # số lượt MÁY CHỦ NÀY đã gọi trong ngày (UTC)


class RateLimited(Exception):
    """Bị giới hạn lượt gọi (429 từ Open-Meteo hoặc hết ngân sách nội bộ). `wait` = số giây nên nghỉ trước khi thử lại."""

    def __init__(self, msg, wait=FAIL_COOLDOWN):
        super().__init__(msg)
        self.wait = wait


def _pace(cost):
    """Đếm lượt đã dùng trong ngày; chờ nếu 60 giây gần nhất đã dùng gần hết hạn mức phút."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if _used["day"] != today:
        _used.update(day=today, n=0)
    if _used["n"] + cost > DAILY_CAP:
        raise RateLimited(f"máy chủ đã gọi {_used['n']}/{DAILY_CAP} lượt hôm nay (UTC), tạm dừng để không vượt hạn mức", LIMIT_COOLDOWN)
    while True:
        now = time.time()
        _win[:] = [(t, c) for t, c in _win if now - t < 60]
        if not _win or sum(c for _, c in _win) + cost <= PER_MIN:
            _win.append((now, cost))
            _used["n"] += cost
            return
        time.sleep(max(0.5, 60 - (now - _win[0][0])))


def _get(cl, url, params, cost=1):
    """GET + JSON. 429 theo phút: chờ rồi thử lại (tối đa 3 lần). 429 theo giờ/ngày: dừng ngay vì chờ vài giây vô ích."""
    low = ""
    for k in range(3):
        _pace(cost)
        r = cl.get(url, params=params)
        if r.status_code != 429:
            r.raise_for_status()
            return r.json()
        try:
            low = str(r.json().get("reason", "")).lower()
        except Exception:
            low = ""
        if low and "minute" not in low:
            break
        if k < 2:
            time.sleep(20 * (k + 1))
    if "daily" in low:
        raise RateLimited("Open-Meteo báo hết hạn mức NGÀY (tính theo IP máy chủ; Render dùng chung IP nên có thể do app khác)", LIMIT_COOLDOWN)
    if "hourly" in low:
        raise RateLimited("Open-Meteo báo hết hạn mức GIỜ", LIMIT_COOLDOWN)
    raise RateLimited(low or "429 Too Many Requests")


def _save(store):
    """Lưu bản dữ liệu cuối cùng ra đĩa để lần khởi động sau còn có gì hiển thị khi nguồn đang bị chặn.
    Trên Render chỉ giữ được qua lần deploy/khởi động lại nếu bạn gắn Persistent Disk và trỏ WEATHER_CACHE_DIR vào đó."""
    try:
        path = os.path.join(CACHE_DIR, f"weather_{store['file']}.json")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"t": store["t"], "data": store["data"]}, f)
        os.replace(path + ".tmp", path)
    except Exception:
        pass


def _load(store):
    try:
        with open(os.path.join(CACHE_DIR, f"weather_{store['file']}.json"), encoding="utf-8") as f:
            d = json.load(f)
        store["data"], store["t"] = d["data"], d["t"]
    except Exception:
        pass


def _ok(store, ttl, refresh, gap):
    """Bộ nhớ đệm còn mới? Làm mới tay (refresh=1) chỉ có hiệu lực nếu đã quá `gap` giây từ lần nạp trước."""
    now = time.time()
    return bool(store["data"]) and now - store["t"] < ttl and not (refresh and now - store["t"] > gap)


def _serve(store, ttl, refresh, gap, fetch, name):
    if _ok(store, ttl, refresh, gap):
        return store["data"]
    with _lock:
        if _ok(store, ttl, refresh, gap):          # luồng khác vừa nạp xong trong lúc mình chờ khóa
            return store["data"]
        left = store["wait"] - (time.time() - store["fail"])
        if left > 0:                               # vừa lỗi: chưa gọi lại, dùng dữ liệu cũ nếu có
            if store["data"]:
                return {**store["data"], "stale": True}
            raise HTTPException(502, f"{name}: nguồn đang bị giới hạn lượt gọi, tự thử lại sau khoảng {math.ceil(left / 60)} phút")
        try:
            store["data"], store["t"] = fetch(), time.time()
            _save(store)
        except Exception as e:
            store["fail"], store["wait"] = time.time(), getattr(e, "wait", FAIL_COOLDOWN)
            if store["data"]:
                return {**store["data"], "stale": True}
            raise HTTPException(502, f"Không lấy được dữ liệu {name}: {e}")
        return store["data"]


for _s in (_cache, _sea):
    _load(_s)


@router.get("/api/weather/usage", dependencies=[Depends(hydro.user)])
def weather_usage():
    """Máy chủ này đã gọi Open-Meteo bao nhiêu lượt hôm nay? Nếu con số nhỏ mà vẫn bị báo hết hạn mức ngày
    thì hạn mức đang bị tính chung với app khác trên cùng IP."""
    now = time.time()
    return {"day_utc": _used["day"], "used_today": _used["n"], "cap": DAILY_CAP,
            "grid_age_min": round((now - _cache["t"]) / 60) if _cache["data"] else None,
            "sea_age_min": round((now - _sea["t"]) / 60) if _sea["data"] else None,
            "grid_retry_in_min": max(0, math.ceil((_cache["wait"] - (now - _cache["fail"])) / 60)),
            "sea_retry_in_min": max(0, math.ceil((_sea["wait"] - (now - _sea["fail"])) / 60)),
            "points_cached": len(_points)}


# ---------------------------------------------------------------- lưới thời tiết
def _grid(aoi, nx, ny):
    w, s, e, n = aoi
    return [(round(s + (n - s) * j / (ny - 1), 3), round(w + (e - w) * i / (nx - 1), 3))
            for j in range(ny) for i in range(nx)]


def _fetch():
    pts, out, js = _grid(AOI, NX, NY), [], []
    with httpx.Client(timeout=25) as cl:
        for k in range(0, len(pts), CHUNK):
            part = pts[k:k + CHUNK]
            js = _get(cl, URL, {
                "latitude": ",".join(str(p[0]) for p in part),
                "longitude": ",".join(str(p[1]) for p in part),
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,weather_code",
                "daily": "precipitation_sum",
                "past_days": 1, "forecast_days": 2,
                "timezone": "Asia/Bangkok",
            }, cost=len(part))
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
    return _serve(_cache, CACHE_TTL, refresh, 300, _fetch, "thời tiết")   # làm mới tay tối đa 5 phút một lần


# ---------------------------------------------------------------- hải lưu
def _marine(cl, part):
    js = _get(cl, MARINE_URL, {
        "latitude": ",".join(str(p[0]) for p in part),
        "longitude": ",".join(str(p[1]) for p in part),
        "current": "ocean_current_velocity,ocean_current_direction",
        "timezone": "Asia/Bangkok",
    }, cost=len(part))
    return js if isinstance(js, list) else [js]


def _fetch_sea():
    pts, out, obs, budget = _grid(SEA_AOI, SEA_NX, SEA_NY), [], None, SEA_FALLBACK_BUDGET
    with httpx.Client(timeout=25) as cl:
        for k in range(0, len(pts), CHUNK):
            part = pts[k:k + CHUNK]
            try:
                js = _marine(cl, part)
            except RateLimited:
                raise            # bị giới hạn lượt gọi thì dừng hẳn, không thử từng điểm (sẽ tốn thêm lượt)
            except Exception:
                js = []          # có thể do điểm trên đất liền làm lỗi cả nhóm: thử từng điểm (có giới hạn)
                for p in part:
                    if budget <= 0:
                        js.append({})
                        continue
                    budget -= 1
                    try:
                        js += _marine(cl, [p])[:1]
                    except RateLimited:
                        raise
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
    return _serve(_sea, SEA_TTL, refresh, 900, _fetch_sea, "hải lưu")     # làm mới tay tối đa 15 phút một lần


# ---------------------------------------------------------------- một điểm, nhiều biến (chọn nhóm/biến để xem)
# Danh sách biến lấy đúng từ link Open-Meteo của bạn. Muốn nhẹ hơn thì xoá bớt tên ở đây hoặc giảm POINT_PAST/POINT_FUTURE.
DAILY = (
    "weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_max,apparent_temperature_min,"
    "uv_index_max,uv_index_clear_sky_max,wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant,"
    "shortwave_radiation_sum,et0_fao_evapotranspiration,sunrise,sunset,daylight_duration,sunshine_duration,"
    "moonrise,moonset,moon_phase,rain_sum,showers_sum,snowfall_sum,precipitation_sum,precipitation_hours,"
    "precipitation_probability_max,temperature_2m_mean,apparent_temperature_mean,cape_mean,cape_max,cape_min,"
    "cloud_cover_mean,cloud_cover_max,cloud_cover_min,dew_point_2m_mean,dew_point_2m_max,dew_point_2m_min,"
    "wet_bulb_temperature_2m_mean,wet_bulb_temperature_2m_max,wet_bulb_temperature_2m_min,"
    "vapour_pressure_deficit_max,et0_fao_evapotranspiration_sum,growing_degree_days_base_0_limit_50,"
    "leaf_wetness_probability_mean,precipitation_probability_mean,precipitation_probability_min,"
    "relative_humidity_2m_mean,relative_humidity_2m_max,relative_humidity_2m_min,snowfall_water_equivalent_sum,"
    "pressure_msl_mean,pressure_msl_max,pressure_msl_min,surface_pressure_mean,surface_pressure_max,updraft_max,"
    "surface_pressure_min,visibility_mean,visibility_min,visibility_max,wind_gusts_10m_mean,wind_speed_10m_mean,"
    "wind_gusts_10m_min,wind_speed_10m_min"
).split(",")
HOURLY = (
    "temperature_2m,relative_humidity_2m,dew_point_2m,apparent_temperature,precipitation_probability,precipitation,"
    "showers,rain,snowfall,snow_depth,weather_code,pressure_msl,surface_pressure,cloud_cover,cloud_cover_low,"
    "cloud_cover_mid,cloud_cover_high,visibility,evapotranspiration,et0_fao_evapotranspiration,"
    "vapour_pressure_deficit,wind_speed_10m,wind_speed_80m,wind_speed_120m,wind_speed_180m,wind_direction_10m,"
    "wind_direction_80m,wind_direction_120m,wind_direction_180m,wind_gusts_10m,temperature_80m,temperature_120m,"
    "temperature_180m,soil_temperature_0cm,soil_temperature_6cm,soil_temperature_18cm,soil_temperature_54cm,"
    "soil_moisture_0_to_1cm,soil_moisture_1_to_3cm,soil_moisture_3_to_9cm,soil_moisture_9_to_27cm,"
    "soil_moisture_27_to_81cm"
).split(",")
CURRENT = (
    "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,wind_speed_10m,wind_direction_10m,"
    "wind_gusts_10m,precipitation,rain,showers,snowfall,weather_code,cloud_cover,pressure_msl,surface_pressure"
).split(",")

POINT_BOX = (102.0, 5.0, 115.0, 20.0)  # W, S, E, N: chỉ cho chọn điểm trong vùng này (tránh ai đó gọi bừa tốn hạn mức)
POINT_PAST, POINT_FUTURE = 7, 16       # ngày quá khứ / ngày dự báo (giống link của bạn)
POINT_TTL = 60 * 60                    # 1 giờ cho mỗi điểm
POINT_MAX = 24                         # giữ tối đa 24 điểm trong bộ nhớ
POINT_COST = 40                        # ước tính số lượt Open-Meteo tính cho một lần gọi nhiều biến x nhiều ngày
_points = {}                           # (lat, lon) -> {"t": thời điểm nạp, "data": JSON Open-Meteo}
_pfail = {"t": 0.0, "wait": 30}

BASE = {
    "weather_code": "Mã thời tiết (WMO)", "temperature_2m": "Nhiệt độ 2 m", "apparent_temperature": "Nhiệt độ cảm nhận",
    "dew_point_2m": "Điểm sương", "wet_bulb_temperature_2m": "Nhiệt độ bầu ướt", "relative_humidity_2m": "Độ ẩm tương đối",
    "vapour_pressure_deficit": "Thiếu hụt áp suất hơi (VPD)", "leaf_wetness_probability": "Xác suất ướt lá",
    "growing_degree_days_base_0_limit_50": "Tích ôn GDD (0–50 °C)", "precipitation": "Lượng giáng thủy", "rain": "Mưa",
    "showers": "Mưa rào", "snowfall": "Tuyết rơi", "snowfall_water_equivalent": "Tuyết quy ra nước", "snow_depth": "Độ dày tuyết",
    "precipitation_hours": "Số giờ có mưa", "precipitation_probability": "Xác suất mưa", "wind_speed_10m": "Tốc độ gió 10 m",
    "wind_gusts_10m": "Gió giật 10 m", "wind_direction_10m": "Hướng gió 10 m", "updraft": "Dòng khí thăng",
    "pressure_msl": "Áp suất mực nước biển", "surface_pressure": "Áp suất bề mặt", "cloud_cover": "Độ phủ mây",
    "cloud_cover_low": "Mây tầng thấp", "cloud_cover_mid": "Mây tầng giữa", "cloud_cover_high": "Mây tầng cao",
    "visibility": "Tầm nhìn xa", "cape": "CAPE (năng lượng đối lưu)", "uv_index": "Chỉ số UV",
    "uv_index_clear_sky": "Chỉ số UV (trời quang)", "shortwave_radiation": "Bức xạ sóng ngắn",
    "et0_fao_evapotranspiration": "Bốc thoát hơi ET0 (FAO)", "evapotranspiration": "Bốc thoát hơi nước",
    "sunrise": "Mặt trời mọc", "sunset": "Mặt trời lặn", "daylight_duration": "Thời gian ban ngày",
    "sunshine_duration": "Thời gian có nắng", "moonrise": "Mặt trăng mọc", "moonset": "Mặt trăng lặn",
    "moon_phase": "Pha mặt trăng", "is_day": "Ban ngày (1) / ban đêm (0)",
}
HEIGHT = {"wind_speed": "Tốc độ gió", "wind_direction": "Hướng gió", "temperature": "Nhiệt độ"}
SUFFIX = (("_max", "cao nhất"), ("_min", "thấp nhất"), ("_mean", "trung bình"), ("_sum", "tổng"), ("_dominant", "chiếm ưu thế"))
GROUPS = (   # kiểm tra theo thứ tự, khớp từ khoá đầu tiên thì lấy nhóm đó
    ("Tổng quan", ("weather_code",)),
    ("Đất", ("soil",)),
    ("Gió", ("wind", "updraft")),
    ("Mưa & tuyết", ("rain", "shower", "snow", "precip")),
    ("Mây & tầm nhìn", ("cloud", "visibility", "cape")),
    ("Độ ẩm & áp suất", ("humidity", "vapour", "leaf_wetness", "dew_point", "pressure")),
    ("Nhiệt độ", ("temperature", "wet_bulb", "growing_degree")),
    ("Nắng, bức xạ & thiên văn", ("uv_index", "radiation", "sun", "daylight", "moon", "et0", "evapotranspiration", "is_day")),
)


def _label(n):
    for suf, txt in SUFFIX:
        if n.endswith(suf):
            return f"{_label(n[:-len(suf)])} ({txt})"
    if n in BASE:
        return BASE[n]
    m = re.fullmatch(r"(wind_speed|wind_direction|temperature)_(\d+)m", n)
    if m:
        return f"{HEIGHT[m[1]]} {m[2]} m"
    m = re.fullmatch(r"soil_temperature_(\d+)cm", n)
    if m:
        return f"Nhiệt độ đất {m[1]} cm"
    m = re.fullmatch(r"soil_moisture_(\d+)_to_(\d+)cm", n)
    if m:
        return f"Độ ẩm đất {m[1]}–{m[2]} cm"
    return n


def _group(n):
    for g, keys in GROUPS:
        if any(k in n for k in keys):
            return g
    return "Khác"


def _point(lat, lon, refresh=0):
    w, s, e, n = POINT_BOX
    if not (s <= lat <= n and w <= lon <= e):
        raise HTTPException(400, "Toạ độ nằm ngoài vùng cho phép")
    key = (round(lat, 1), round(lon, 1))     # làm tròn 0,1 độ: nhiều lần bấm gần nhau dùng chung một lần gọi

    def fresh():
        ent, now = _points.get(key), time.time()
        return ent if ent and now - ent["t"] < POINT_TTL and not (refresh and now - ent["t"] > 600) else None

    if fresh():
        return key, fresh()
    with _lock:
        if fresh():
            return key, fresh()
        old = _points.get(key)
        if time.time() - _pfail["t"] < _pfail["wait"]:
            if old:
                return key, old
            raise HTTPException(502, "Điểm chi tiết: nguồn đang bị giới hạn lượt gọi, thử lại sau ít phút")
        try:
            with httpx.Client(timeout=40) as cl:
                data = _get(cl, URL, {
                    "latitude": key[0], "longitude": key[1],
                    "current": ",".join(CURRENT), "daily": ",".join(DAILY), "hourly": ",".join(HOURLY),
                    "past_days": POINT_PAST, "forecast_days": POINT_FUTURE, "timezone": "Asia/Bangkok",
                }, cost=POINT_COST)
        except Exception as e:
            _pfail.update(t=time.time(), wait=getattr(e, "wait", 30))
            if old:
                return key, old
            raise HTTPException(502, f"Không lấy được dữ liệu điểm chi tiết: {e}")
        ent = _points[key] = {"t": time.time(), "data": data}
        while len(_points) > POINT_MAX:
            del _points[next(iter(_points))]
        return key, ent


def _unit(units, n):
    u = (units or {}).get(n, "")
    return "" if u == "iso8601" else u


@router.get("/api/weather/point", dependencies=[Depends(hydro.user)])
def weather_point(lat: float = 16.0, lon: float = 108.0, refresh: int = 0):
    """Thông tin hiện tại + danh mục biến đã chia nhóm. Chuỗi số liệu của từng biến lấy ở /api/weather/point/series."""
    key, ent = _point(lat, lon, refresh)
    js = ent["data"]
    cur, cu = js.get("current") or {}, js.get("current_units") or {}
    groups = {}
    for scope in ("daily", "hourly"):
        block, units = js.get(scope) or {}, js.get(f"{scope}_units") or {}
        for n in (DAILY if scope == "daily" else HOURLY):
            if n in block:   # biến nào Open-Meteo không trả thì bỏ qua
                groups.setdefault(_group(n), []).append(
                    {"scope": scope, "name": n, "label": _label(n), "unit": _unit(units, n)})
    return {"lat": key[0], "lon": key[1], "elevation": js.get("elevation"), "updated": ent["t"],
            "obs_time": cur.get("time"),
            "current": [{"name": n, "label": _label(n), "value": cur.get(n), "unit": _unit(cu, n)} for n in CURRENT],
            "groups": groups}


@router.get("/api/weather/point/series", dependencies=[Depends(hydro.user)])
def weather_point_series(lat: float = 16.0, lon: float = 108.0, scope: str = "daily", var: str = "temperature_2m_max"):
    """Một biến: danh sách thời điểm + giá trị (dùng bộ nhớ đệm của /api/weather/point, thường không tốn thêm lượt gọi)."""
    if scope not in ("daily", "hourly"):
        raise HTTPException(400, "scope phải là daily hoặc hourly")
    key, ent = _point(lat, lon)
    js = ent["data"]
    block = js.get(scope) or {}
    if var == "time" or var not in block:
        raise HTTPException(404, "Không có biến này")
    return {"scope": scope, "var": var, "label": _label(var), "unit": _unit(js.get(f"{scope}_units"), var),
            "time": block.get("time", []), "values": block[var]}
