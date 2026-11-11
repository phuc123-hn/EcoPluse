"""Tải 1 cảnh NISAR -> tính chỉ số -> xuất ảnh PNG phủ bản đồ (WGS84)."""
import gc, json, os, re, threading, time
from pathlib import Path

import h5py
import numpy as np
import requests
from PIL import Image
from pyproj import Transformer

CACHE = Path(os.getenv("CACHE_DIR") or Path(__file__).resolve().parent.parent / "cache")   # trên Railway: CACHE_DIR=/data/cache (ổ đĩa bền)
CACHE.mkdir(exist_ok=True)
AOI = (106.0, 11.0, 109.0, 15.6)
RES = 0.0015  # độ/pixel (~165 m)

VERSION = 3  # tăng số này khi đổi cách tô màu/phân loại để cảnh cũ trong cache được xử lý lại

# Phân loại độ ẩm đất theo ngưỡng của nhóm (m³/m³). MỌI ngưỡng, màu và lời khuyên nằm ở đây, sửa 1 chỗ.
SOIL_CLS = [
    dict(max=0.10, name="Rất khô", color="#e8261a", action="Cần tưới ngay lập tức",
         state="Đất mặt rất khô, cây dễ thiếu nước, héo lá.",
         todo="Tưới ngay, ưu tiên vườn đang ra hoa, đậu quả. Tưới sáng sớm hoặc chiều mát, tưới đủ thấm sâu vùng rễ thay vì tưới ít nhiều lần.",
         tip="Phủ gốc bằng rơm, cỏ khô để giữ ẩm lâu hơn."),
    dict(max=0.20, name="Khô vừa", color="#f4e43a", action="Bắt đầu bật hệ thống tưới",
         state="Đất bắt đầu thiếu nước, chưa nguy cấp.",
         todo="Chuẩn bị và bật hệ thống tưới, tưới nhẹ theo chu kỳ. Kiểm tra lại sau 1-2 ngày, xem thêm dự báo mưa.",
         tip="Nếu sắp có mưa to trong 1-2 ngày tới thì có thể hoãn tưới."),
    dict(max=0.35, name="Lý tưởng", color="#6fe3ec", action="Tắt tưới / Duy trì",
         state="Độ ẩm phù hợp với đa số cây trồng.",
         todo="Không cần tưới thêm. Giữ nguyên chế độ chăm sóc, kiểm tra lại sau 2-3 ngày hoặc sau đợt nắng nóng.",
         tip="Đây là mức nên giữ ổn định, tránh tưới thừa gây lãng phí nước và điện."),
    dict(max=0.40, name="Ẩm ướt", color="#4575b4", action="Tắt tưới",
         state="Đất khá ẩm, đủ nước.",
         todo="Tắt tưới. Theo dõi dự báo mưa và kiểm tra rãnh thoát nước nếu sắp có mưa lớn.",
         tip="Chưa nguy hiểm nhưng không nên tưới thêm."),
    dict(max=None, name="Quá ẩm / Thừa nước", color="#1c2c6b", action="Cảnh báo ngập úng",
         state="Đất rất ẩm hoặc bão hòa nước, có nguy cơ úng rễ.",
         todo="Ngừng tưới hoàn toàn, khơi thông rãnh thoát nước, hạn chế bón phân và phun thuốc lúc này. Kiểm tra gốc những cây dễ úng như tiêu, sầu riêng, cà phê.",
         tip="Ruộng lúa hoặc vùng trũng có thể ẩm cao là bình thường, hãy đối chiếu với loại cây trồng."),
]
VEG = [(0, "#d9c9a3"), (0.3, "#a8c66c"), (0.6, "#4f9d4f"), (1, "#0b5d2a")]
HVDB = [(-25, "#f2f0e6"), (-18, "#b5d6a7"), (-12, "#4f9d4f"), (-6, "#0b3d1e")]

LAYERS = {
    "soil": dict(level="SME2", title="Độ ẩm đất", cls=SOIL_CLS, scale=100, unit="%", digits=0, unc=0.06,
                 note="Giá trị tính theo m³/m³, phần trăm cuối dòng là tỷ lệ diện tích mỗi mức trong cảnh. Đây là độ ẩm lớp đất mặt (vài cm), độ phân giải 200 m, sai số mục tiêu của NISAR khoảng ±0.06 m³/m³. Dữ liệu còn đang hiệu chuẩn nên dùng để tham khảo, hãy đối chiếu thực tế."),
    "veg": dict(level="GCOV", title="Thảm thực vật (RVI)", stops=VEG, scale=1, unit="", digits=2,
                classes=[[0.25, "Đất trống / cây thưa"], [0.55, "Cây phát triển vừa"], [9, "Tán cây dày"]],
                note="Chỉ số thực vật radar: càng xanh đậm thì tán cây càng dày. Quan sát được cả ngày nhiều mây."),
    "hv": dict(level="GCOV", title="Cường độ HV (dB)", stops=HVDB, scale=1, unit=" dB", digits=1,
               classes=[[-18, "Ít sinh khối"], [-12, "Sinh khối vừa"], [99, "Sinh khối cao"]],
               note="Tín hiệu phân cực chéo, tăng theo lượng thân lá cây."),
}


def _find(f, root, name):
    hit = []
    f[root].visititems(lambda n, o: hit.append(o) if n.split("/")[-1] == name and isinstance(o, h5py.Dataset) else None)
    return hit[0] if hit else None


def _read(f, layer):
    root = "/science/LSAR/SME2/grids" if layer == "soil" else "/science/LSAR/GCOV/grids/frequencyA"
    xd, yd = _find(f, root, "xCoordinates"), _find(f, root, "yCoordinates")
    if xd is None or yd is None:
        raise KeyError(f"Không thấy xCoordinates/yCoordinates dưới {root}")
    x, y = xd[:], yd[:]
    epsg = 6933
    p = _find(f, root, "projection")
    if p is not None:
        try:
            epsg = int(p.attrs.get("epsg_code", p[()]))
        except Exception:
            pass
    step = 1 if layer == "soil" else max(1, int(round(100 / abs(x[1] - x[0]))))
    xs, ys = x[::step], y[::step]
    g = lambda n: f[f"{root}/{n}"][::step, ::step].astype("float32")
    if layer == "soil":
        a = g("soilMoisture")
        a[(a < 0) | (a > 1)] = np.nan
    else:
        hh, hv = g("HHHH"), g("HVHV")
        bad = ~((hh > 0) & (hv > 0))
        a = 4 * hv / (hh + hv) if layer == "veg" else 10 * np.log10(hv)
        a[bad] = np.nan
    return a, xs, ys, epsg


def _to_lonlat(a, xs, ys, epsg):
    inv = Transformer.from_crs(epsg, 4326, always_xy=True)
    X, Y = np.meshgrid([xs[0], xs[-1]], [ys[0], ys[-1]])
    lo, la = inv.transform(X.ravel(), Y.ravel())
    w, e = max(AOI[0], min(lo)), min(AOI[2], max(lo))
    s, n = max(AOI[1], min(la)), min(AOI[3], max(la))
    if w >= e or s >= n:
        raise ValueError("Cảnh này không giao với vùng Tây Nguyên / Bình Phước")
    W, H = int((e - w) / RES), int((n - s) / RES)
    LON, LAT = np.meshgrid(w + (np.arange(W) + .5) * RES, n - (np.arange(H) + .5) * RES)
    X, Y = Transformer.from_crs(4326, epsg, always_xy=True).transform(LON, LAT)
    c = np.rint((X - xs[0]) / (xs[1] - xs[0])).astype(int)
    r = np.rint((Y - ys[0]) / (ys[1] - ys[0])).astype(int)
    ok = (c >= 0) & (c < len(xs)) & (r >= 0) & (r < len(ys))
    out = np.full((H, W), np.nan, np.float32)
    out[ok] = a[r[ok], c[ok]]
    return out, (w, s, e, n)


def _colorize(a, stops):
    v = [s[0] for s in stops]
    rgb = [[int(c[i:i + 2], 16) for i in (1, 3, 5)] for _, c in stops]
    img = np.zeros(a.shape + (4,), np.uint8)
    z = np.nan_to_num(a)
    for k in range(3):
        img[..., k] = np.interp(z, v, [c[k] for c in rgb])
    img[..., 3] = np.where(np.isfinite(a), 205, 0)
    return img


def _cls_idx(a, cls):
    return np.digitize(np.nan_to_num(a), [c["max"] for c in cls[:-1]])


def process(h5path, key, layer):
    with h5py.File(h5path, "r") as f:
        a, xs, ys, epsg = _read(f, layer)
    out, b = _to_lonlat(a, xs, ys, epsg)
    if not np.isfinite(out).any():
        raise ValueError("Không có điểm ảnh hợp lệ trong vùng (có thể toàn bộ là null / bị che).")
    np.save(CACHE / f"{key}.npy", out)
    L = LAYERS[layer]
    hist = None
    if "cls" in L:
        idx, ok = _cls_idx(out, L["cls"]), np.isfinite(out)
        img = np.zeros(out.shape + (4,), np.uint8)
        for i, c in enumerate(L["cls"]):
            img[idx == i, :3] = [int(c["color"][j:j + 2], 16) for j in (1, 3, 5)]
        img[..., 3] = np.where(ok, 215, 0)
        hist = [float(np.mean(idx[ok] == i)) for i in range(len(L["cls"]))]
    else:
        img = _colorize(out, L["stops"])
    Image.fromarray(img, "RGBA").save(CACHE / f"{key}.png")
    meta = {"status": "done", "bounds": b, "res": RES, "layer": layer, "mean": float(np.nanmean(out)),
            "png": f"/tiles/{key}.png", "hist": hist, "v": VERSION,
            "legend": {k: v for k, v in L.items() if k != "level"}}
    (CACHE / f"{key}.json").write_text(json.dumps(meta))
    return meta


def _dump(h5path):
    try:
        names = []
        with h5py.File(h5path, "r") as f:
            f.visititems(lambda n, o: names.append(n) if isinstance(o, h5py.Dataset) and "metadata" not in n else None)
        return " | ".join(names[:40])
    except Exception:
        return ""


JOBS: dict = {}
(CACHE / "manual").mkdir(exist_ok=True)
DEBUG = bool(os.getenv("RENDER_DEBUG"))
SAFE_KEY = re.compile(r"^[A-Za-z0-9_\-]{1,250}$")      # chỉ cho tên file an toàn, chặn ../ và ký tự lạ


def cached_meta(key):
    """Trả meta nếu cảnh đã xử lý xong bằng đúng phiên bản hiện tại, ngược lại None."""
    if not SAFE_KEY.fullmatch(key or ""):
        return None
    j = CACHE / f"{key}.json"
    if j.exists():
        try:
            m = json.loads(j.read_text())
        except Exception:
            return None
        if m.get("v") == VERSION:
            return m
    return None


def _rm(path, tries=10):
    """Xóa file; trên Windows file có thể bị khóa tạm thời (Defender/OneDrive quét) nên thử lại."""
    for _ in range(tries):
        try:
            path.unlink(missing_ok=True)
            return True
        except PermissionError:
            gc.collect()          # nhả mọi handle h5py còn sót
            time.sleep(1)
    return False                  # không xóa được thì bỏ qua, dọn lần khởi động sau


for _f in CACHE.glob("*.part"):    # dọn file dở còn sót từ lần chạy trước
    try:
        _f.unlink()
    except OSError:
        pass


class _Session(requests.Session):
    """Giữ token khi chuyển hướng trong miền nasa.gov, bỏ token khi sang S3."""
    def rebuild_auth(self, prepared, response):
        host = (requests.utils.urlparse(prepared.url).hostname or "").lower()
        if not (host == "nasa.gov" or host.endswith(".nasa.gov")):      # không dùng endswith("nasa.gov") trần: "evilnasa.gov" cũng khớp
            prepared.headers.pop("Authorization", None)


def _download(url, dest, token, key):
    s = _Session()
    s.headers["Authorization"] = f"Bearer {token}"
    r = s.get(url, stream=True, timeout=(15, 60))
    try:
        _save(r, dest, key)
    finally:
        r.close()


def _save(r, dest, key):
    if r.status_code in (401, 403):
        raise RuntimeError(f"ASF từ chối ({r.status_code}). Kiểm tra token còn hạn và đã cho phép ứng dụng ASF trong Earthdata (Applications > Authorized Apps).")
    r.raise_for_status()
    if "text/html" in r.headers.get("content-type", ""):
        raise RuntimeError("ASF trả về trang đăng nhập: token không được chấp nhận. Tạo lại token trên urs.earthdata.nasa.gov.")
    total = int(r.headers.get("content-length", 0))
    done = 0
    JOBS[key] = {"status": "running", "phase": "download", "done": 0, "total": total}
    with open(dest, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
            done += len(chunk)
            JOBS[key]["done"] = done
    if not h5py.is_hdf5(dest):
        raise RuntimeError("File tải về không phải HDF5 (có thể bị cắt giữa chừng). Thử lại.")


def start(fid, url, layer, retry=False):
    key = f"{fid}__{layer}"
    if not SAFE_KEY.fullmatch(key) or layer not in LAYERS:
        raise ValueError("id/layer không hợp lệ")
    m = cached_meta(key)
    if m:
        return m
    cur = JOBS.get(key, {})
    if cur.get("status") == "running":
        return cur
    if cur.get("status") == "error" and not retry:
        return cur  # trả lỗi về cho người dùng, không tự chạy lại
    JOBS[key] = {"status": "running", "phase": "connect"}
    threading.Thread(target=_run, args=(key, fid, url, layer), daemon=True).start()
    return JOBS[key]


def _run(key, fid, url, layer):
    manual = CACHE / "manual" / f"{fid}.h5"   # file tự tải tay: bỏ vào đây là dùng luôn
    tmp = CACHE / f"{fid}.h5.part"
    src = manual
    try:
        if not manual.exists():
            token = os.getenv("EARTHDATA_TOKEN", "").strip()
            if not token:
                raise RuntimeError("Chưa điền EARTHDATA_TOKEN trong file .env (hoặc bỏ file .h5 vào cache/manual/)")
            _download(url, tmp, token, key)
            src = tmp
        JOBS[key] = {"status": "running", "phase": "process"}
        JOBS[key] = process(src, key, layer)
    except Exception as e:
        print(f"[EcoPulse] Lỗi xử lý cảnh {key}: {type(e).__name__}: {e}", flush=True)       # chi tiết kỹ thuật chỉ ghi ở máy chủ
        friendly = isinstance(e, (RuntimeError, ValueError, KeyError))                        # các lỗi do chính code này đặt thông điệp tiếng Việt
        JOBS[key] = {"status": "error", "detail": str(e).strip("'\"") if friendly else "Không xử lý được cảnh này. Hãy báo quản trị viên.",
                     "layers": _dump(src) if (DEBUG and src.exists()) else ""}
    finally:
        _rm(tmp)


def value(key, lng, lat):
    if not SAFE_KEY.fullmatch(key or ""):
        return None
    j = CACHE / f"{key}.json"
    if not j.exists():
        return None
    m = json.loads(j.read_text())
    w, s, e, n = m["bounds"]
    arr = np.load(CACHE / f"{key}.npy", mmap_mode="r")
    c, r = int((lng - w) / RES), int((n - lat) / RES)
    if not (0 <= r < arr.shape[0] and 0 <= c < arr.shape[1]) or not np.isfinite(arr[r, c]):
        return None
    return float(arr[r, c])


def probe(key, bbox, pt=None):
    """Tìm điểm có dữ liệu đại diện trong khung bbox (W,S,E,N) và tổng hợp thống kê."""
    if not SAFE_KEY.fullmatch(key or ""):
        return {"found": False}
    j = CACHE / f"{key}.json"
    if not j.exists():
        return {"found": False}
    m = json.loads(j.read_text())
    bw, bs, be, bn = m["bounds"]
    arr = np.load(CACHE / f"{key}.npy", mmap_mode="r")
    w, s_, e, n = bbox
    c0, c1 = max(0, int((w - bw) / RES)), min(arr.shape[1], int(np.ceil((e - bw) / RES)))
    r0, r1 = max(0, int((bn - n) / RES)), min(arr.shape[0], int(np.ceil((bn - s_) / RES)))
    if c1 <= c0 or r1 <= r0:
        return {"found": False}
    sub = np.asarray(arr[r0:r1, c0:c1])
    ok = np.isfinite(sub)
    if not ok.any():
        return {"found": False}
    ys, xs = np.nonzero(ok)
    if pt:  # điểm người dùng chọn: lấy ô có dữ liệu gần nhất
        ty, tx = (bn - pt[1]) / RES - r0, (pt[0] - bw) / RES - c0
    else:   # khu vực: lấy ô có dữ liệu gần tâm của các ô có dữ liệu
        ty, tx = ys.mean(), xs.mean()
    k = int(np.argmin((ys - ty) ** 2 + (xs - tx) ** 2))
    r, c = r0 + ys[k], c0 + xs[k]
    expected = max(1.0, (e - w) / RES * (n - s_) / RES)
    vals = sub[ok]
    L = LAYERS[m["layer"]]
    hist = [float(np.mean(_cls_idx(vals, L["cls"]) == i)) for i in range(len(L["cls"]))] if "cls" in L else None
    return {"found": True, "lng": float(bw + (c + .5) * RES), "lat": float(bn - (r + .5) * RES),
            "value": float(arr[r, c]), "mean": float(vals.mean()), "hist": hist,
            "cover": float(min(1.0, ok.sum() / expected))}
