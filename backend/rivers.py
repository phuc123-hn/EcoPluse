"""Mạch nước (sông, suối, kênh) cho nút "Hiện mạch nước" trên bản đồ.
Dữ liệu: static/rivers.geojson. Tạo lại khi có shapefile mới:  python scripts/build_rivers.py data/waterway-river.shp
"""
import gzip
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

FILE = Path(__file__).resolve().parent.parent / "static" / "rivers.geojson"
router = APIRouter()
_mem = {}


@router.get("/api/rivers")
def rivers(req: Request):
    if not FILE.exists():
        raise HTTPException(404, "Chưa có static/rivers.geojson. Chạy: python scripts/build_rivers.py data/waterway-river.shp")
    m = FILE.stat().st_mtime
    if _mem.get("m") != m:                      # nạp lại khi file đổi
        raw = FILE.read_bytes()
        _mem.update(m=m, raw=raw, gz=gzip.compress(raw, 6))
    h = {"Cache-Control": "public, max-age=3600", "Vary": "Accept-Encoding"}
    if "gzip" in req.headers.get("accept-encoding", ""):
        return Response(_mem["gz"], media_type="application/json", headers={**h, "Content-Encoding": "gzip"})
    return Response(_mem["raw"], media_type="application/json", headers=h)
