"""NISAR live map – backend.
Chạy:  python -m uvicorn backend.main:app --reload --port 8000
"""
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from . import assistant, boundaries, hydro, render, rivers, security, suitability, swi, weather
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

ASF_URL = "https://api.daac.asf.alaska.edu/services/search/param"
# Tây Nguyên + Bình Phước (W, S, E, N) – chỉnh tại đây nếu cần
AOI = (106.0, 11.0, 109.0, 15.6)
LEVELS = {"SME2", "GCOV", "GSLC", "RSLC"}
ALLOWED_HOST = "https://nisar.asf.earthdatacloud.nasa.gov/"
CACHE_TTL = 15 * 60  # giây
_cache: dict = {}

class SmartGZip(GZipMiddleware):
    """Nén gzip cho HTML/JS/JSON (app.html 115 KB còn khoảng 25 KB, rất đáng kể trên mạng di động vùng sâu).
    Bỏ qua ảnh PNG: đã nén sẵn, nén lại chỉ tốn CPU của máy chủ miễn phí."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(("/tiles/", "/static/img/")):
            return await self.app(scope, receive, send)
        await super().__call__(scope, receive, send)


# PRODUCTION=1 thì tắt trang tài liệu API công khai (/docs, /redoc, /openapi.json)
app = FastAPI(title="EcoPulse", **(dict(docs_url=None, redoc_url=None, openapi_url=None) if os.getenv("PRODUCTION") else {}))
app.add_middleware(SmartGZip, minimum_size=1024)
app.include_router(hydro.router)
app.include_router(boundaries.router)
app.include_router(rivers.router)
app.include_router(weather.router)
app.include_router(assistant.router)
app.include_router(swi.router)
app.include_router(suitability.router)
security.install(app)


def _wkt(b):
    w, s, e, n = b
    return f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"


@app.get("/api/config")
def config():
    return {"maptilerKey": os.getenv("NEXT_PUBLIC_MAPTILER_KEY", ""), "aoi": AOI}


@app.get("/api/scenes", dependencies=[Depends(hydro.user)])
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
        raise HTTPException(502, f"Không gọi được ASF: {e}")

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


@app.get("/api/render", dependencies=[Depends(hydro.user)])
def api_render(id: str, url: str, layer: str, retry: int = 0):
    if layer not in render.LAYERS:
        raise HTTPException(400, "layer không hợp lệ")
    if not url.startswith(ALLOWED_HOST) or not id.replace("_", "").isalnum():
        raise HTTPException(400, "url/id không hợp lệ")
    return render.start(id, url, layer, bool(retry))


@app.get("/api/value", dependencies=[Depends(hydro.user)])
def api_value(id: str, layer: str, lng: float, lat: float):
    if layer not in render.LAYERS or not id.replace("_", "").isalnum():      # chặn ký tự lạ (../) đi vào đường dẫn file cache
        raise HTTPException(400, "id/layer không hợp lệ")
    return {"value": render.value(f"{id}__{layer}", lng, lat)}


app.mount("/tiles", StaticFiles(directory=render.CACHE), name="tiles")
STATIC = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def landing():
    return FileResponse(STATIC / "landing.html", headers={"Cache-Control": "no-cache"})


@app.get("/app")
def index():
    return FileResponse(STATIC / "app.html", headers={"Cache-Control": "no-cache"})


@app.get("/dat")
def land_page():
    return FileResponse(STATIC / "dat.html", headers={"Cache-Control": "no-cache"})


@app.get("/map")
def map_page():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
