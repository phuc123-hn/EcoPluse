"""NISAR live map – backend.
Chạy:  python -m uvicorn backend.main:app --reload --port 8000
"""
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from . import assistant, boundaries, hydro, render, rivers, weather
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

ASF_URL = "https://api.daac.asf.alaska.edu/services/search/param"
# Tây Nguyên + Bình Phước (W, S, E, N) – chỉnh tại đây nếu cần
AOI = (106.0, 11.0, 109.0, 15.6)
LEVELS = {"SME2", "GCOV", "GSLC", "RSLC"}
ALLOWED_HOST = "https://nisar.asf.earthdatacloud.nasa.gov/"
ALLOWED_HOSTNAME = "nisar.asf.earthdatacloud.nasa.gov"
ID_RE = re.compile(r"^[A-Za-z0-9_]{1,200}$")                  # id cảnh NISAR: chữ, số, gạch dưới (ASCII)
TILE_RE = re.compile(r"^[A-Za-z0-9_\-]{1,250}\.png$")          # chỉ phục vụ ảnh PNG đã xử lý, không phục vụ .h5/.npy/.json trong cache
MAX_JOBS = int(os.getenv("MAX_RENDER_JOBS") or 2)             # số cảnh được xử lý cùng lúc (mỗi cảnh tải file HDF5 rất lớn)
_render_lock = threading.Lock()
CACHE_TTL = 15 * 60  # giây
_cache: dict = {}

# PRODUCTION=1 thì tắt trang tài liệu API công khai (/docs, /redoc, /openapi.json)
app = FastAPI(title="EcoPulse", **(dict(docs_url=None, redoc_url=None, openapi_url=None) if hydro.IS_PROD else {}))


@app.middleware("http")
async def security_headers(request: Request, call_next):
    r = await call_next(request)
    h = r.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "SAMEORIGIN")                    # /app nhúng /map cùng origin nên dùng SAMEORIGIN
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "geolocation=(self)")
    # Chưa đặt script-src vì giao diện còn dùng onclick/onerror và script inline; các chỉ thị dưới đây vẫn an toàn và có tác dụng
    h.setdefault("Content-Security-Policy", "frame-ancestors 'self'; base-uri 'self'; object-src 'none'; form-action 'self'")
    if hydro.COOKIE_SECURE:
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    if "application/json" in h.get("content-type", ""):
        h.setdefault("Cache-Control", "no-store")                    # dữ liệu cá nhân không được lưu đệm
    return r


def _url_ok(url):
    try:
        p = urlparse(url)
        return p.scheme == "https" and p.hostname == ALLOWED_HOSTNAME and not p.username and not p.password and p.port in (None, 443)
    except ValueError:
        return False
app.include_router(hydro.router)
app.include_router(boundaries.router)
app.include_router(rivers.router)
app.include_router(weather.router)
app.include_router(assistant.router)


def _wkt(b):
    w, s, e, n = b
    return f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"


@app.get("/api/config")
def config():
    return {"maptilerKey": os.getenv("NEXT_PUBLIC_MAPTILER_KEY", ""), "aoi": AOI}


@app.get("/api/scenes", dependencies=[Depends(hydro.full)])
async def scenes(level: str = "SME2", days: int = 60):
    level = level.upper()
    if level not in LEVELS:
        raise HTTPException(400, f"level phải thuộc {sorted(LEVELS)}")
    days = max(1, min(days, 365))

    key = (level, days)
    hit = _cache.get(key)
    if hit and time.time() - hit["t"] < CACHE_TTL:
        return hit["data"]

    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "dataset": "NISAR",
        "processingLevel": level,
        "intersectsWith": _wkt(AOI),
        "start": start,
        "maxResults": 500,
        "output": "geojson",
    }
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.get(ASF_URL, params=params)
            r.raise_for_status()
            gj = r.json()
    except Exception as e:  # mạng lỗi / ASF lỗi -> dùng cache cũ nếu có
        if hit:
            return {**hit["data"], "stale": True}
        print(f"[EcoPulse] Không gọi được ASF: {type(e).__name__}: {e}", flush=True)
        raise HTTPException(502, "Không gọi được máy chủ ASF lúc này, hãy thử lại sau")

    feats = gj.get("features", []) if isinstance(gj, dict) else []
    data = {
        "type": "FeatureCollection",
        "features": feats,
        "meta": {
            "count": len(feats),
            "level": level,
            "days": days,
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
        },
    }
    _cache[key] = {"t": time.time(), "data": data}
    return data


@app.get("/api/render")
def api_render(id: str, url: str, layer: str, retry: int = 0, u=Depends(hydro.full)):
    if layer not in render.LAYERS or not ID_RE.fullmatch(id) or not _url_ok(url):
        raise HTTPException(400, "url/id/layer không hợp lệ")
    key = f"{id}__{layer}"
    with _render_lock:
        # Cảnh đã xử lý xong hoặc đang chạy thì ai đăng nhập cũng xem được trạng thái. Bắt đầu cảnh MỚI (tải file lớn, tốn CPU/RAM/đĩa) chỉ dành cho cán bộ.
        if render.cached_meta(key) is None and render.JOBS.get(key, {}).get("status") != "running":
            if u["role"] not in hydro.ADMINS:
                raise HTTPException(403, "Cảnh này chưa được xử lý. Hãy nhờ cán bộ xử lý giúp.")
            hydro.throttle("render", u["id"], 20, 3600)
            if sum(1 for j in render.JOBS.values() if j.get("status") == "running") >= MAX_JOBS:
                raise HTTPException(429, f"Máy chủ đang xử lý tối đa {MAX_JOBS} cảnh, hãy thử lại sau ít phút")
        return render.start(id, url, layer, bool(retry))


@app.get("/api/value", dependencies=[Depends(hydro.full)])
def api_value(id: str, layer: str, lng: float, lat: float):
    if layer not in render.LAYERS or not ID_RE.fullmatch(id):
        raise HTTPException(400, "id/layer không hợp lệ")
    return {"value": render.value(f"{id}__{layer}", lng, lat)}


@app.get("/tiles/{name}")
def tiles(name: str):
    """Chỉ phục vụ ảnh PNG đã xử lý (dữ liệu vệ tinh công khai). Trước đây cả thư mục cache, gồm file .h5 tải tay, bị mở công khai."""
    f = render.CACHE / name
    if not TILE_RE.fullmatch(name) or not f.is_file():
        raise HTTPException(404, "Không có ảnh")
    return FileResponse(f, media_type="image/png", headers={"Cache-Control": "no-cache"})


STATIC = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def landing():
    return FileResponse(STATIC / "landing.html", headers={"Cache-Control": "no-cache"})


@app.get("/app")
def index():
    return FileResponse(STATIC / "app.html", headers={"Cache-Control": "no-cache"})


@app.get("/map")
def map_page():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
