"""Cổng duy nhất để MÁY CHỦ gọi Open-Meteo (thời tiết lưới, hải lưu, thời tiết tại thửa).

Vì sao cần file này
  Trên Render gói miễn phí, nhiều app dùng chung địa chỉ IP đi ra ngoài, mà Open-Meteo tính hạn mức theo IP
  (600 lượt/phút, 10.000 lượt/ngày). Nên có lúc bị 429 dù app mình gọi rất ít, và trước đây lỗi đó biến thành 502.

Ba nguyên tắc
  1. Yêu cầu của người dùng KHÔNG BAO GIỜ đứng chờ Open-Meteo. Có bản đã lưu (kể cả cũ) thì trả ngay;
     việc làm mới chạy ở một luồng nền, mỗi khóa tối đa một luồng.
  2. Bản đã lưu được ghi vào database (bảng kv_cache). Dùng Postgres (DATABASE_URL) thì khi Render khởi động lại,
     dữ liệu vẫn còn để hiện ngay. Dùng SQLite trên ổ đĩa tạm của Render thì sẽ mất sau mỗi lần khởi động lại.
  3. Mọi lượt gọi đều qua pace(): giữ dưới hạn mức phút và ngày; bị 429 thì nghỉ rồi mới thử lại.
"""
import json
import threading
import time

PER_MIN = 400                  # tự giữ dưới hạn mức 600 lượt/phút của Open-Meteo
DAILY_CAP = 8000               # ngân sách nội bộ mỗi ngày (UTC), chừa chỗ dưới hạn mức 10.000
FAIL_COOLDOWN = 120            # sau lỗi thường: không thử lại ngay (giây)
LIMIT_COOLDOWN = 30 * 60       # sau khi hết hạn mức giờ/ngày: nghỉ 30 phút

_lk = threading.Lock()
_win = []                      # [(thời điểm, số lượt)] trong 60 giây gần nhất
_used = {"day": "", "n": 0}    # số lượt MÁY CHỦ NÀY đã gọi trong ngày (UTC)
_mem = {}                      # khóa -> {"t", "data", "fail", "wait", "run", "err"}


class RateLimited(Exception):
    """Bị giới hạn lượt gọi (429 từ Open-Meteo hoặc hết ngân sách nội bộ). `wait` = số giây nên nghỉ trước khi thử lại."""

    def __init__(self, msg, wait=FAIL_COOLDOWN):
        super().__init__(msg)
        self.wait = wait


class Unavailable(Exception):
    """Chưa có bản dữ liệu nào (chưa từng tải được) và nguồn đang lỗi hoặc đang tải."""


def pace(cost, max_wait=None):
    """Ghi nhận `cost` lượt. Nếu 60 giây gần nhất sắp chạm hạn mức phút thì chờ.
    max_wait: số giây tối đa chịu chờ (None = chờ thoải mái, dành cho luồng nền)."""
    waited = 0.0
    while True:
        with _lk:
            today = time.strftime("%Y-%m-%d", time.gmtime())
            if _used["day"] != today:
                _used.update(day=today, n=0)
            if _used["n"] + cost > DAILY_CAP:
                raise RateLimited(f"máy chủ đã gọi {_used['n']}/{DAILY_CAP} lượt hôm nay (UTC), tạm dừng để không vượt hạn mức", LIMIT_COOLDOWN)
            now = time.time()
            _win[:] = [(t, c) for t, c in _win if now - t < 60]
            if not _win or sum(c for _, c in _win) + cost <= PER_MIN:
                _win.append((now, cost))
                _used["n"] += cost
                return
            need = max(0.5, 60 - (now - _win[0][0]))
        if max_wait is not None and waited + need > max_wait:
            raise RateLimited("sắp chạm hạn mức phút", 60)
        time.sleep(need)
        waited += need


def get(cl, url, params, cost=1, max_wait=None):
    """GET + JSON. 429 theo phút: chờ rồi thử lại (tối đa 3 lần). 429 theo giờ/ngày: dừng ngay vì chờ vài giây vô ích.
    Với max_wait (luồng có người đang chờ) thì không ngủ chờ gì cả: báo lỗi ngay để dùng bản cũ."""
    low = ""
    for k in range(3):
        pace(cost, max_wait)
        r = cl.get(url, params=params)
        if r.status_code != 429:
            r.raise_for_status()
            return r.json()
        try:
            low = str(r.json().get("reason", "")).lower()
        except Exception:
            low = ""
        if (low and "minute" not in low) or max_wait is not None:
            break
        if k < 2:
            time.sleep(20 * (k + 1))
    if "daily" in low:
        raise RateLimited("Open-Meteo báo hết hạn mức NGÀY (tính theo IP máy chủ; Render dùng chung IP nên có thể do app khác)", LIMIT_COOLDOWN)
    if "hourly" in low:
        raise RateLimited("Open-Meteo báo hết hạn mức GIỜ", LIMIT_COOLDOWN)
    raise RateLimited(low or "429 Too Many Requests")


# ---------------------------------------------------------------- bản lưu bền trong database
_table = {"ok": False}


def _q():
    from . import hydro            # nạp muộn: hydro cũng import file này, nạp ngay đầu file sẽ bị import vòng
    if not _table["ok"]:
        hydro.q("CREATE TABLE IF NOT EXISTS kv_cache(k TEXT PRIMARY KEY, t REAL NOT NULL, v TEXT NOT NULL)")
        _table["ok"] = True
    return hydro.q


def save(key, t, data):
    try:
        s = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(s) > 3_000_000:
            return
        _q()("INSERT INTO kv_cache(k,t,v) VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET t=excluded.t, v=excluded.v", (key, t, s))
    except Exception:
        pass


def load(key):
    try:
        r = _q()("SELECT t, v FROM kv_cache WHERE k=?", (key,), one=True)
        return (float(r["t"]), json.loads(r["v"])) if r else None
    except Exception:
        return None


# ---------------------------------------------------------------- stale-while-revalidate
def _entry(key):
    with _lk:
        e = _mem.get(key)
        first = e is None
        if first:
            e = _mem[key] = {"t": 0.0, "data": None, "fail": 0.0, "wait": 0, "run": None, "err": ""}
    if first:
        snap = load(key)
        if snap and e["data"] is None:
            e["t"], e["data"] = snap
    return e


def _work(key, e, fetch):
    try:
        data = fetch()
        now = time.time()
        e.update(data=data, t=now, fail=0.0, wait=0, err="")
        save(key, now, data)
    except Exception as ex:
        e.update(fail=time.time(), wait=getattr(ex, "wait", FAIL_COOLDOWN), err=str(ex)[:300])


def swr(key, ttl, fetch, *, force=False, min_gap=0, wait_first=0):
    """Trả (dữ liệu, cũ_hay_không) NGAY LẬP TỨC nếu đã có bản nào đó.
    Quá hạn `ttl` thì bật một luồng nền gọi fetch() (một luồng mỗi khóa; đang nghỉ sau lỗi thì chưa gọi lại).
    force=True là người dùng bấm làm mới tay: chỉ có hiệu lực nếu bản hiện có đã quá `min_gap` giây.
    wait_first: lần ĐẦU TIÊN (chưa có gì để hiện) chờ tối đa chừng ấy giây cho luồng nền; hết giờ thì ném Unavailable."""
    e = _entry(key)
    now = time.time()
    age = now - e["t"]
    have = e["data"] is not None
    th = None
    if (not have) or age >= ttl or (force and age >= min_gap):
        with _lk:
            run = e["run"]
            if run is not None and run.is_alive():
                th = run
            elif now - e["fail"] >= e["wait"]:
                th = e["run"] = threading.Thread(target=_work, args=(key, e, fetch), daemon=True)
                th.start()
    if not have and th is not None and wait_first:
        th.join(wait_first)
    if e["data"] is None:
        left = max(0, int(e["wait"] - (time.time() - e["fail"]))) if e["fail"] else 0
        msg = e["err"] or "đang tải dữ liệu lần đầu"
        if left:
            msg += f" (thử lại sau khoảng {left // 60 + 1} phút)"
        raise Unavailable(msg)
    return e["data"], (time.time() - e["t"]) >= ttl


def status(key):
    """Tình trạng một khóa, dùng cho /api/weather/usage."""
    e = _mem.get(key)
    now = time.time()
    if not e:
        return {"has_data": False, "age_min": None, "retry_in_min": 0, "last_error": ""}
    return {"has_data": e["data"] is not None, "age_min": round((now - e["t"]) / 60) if e["data"] is not None else None,
            "retry_in_min": max(0, -(-int(e["wait"] - (now - e["fail"])) // 60)) if e["fail"] else 0, "last_error": e["err"]}


def usage():
    return {"day_utc": _used["day"], "used_today": _used["n"], "cap": DAILY_CAP}
