"""Nhập một lớp dữ liệu GeoTIFF vào lưới chung của bản đồ thích nghi đất đai.

Chạy trên máy bạn (cần:  python -m pip install rasterio numpy).  Ví dụ:
  python scripts/import_layer.py dem/*.tif do_cao --resampling average
  python scripts/import_layer.py dem/*.tif do_doc --slope --resampling average
  python scripts/import_layer.py phh2o_0-5cm.tif ph --scale 0.1
  python scripts/import_layer.py soc_0-5cm.tif carbon --scale 0.1          (dg/kg -> g/kg)
  python scripts/import_layer.py prec_annual.tif mua --resampling bilinear
  python scripts/import_layer.py soil_class.tif loai_dat --resampling nearest   (lớp dạng nhóm: mã số nguyên)

Kết quả: data/layers/<id>.npy (float32, NaN = không có dữ liệu), lưới 3067 x 2000 ô, mỗi ô 0.0015 độ (~165 m), khung 106.0-109.0 E, 11.0-15.6 N.
Mã lớp phải trùng với "layer" trong backend/suitability_config.json (do_cao, do_doc, ph, carbon, do_day, mua, kho_han, do_am_kk, tuoi, ngap, loai_dat, co_gioi).
"""
import argparse
import os
from pathlib import Path

import numpy as np

AOI, RES, W, H = (106.0, 11.0, 109.0, 15.6), 0.0015, 2000, 3067


def slope_deg(z, dx_deg, dy_deg, lat):
    """Độ dốc (độ) từ DEM: dx, dy là kích thước ô (độ); lat là vĩ độ từng hàng (độ) để đổi ra mét."""
    dx, dy = dx_deg * 111320 * np.cos(np.radians(lat)), dy_deg * 110540
    gy, gx = np.gradient(z.astype(np.float64))                   # độ chênh cao theo từng ô, chia cho khoảng cách thật bên dưới
    return np.degrees(np.arctan(np.hypot(gx / dx, gy / dy))).astype(np.float32)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="một hoặc nhiều file .tif (nhiều ô DEM sẽ được ghép)")
    ap.add_argument("layer", help="mã lớp, ví dụ do_cao")
    ap.add_argument("--resampling", default="average", choices=["average", "bilinear", "nearest", "mode"], help="average cho số liệu liên tục; nearest/mode cho lớp nhóm")
    ap.add_argument("--scale", type=float, default=1.0, help="nhân giá trị gốc (ví dụ pH của SoilGrids x0.1)")
    ap.add_argument("--offset", type=float, default=0.0)
    ap.add_argument("--band", type=int, default=1)
    ap.add_argument("--nodata", type=float, default=None, help="giá trị không dữ liệu nếu file không ghi sẵn")
    ap.add_argument("--slope", action="store_true", help="tính độ dốc (độ) từ DEM rồi mới ghép vào lưới")
    ap.add_argument("--out", default=None, help="thư mục xuất (mặc định: cùng thư mục với HN_DB, thư mục con layers)")
    a = ap.parse_args()
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import Resampling, reproject

    out_dir = Path(a.out) if a.out else (Path(os.getenv("HN_DB", "data/hydronisar.db")).parent / "layers")
    out_dir.mkdir(parents=True, exist_ok=True)
    dst_tf, res = from_origin(AOI[0], AOI[3], RES, RES), getattr(Resampling, a.resampling)
    out = np.full((H, W), np.nan, np.float32)
    for f in a.inputs:
        with rasterio.open(f) as src:
            nd = a.nodata if a.nodata is not None else src.nodata
            arr = src.read(a.band).astype(np.float32)
            if nd is not None:
                arr[arr == nd] = np.nan
            if a.slope:
                lat = src.bounds.top - (np.arange(arr.shape[0]) + .5) * abs(src.transform.e)
                arr = slope_deg(np.nan_to_num(arr, nan=0.0), abs(src.transform.a), abs(src.transform.e), lat[:, None]) * np.where(np.isfinite(arr), 1, np.nan).astype(np.float32)
            tmp = np.full((H, W), np.nan, np.float32)
            reproject(arr, tmp, src_transform=src.transform, src_crs=src.crs, dst_transform=dst_tf, dst_crs="EPSG:4326", resampling=res, src_nodata=np.nan, dst_nodata=np.nan)
            out = np.where(np.isnan(out), tmp, out)
            print(f"  đã ghép {f}")
    out = out * a.scale + a.offset
    np.save(out_dir / f"{a.layer}.npy", out.astype(np.float32))
    ok = np.isfinite(out)
    print(f"Xong: {out_dir / (a.layer + '.npy')}\n  phủ {ok.mean() * 100:.1f}% lưới" + (f", nhỏ nhất {np.nanmin(out):.3g}, lớn nhất {np.nanmax(out):.3g}, trung bình {np.nanmean(out):.3g}" if ok.any() else "  (KHÔNG có điểm nào hợp lệ: kiểm tra vùng phủ và --nodata)"))


if __name__ == "__main__":
    main()
