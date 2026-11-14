"""EcoPulse: tài khoản, phân quyền (nông dân / quản lý xã-phường / quản lý tỉnh), thửa, nhắn tin, thông báo.
Gắn vào backend.main.

Vai trò:
  farmer          nông dân (đăng nhập bằng tên + mật khẩu, có tỉnh và xã/phường)
  admin_ward      quản lý xã/phường (đăng nhập bằng email, đăng ký bằng mã OTP gửi qua email)
  admin_province  quản lý tỉnh (tạo bằng tools/make_admin.py). Nếu tỉnh để trống thì thấy toàn bộ các tỉnh.
"""
import hashlib, hmac, json, os, re, secrets, smtplib, sqlite3, threading, time, unicodedata
from collections import defaultdict, deque
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from fastapi import BackgroundTasks

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import render

load_dotenv()   # phải nạp .env TRƯỚC khi đọc HN_DB, HN_ADMIN_*, SMTP_* bên dưới

DB = Path(os.getenv("HN_DB") or Path(__file__).resolve().parent.parent / "data" / "hydronisar.db")   # giữ tên file cũ để không mất dữ liệu
DB.parent.mkdir(parents=True, exist_ok=True)
SESSION_DAYS, MAX_FAILS, LOCK_SEC = 14, 10, 15 * 60     # MAX_FAILS: số lần sai mật khẩu trước khi khóa tài khoản 15 phút (đã có thêm giới hạn theo IP nên nâng lên 10 để người lạ khó khóa tài khoản người khác)
LIMITED_DAYS = int(os.getenv("LIMITED_DAYS") or 7)      # phiên "chế độ hạn chế" (vào bằng Quên mật khẩu) sống bao lâu
# Tự nhận biết môi trường chạy thật (Railway) để bật cookie Secure và tắt /docs ngay cả khi quên đặt biến môi trường
IS_PROD = bool(os.getenv("PRODUCTION") or os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_ENVIRONMENT_NAME") or os.getenv("RAILWAY_PROJECT_ID"))
_cs = (os.getenv("COOKIE_SECURE") or "").strip().lower()
COOKIE_SECURE = (_cs not in ("0", "false", "no")) if _cs else IS_PROD
TRUSTED_PROXIES = int(os.getenv("TRUSTED_PROXIES") or (1 if IS_PROD else 0))   # số proxy CỦA BẠN đứng trước app (Railway = 1; Vercel rewrite + Railway = 2)
ALERT_BANDS = (0, 1, 4)          # Rất khô, Khô vừa, Quá ẩm -> tạo thông báo
ONLINE_SEC = 5 * 60              # thấy hoạt động trong 5 phút gần nhất thì coi là "đang hoạt động"
OTP_TTL, OTP_TRIES, OTP_GAP = 10 * 60, 5, 60
PROVINCES = ["Quảng Ngãi", "Gia Lai", "Đắk Lắk", "Lâm Đồng", "Đồng Nai"]   # gợi ý khi đăng ký (tỉnh sau sáp nhập 2025 quanh vùng bản đồ)
ROLE_VI = {"farmer": "Nông dân", "admin_ward": "Quản lý xã/phường", "admin_province": "Quản lý tỉnh"}

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, full_name TEXT NOT NULL, name_key TEXT NOT NULL DEFAULT '', phone TEXT, email TEXT,
  pw_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'farmer' CHECK(role IN('farmer','admin_ward','admin_province')),
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN('active','locked')),
  province TEXT, province_key TEXT, ward TEXT, ward_key TEXT,
  fails INTEGER NOT NULL DEFAULT 0, locked_until REAL, last_seen REAL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE UNIQUE INDEX IF NOT EXISTS farmer_name ON users(name_key) WHERE role='farmer';
CREATE UNIQUE INDEX IF NOT EXISTS user_email ON users(lower(email)) WHERE email IS NOT NULL;
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS areas(id INTEGER PRIMARY KEY, level TEXT NOT NULL CHECK(level IN('province','ward')), province TEXT NOT NULL, province_key TEXT NOT NULL,
  ward TEXT, ward_key TEXT NOT NULL DEFAULT '', code TEXT UNIQUE NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(level, province_key, ward_key));
CREATE TABLE IF NOT EXISTS plots(id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL, crop TEXT,
  lon REAL NOT NULL, lat REAL NOT NULL, shared INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), title TEXT NOT NULL, body TEXT NOT NULL,
  dedupe_key TEXT UNIQUE NOT NULL, read INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS dm(id INTEGER PRIMARY KEY, sender_id INTEGER NOT NULL REFERENCES users(id), receiver_id INTEGER NOT NULL REFERENCES users(id),
  body TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS dm_pair ON dm(sender_id, receiver_id);
CREATE TABLE IF NOT EXISTS otps(id INTEGER PRIMARY KEY, purpose TEXT NOT NULL, email TEXT NOT NULL, code_hash TEXT NOT NULL, payload TEXT,
  expires_at REAL NOT NULL, tries INTEGER NOT NULL DEFAULT 0, sent_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY, at TEXT DEFAULT CURRENT_TIMESTAMP, actor_id INTEGER, action TEXT NOT NULL, target TEXT, detail TEXT);
"""


# ---------- tiện ích văn bản / khu vực ----------
def fold(s):
    """Bỏ dấu, hạ chữ thường, gọn khoảng trắng: dùng để so sánh tên."""
    s = unicodedata.normalize("NFD", str(s or "").lower().replace("đ", "d"))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def prov_name(s):
    return re.sub(r"^(tỉnh|thành phố|tp\.?)\s+", "", clean(s), flags=re.I)


def province_key(s):
    return re.sub(r"^(tinh|thanh pho|tp)\s+", "", fold(s))


def ward_key(s):
    return re.sub(r"^(phuong|xa|thi tran|dac khu|tt)\s+", "", fold(s))


def clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


# ---------- cơ sở dữ liệu ----------
def q(sql, a=(), one=False, write=False):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; c.execute("PRAGMA foreign_keys=ON")
    try:
        with c:
            cur = c.execute(sql, a)
            if write:
                return cur.lastrowid
            rows = [dict(r) for r in cur.fetchall()]
        return (rows[0] if rows else None) if one else rows
    finally:
        c.close()


def hash_pw(p, salt=None):
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.scrypt(p.encode(), salt=salt, n=2 ** 14, r=8, p=1).hex()


def check_pw(p, h):
    return hmac.compare_digest(hash_pw(p, bytes.fromhex(h.split("$")[0])), h)


sha = lambda t: hashlib.sha256(t.encode()).hexdigest()
_DUMMY_HASH = hash_pw(secrets.token_hex(8))      # so mật khẩu với hash giả khi không có tài khoản, để thời gian trả lời không lộ tài khoản nào tồn tại


# ---------- giới hạn tần suất (trong bộ nhớ, đủ cho 1 tiến trình) ----------
_rl = defaultdict(deque)
_rl_gc = {"t": 0.0}


def client_ip(req):
    """IP người dùng. Chỉ tin các mục do proxy CỦA BẠN thêm vào X-Forwarded-For (đếm từ bên phải), không tin phần người dùng tự gửi."""
    xff = [x.strip() for x in (req.headers.get("x-forwarded-for") or "").split(",") if x.strip()]
    if TRUSTED_PROXIES > 0 and len(xff) >= TRUSTED_PROXIES:
        return xff[-TRUSTED_PROXIES]
    return req.client.host if req.client else "?"


def _win(key, ident, per):
    now = time.time()
    if now - _rl_gc["t"] > 300:                      # dọn khóa cũ để bộ nhớ không phình ra
        _rl_gc["t"] = now
        for k in [k for k, d in _rl.items() if not d or now - d[-1] > 86400]:
            _rl.pop(k, None)
    d = _rl[(key, ident)]
    while d and now - d[0] > per:
        d.popleft()
    return d, now


def too_many(key, ident, n, per=3600):
    """Chỉ KIỂM TRA (không ghi): đã đạt n lần trong per giây chưa."""
    return len(_win(key, ident, per)[0]) >= n


def note(key, ident, per=3600):
    d, now = _win(key, ident, per)
    d.append(now)


def throttle(key, ident, n, per=3600, msg="Bạn thao tác quá nhanh, hãy thử lại sau ít phút"):
    """Kiểm tra rồi ghi một lần; vượt giới hạn thì báo 429."""
    if too_many(key, ident, n, per):
        raise HTTPException(429, msg)
    note(key, ident, per)


def migrate():
    """Nâng cơ sở dữ liệu cũ (HydroNisar: farmer/coop/admin, đăng nhập bằng số điện thoại) lên bản mới. Không xóa dữ liệu cũ."""
    c = sqlite3.connect(DB); c.execute("PRAGMA foreign_keys=OFF"); c.execute("PRAGMA legacy_alter_table=ON")   # đổi tên bảng mà không sửa khóa ngoại của bảng khác
    try:
        old = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
        if old and "name_key" not in old[0]:
            rows = c.execute("SELECT id,phone,full_name,pw_hash,role,status,fails,locked_until,created_at FROM users").fetchall()
            c.execute("ALTER TABLE users RENAME TO users_v1")
            c.executescript(SCHEMA_V2.split("CREATE TABLE IF NOT EXISTS sessions")[0])
            seen = set()
            for (i, ph, nm, pw, role, st, fl, lu, ca) in rows:
                k = fold(nm) or f"nong dan {i}"
                if k in seen:
                    k = f"{k} {i}"
                seen.add(k)
                c.execute("INSERT INTO users(id,full_name,name_key,phone,pw_hash,role,status,fails,locked_until,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (i, nm, k, ph, pw, "admin_province" if role == "admin" else "farmer", st, fl, lu, ca))
        c.executescript(SCHEMA_V2)
        cols = {r[1] for r in c.execute("PRAGMA table_info(audit_log)")}
        for col in ("province_key", "ward_key"):
            if col not in cols:
                c.execute(f"ALTER TABLE audit_log ADD COLUMN {col} TEXT")
        if "pending" not in {r[1] for r in c.execute("PRAGMA table_info(users)")}:      # quản lý xã/phường chờ tỉnh duyệt
            c.execute("ALTER TABLE users ADD COLUMN pending INTEGER NOT NULL DEFAULT 0")
        if "level" not in {r[1] for r in c.execute("PRAGMA table_info(sessions)")}:     # full = đăng nhập bằng mật khẩu, limited = vào bằng "Quên mật khẩu"
            c.execute("ALTER TABLE sessions ADD COLUMN level TEXT NOT NULL DEFAULT 'full'")
            c.execute("DELETE FROM sessions")      # một lần duy nhất: mọi người đăng nhập lại để phiên cũ (kể cả phiên Quên mật khẩu cũ) không còn quyền đầy đủ
        c.commit()
    finally:
        c.close()


migrate()
if os.getenv("HN_ADMIN_PASSWORD") and (os.getenv("HN_ADMIN_EMAIL") or os.getenv("HN_ADMIN_PHONE")) \
        and not q("SELECT 1 FROM users WHERE role='admin_province'", one=True):
    q("INSERT INTO users(full_name,name_key,phone,email,pw_hash,role) VALUES(?,?,?,?,?,'admin_province')",
      ("Quản trị", "quan tri", os.getenv("HN_ADMIN_PHONE"), os.getenv("HN_ADMIN_EMAIL"), hash_pw(os.environ["HN_ADMIN_PASSWORD"])), write=True)


def area_for(level, province, ward=None):
    """Lấy (hoặc tạo) khu vực và mã tham gia của tỉnh / xã-phường."""
    pk, wk = province_key(province), (ward_key(ward) if level == "ward" else "")
    for _ in range(5):
        a = q("SELECT * FROM areas WHERE level=? AND province_key=? AND ward_key=?", (level, pk, wk), one=True)
        if a:
            return a
        try:
            q("INSERT INTO areas(level,province,province_key,ward,ward_key,code) VALUES(?,?,?,?,?,?)",
              (level, prov_name(province), pk, clean(ward) if level == "ward" else None, wk, ("X-" if level == "ward" else "T-") + secrets.token_hex(3).upper()), write=True)
        except sqlite3.IntegrityError:
            pass
    raise HTTPException(500, "Không tạo được mã khu vực")


def audit(actor, text, about=None):
    """Ghi nhật ký bằng một câu tiếng Việt. actor / about là hàng người dùng (dict) hoặc None."""
    a = about or actor or {}
    q("INSERT INTO audit_log(actor_id,action,target,detail,province_key,ward_key) VALUES(?,?,?,?,?,?)",
      (actor["id"] if actor else None, "log", "", text, a.get("province_key"), a.get("ward_key")), write=True)


def notify(uid, title, body, key):
    q("INSERT OR IGNORE INTO notifications(user_id,title,body,dedupe_key) VALUES(?,?,?,?)", (uid, title, body, key), write=True)


def place(u):
    return ", ".join(x for x in (u.get("ward"), u.get("province")) if x)


# ---------- đăng nhập, phiên ----------
def user(req: Request):
    """Người đang đăng nhập, kể cả phiên hạn chế (u["level"] == "limited"). Chỉ dùng cho các API được phép trong chế độ hạn chế."""
    t = req.cookies.get("hn_session")
    u = t and q("SELECT u.*, s.level AS level FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=? AND s.expires_at>? AND u.status='active'", (sha(t), time.time()), one=True)
    if not u:
        raise HTTPException(401, "Chưa đăng nhập")
    if time.time() - (u["last_seen"] or 0) > 60:
        q("UPDATE users SET last_seen=? WHERE id=?", (time.time(), u["id"]), write=True)
    return u


def full(u=Depends(user)):
    """Mặc định cho mọi API: phải đăng nhập bằng mật khẩu. Phiên hạn chế (Quên mật khẩu) bị chặn ở đây."""
    if u["level"] != "full":
        raise HTTPException(403, "Chức năng này cần đăng nhập bằng mật khẩu")
    return u


def need(*roles):
    def dep(u=Depends(full)):
        if u["role"] not in roles:
            raise HTTPException(403, "Không đủ quyền")
        return u
    return dep


ADMINS = ("admin_ward", "admin_province")
router = APIRouter()


def start_session(res, uid, level="full"):
    days = SESSION_DAYS if level == "full" else LIMITED_DAYS
    t = secrets.token_urlsafe(32)
    q("DELETE FROM sessions WHERE expires_at<?", (time.time(),), write=True)      # dọn phiên hết hạn
    q("INSERT INTO sessions(id,user_id,expires_at,level) VALUES(?,?,?,?)", (sha(t), uid, time.time() + days * 86400, level), write=True)
    q("UPDATE users SET last_seen=? WHERE id=?", (time.time(), uid), write=True)
    res.set_cookie("hn_session", t, httponly=True, samesite="lax", secure=COOKIE_SECURE, max_age=days * 86400, path="/")


class Reg(BaseModel):
    full_name: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=8, max_length=100)
    province: str = Field(min_length=2, max_length=60)
    ward: str = Field(min_length=1, max_length=60)


class Login(BaseModel):
    ident: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=200)


class Forgot(BaseModel):
    name: str = Field(min_length=2, max_length=80)


@router.get("/geo/provinces")
def geo_provinces():
    return PROVINCES


_geo = {"t": 0.0, "lock": threading.Lock(), "cache": {}}


@router.get("/geo/reverse")
def geo_reverse(req: Request, lat: float, lon: float):
    """Từ tọa độ suy ra tỉnh và xã/phường. KHÔNG lưu tọa độ; chỉ trả về tên."""
    if not (8 <= lat <= 24 and 102 <= lon <= 110):
        raise HTTPException(400, "Vị trí nằm ngoài Việt Nam")
    key = (round(lat, 3), round(lon, 3))
    if key in _geo["cache"]:
        return _geo["cache"][key]
    throttle("geo", client_ip(req), 20, 600)          # chỉ tính các lượt thật sự gọi Nominatim
    with _geo["lock"]:                                   # Nominatim yêu cầu tối đa 1 lượt/giây
        time.sleep(max(0, 1.1 - (time.time() - _geo["t"])))
        try:
            r = httpx.get("https://nominatim.openstreetmap.org/reverse", timeout=12,
                          params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 14, "addressdetails": 1, "accept-language": "vi"},
                          headers={"User-Agent": "EcoPulse/1.0 " + (os.getenv("NOMINATIM_CONTACT") or "")})
            r.raise_for_status()
            ad = r.json().get("address") or {}
        except Exception:
            raise HTTPException(502, "Không tra được tên xã/phường từ vị trí. Bạn hãy nhập tay.")
        finally:
            _geo["t"] = time.time()
    prov = ad.get("state") or ad.get("province") or ad.get("city") or ""
    ward = next((ad[k] for k in ("suburb", "quarter", "village", "town", "city_district", "municipality", "hamlet") if ad.get(k)), "")
    out = {"province": re.sub(r"^(Tỉnh|Thành phố)\s+", "", prov), "ward": ward}
    if len(_geo["cache"]) > 5000:
        _geo["cache"].clear()
    _geo["cache"][key] = out
    return out


@router.get("/auth/name-free")
def name_free(req: Request, name: str):
    throttle("namefree", client_ip(req), 60, 600)
    return {"free": not q("SELECT 1 FROM users WHERE role='farmer' AND name_key=?", (fold(name),), one=True)}


@router.post("/auth/register")
def register(b: Reg, req: Request, res: Response):
    throttle("register", client_ip(req), 30, 3600, "Đăng ký quá nhiều lần từ cùng một nơi, hãy thử lại sau")
    name, k = clean(b.full_name), fold(b.full_name)
    if not k:
        raise HTTPException(400, "Tên chưa hợp lệ")
    if q("SELECT 1 FROM users WHERE role='farmer' AND name_key=?", (k,), one=True):
        raise HTTPException(409, "Tên này đã có người dùng. Hãy thêm số, tên đệm hoặc tên ấp/thôn để phân biệt")
    try:
        uid = q("INSERT INTO users(full_name,name_key,pw_hash,province,province_key,ward,ward_key) VALUES(?,?,?,?,?,?,?)",
                (name, k, hash_pw(b.password), prov_name(b.province), province_key(b.province), clean(b.ward), ward_key(b.ward)), write=True)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Tên này đã có người dùng")
    me_ = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    audit(me_, f"{name} đã đăng ký tài khoản nông dân ({place(me_)})")
    start_session(res, uid)
    return {"ok": True}


def find_login(ident):
    s = ident.strip()
    if "@" in s:
        return q("SELECT * FROM users WHERE lower(email)=?", (s.lower(),), one=True)
    if re.fullmatch(r"\+?\d{9,12}", s):                  # tài khoản quản trị cũ đăng nhập bằng số điện thoại
        return q("SELECT * FROM users WHERE phone=? AND role<>'farmer'", (s,), one=True)
    return q("SELECT * FROM users WHERE name_key=? AND (role='farmer' OR email IS NULL)", (fold(s),), one=True)


def _bad_password(u, fail_key):
    """Ghi nhận một lần sai mật khẩu: tính theo IP+tên và theo tài khoản (khóa 15 phút sau MAX_FAILS lần)."""
    note("login_fail", fail_key, 900)
    q("UPDATE users SET fails=fails+1, locked_until=CASE WHEN fails+1>=? THEN ? END WHERE id=?", (MAX_FAILS, time.time() + LOCK_SEC, u["id"]), write=True)


@router.post("/auth/login")
def login(b: Login, req: Request, res: Response):
    ip, ident = client_ip(req), fold(b.ident)[:80]
    fail_key = f"{ip}|{ident}"
    if too_many("login_ip", ip, 60, 900) or too_many("login_fail", fail_key, 8, 900):
        raise HTTPException(429, "Bạn thử đăng nhập quá nhiều lần, hãy đợi vài phút rồi thử lại")
    note("login_ip", ip, 900)
    bad = HTTPException(401, "Sai tên/email hoặc mật khẩu, hoặc tài khoản đang bị khóa")
    u = find_login(b.ident)
    if not u:
        check_pw(b.password, _DUMMY_HASH)          # tốn thời gian như khi có tài khoản
        note("login_fail", fail_key, 900)
        raise bad
    if (u["locked_until"] or 0) > time.time():
        raise bad
    if u["pending"]:                               # quản lý xã/phường chưa được tỉnh duyệt: chỉ báo khi mật khẩu đúng
        if check_pw(b.password, u["pw_hash"]):
            raise HTTPException(403, "Tài khoản đang chờ quản lý tỉnh duyệt. Bạn sẽ đăng nhập được sau khi được duyệt.")
        _bad_password(u, fail_key)
        raise bad
    if u["status"] != "active":
        raise bad
    if not check_pw(b.password, u["pw_hash"]):
        _bad_password(u, fail_key)
        raise bad
    q("UPDATE users SET fails=0, locked_until=NULL WHERE id=?", (u["id"],), write=True)
    q("DELETE FROM sessions WHERE user_id=? AND level='limited'", (u["id"],), write=True)   # chủ tài khoản vừa đăng nhập thật: thu hồi mọi phiên "Quên mật khẩu"
    start_session(res, u["id"])
    return {"ok": True}


@router.post("/auth/forgot")
def forgot(b: Forgot, req: Request, res: Response):
    """Quên mật khẩu (chỉ nông dân): vào CHẾ ĐỘ HẠN CHẾ, chỉ xem được "Thửa của tôi" (không sửa/xóa, không chat, không tin tức, không trợ lý).
    Muốn mở đầy đủ phải nhập mật khẩu (POST /auth/upgrade)."""
    k = fold(b.name)
    throttle("forgot_ip", client_ip(req), 15, 900)
    throttle("forgot_name", k, 6, 900)
    u = q("SELECT * FROM users WHERE role='farmer' AND name_key=?", (k,), one=True)
    if not u or u["status"] != "active":
        raise HTTPException(404, "Không tìm thấy nông dân có tên này, hoặc tài khoản đang bị khóa")
    audit(u, f"{u['full_name']} vào xem thửa bằng chức năng quên mật khẩu (chế độ chỉ xem)")
    notify(u["id"], "Có người vào tài khoản bằng \"Quên mật khẩu\"",
           "Phiên này chỉ được xem thửa, không nhắn tin hay sửa dữ liệu. Nếu không phải bạn, hãy báo cán bộ xã/phường.", f"forgot:{u['id']}:{int(time.time() // 3600)}")
    start_session(res, u["id"], "limited")
    return {"ok": True, "limited": True}


class Upgrade(BaseModel):
    password: str = Field(min_length=1, max_length=200)


@router.post("/auth/upgrade")
def upgrade(b: Upgrade, req: Request, res: Response, u=Depends(user)):
    """Nhập mật khẩu để nâng phiên hạn chế thành phiên đầy đủ (cấp lại token mới)."""
    if u["level"] == "full":
        return {"ok": True}
    fail_key = f"{client_ip(req)}|u{u['id']}"
    if too_many("login_fail", fail_key, 8, 900) or (u["locked_until"] or 0) > time.time():
        raise HTTPException(429, "Bạn thử quá nhiều lần, hãy đợi vài phút rồi thử lại")
    if not check_pw(b.password, u["pw_hash"]):
        _bad_password(u, fail_key)
        raise HTTPException(401, "Mật khẩu chưa đúng")
    q("UPDATE users SET fails=0, locked_until=NULL WHERE id=?", (u["id"],), write=True)
    old = req.cookies.get("hn_session")
    q("DELETE FROM sessions WHERE user_id=? AND level='limited'", (u["id"],), write=True)
    old and q("DELETE FROM sessions WHERE id=?", (sha(old),), write=True)
    start_session(res, u["id"])
    return {"ok": True}


@router.post("/auth/logout")
def logout(req: Request, res: Response):
    t = req.cookies.get("hn_session")
    t and q("DELETE FROM sessions WHERE id=?", (sha(t),), write=True)
    res.delete_cookie("hn_session")
    return {"ok": True}


# ---------- OTP qua email ----------
LOGO_FILE = Path(__file__).resolve().parent.parent / "static" / "img" / "logo.png"
_logo = {}


def logo_png():
    """Logo thu nhỏ (khoảng 120px) để nhúng vào email, không gửi nguyên file 1,8 MB."""
    if "b" not in _logo:
        try:
            import io
            from PIL import Image
            im = Image.open(LOGO_FILE).convert("RGB"); im.thumbnail((120, 120))
            buf = io.BytesIO(); im.save(buf, "PNG", optimize=True); _logo["b"] = buf.getvalue()
        except Exception:
            _logo["b"] = None
    return _logo["b"]


def otp_html(code, intro, minutes, logo):
    from html import escape
    img = ('<img src="cid:logo" width="64" height="64" alt="EcoPulse" style="display:block;border-radius:50%;border:3px solid #ffffff;margin:0 auto 10px">' if logo else "")
    return f"""<!doctype html><html lang="vi"><body style="margin:0;padding:0;background:#eef5f1;font-family:'Segoe UI',Arial,Helvetica,sans-serif;color:#17323a">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef5f1"><tr><td align="center" style="padding:28px 12px">
<table role="presentation" width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #d8e4e8">
<tr><td align="center" style="background:#2f9e5a;background-image:linear-gradient(135deg,#2f9e5a,#2a9fe0);padding:28px 20px 22px">
{img}<div style="font-size:26px;font-weight:800;color:#ffffff;letter-spacing:.5px">Eco<span style="color:#d6f0ff">Pulse</span></div>
<div style="font-size:13px;color:#e8f7ee;margin-top:4px">Độ ẩm đất từ vệ tinh cho nông dân</div></td></tr>
<tr><td style="padding:28px 30px 8px;font-size:15px;line-height:1.6">
<p style="margin:0 0 14px">Xin chào,</p><p style="margin:0 0 18px">{escape(intro)}</p>
<div style="text-align:center;margin:6px 0 20px"><span style="display:inline-block;background:#f1faf5;border:2px dashed #2f9e5a;border-radius:12px;padding:14px 26px;font-size:36px;font-weight:800;letter-spacing:10px;color:#1f7a43;font-family:Consolas,'Courier New',monospace">{escape(code)}</span></div>
<p style="margin:0 0 6px;text-align:center;color:#5d7279;font-size:14px">⏱ Mã có hiệu lực trong <b>{minutes} phút</b>.</p></td></tr>
<tr><td style="padding:10px 30px 26px"><div style="background:#fff8e1;border-left:4px solid #e0a100;border-radius:6px;padding:10px 14px;font-size:13px;line-height:1.5;color:#6b5200">
🔒 <b>Không chia sẻ mã này cho bất kỳ ai</b>, kể cả người tự nhận là nhân viên EcoPulse.</div>
<p style="margin:16px 0 0;font-size:13px;color:#5d7279">Nếu bạn không yêu cầu mã này, hãy bỏ qua email, tài khoản của bạn vẫn an toàn.</p></td></tr>
<tr><td align="center" style="background:#f6faf8;border-top:1px solid #e3ecef;padding:14px 20px;font-size:12px;color:#8aa0a7">EcoPulse · Dữ liệu NISAR đang hiệu chuẩn, thông tin chỉ mang tính tham khảo.</td></tr>
</table></td></tr></table></body></html>"""


def send_mail(to, subject, text, html=None, logo=None):
    """Gửi email qua SMTP (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM trong .env). Chưa cấu hình thì in nội dung ra cửa sổ chạy server."""
    host = os.getenv("SMTP_HOST")
    if not host:
        print(f"\n[EcoPulse] Chưa cấu hình SMTP, nội dung email gửi tới {to}:\n  {subject}\n  {text}\n", flush=True)
        return False
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER") or "ecopulse@localhost", to, subject
    m.set_content(text)
    if html:
        m.add_alternative(html, subtype="html")
        if logo:
            m.get_payload()[1].add_related(logo, "image", "png", cid="<logo>")
    port = int(os.getenv("SMTP_PORT") or 587)
    try:
        s = smtplib.SMTP_SSL(host, port, timeout=15) if port == 465 else smtplib.SMTP(host, port, timeout=15)
        with s:
            if port != 465:
                s.starttls()
            if os.getenv("SMTP_USER"):
                s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS") or "")
            s.send_message(m)
    except Exception as e:
        print("[EcoPulse] Lỗi gửi email:", e, flush=True)
        raise HTTPException(502, "Không gửi được email. Hãy kiểm tra lại địa chỉ email hoặc cấu hình SMTP")
    return True


def issue_otp(purpose, email, payload, subject, intro="Mã xác nhận của bạn là:"):
    email = email.lower()
    last = q("SELECT sent_at FROM otps WHERE purpose=? AND email=? ORDER BY id DESC LIMIT 1", (purpose, email), one=True)
    if last and time.time() - last["sent_at"] < OTP_GAP:
        raise HTTPException(429, f"Vui lòng đợi {OTP_GAP} giây rồi gửi lại mã")
    throttle("otp_issue", email, 5, 3600, "Đã gửi quá nhiều mã tới email này, hãy thử lại sau 1 giờ")     # cấp mã mới làm đặt lại số lần thử, nên phải giới hạn
    code = f"{secrets.randbelow(10 ** 6):06d}"
    q("DELETE FROM otps WHERE purpose=? AND email=?", (purpose, email), write=True)
    q("INSERT INTO otps(purpose,email,code_hash,payload,expires_at,sent_at) VALUES(?,?,?,?,?,?)",
      (purpose, email, sha(code + email), json.dumps(payload), time.time() + OTP_TTL, time.time()), write=True)
    plain = f"{intro} {code}. Mã có hiệu lực trong {OTP_TTL // 60} phút. Không chia sẻ mã này cho bất kỳ ai."
    logo = logo_png()
    return send_mail(email, subject, plain, otp_html(code, intro, OTP_TTL // 60, logo), logo)


def check_otp(purpose, email, code):
    email = email.lower()
    o = q("SELECT * FROM otps WHERE purpose=? AND email=? ORDER BY id DESC LIMIT 1", (purpose, email), one=True)
    if not o or o["expires_at"] < time.time():
        raise HTTPException(400, "Mã đã hết hạn hoặc chưa được gửi. Hãy gửi lại mã")
    if o["tries"] >= OTP_TRIES:
        raise HTTPException(429, "Nhập sai quá nhiều lần. Hãy gửi lại mã mới")
    if not hmac.compare_digest(o["code_hash"], sha(clean(code) + email)):
        q("UPDATE otps SET tries=tries+1 WHERE id=?", (o["id"],), write=True)
        raise HTTPException(400, "Mã chưa đúng")
    q("DELETE FROM otps WHERE id=?", (o["id"],), write=True)
    return json.loads(o["payload"] or "{}")


EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class AdminReg(BaseModel):
    full_name: str = Field(min_length=2, max_length=80)
    phone: str = Field(pattern=r"^\+?\d{9,12}$")
    email: str = Field(pattern=EMAIL_RE, max_length=120)
    province: str = Field(min_length=2, max_length=60)
    ward: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=8, max_length=100)


class OtpIn(BaseModel):
    email: str = Field(pattern=EMAIL_RE)
    code: str = Field(min_length=4, max_length=8)


@router.post("/auth/admin/start")
def admin_start(b: AdminReg, req: Request):
    """Đăng ký quản lý xã/phường. KHÔNG còn mã OTP qua email: tạo luôn tài khoản ở trạng thái CHỜ DUYỆT
    (khóa + pending=1): chưa đăng nhập được, chưa hiện trong danh bạ, chưa nhận tin nhắn cho tới khi quản lý tỉnh bấm Duyệt."""
    throttle("adminreg_ip", client_ip(req), 8, 3600, "Đăng ký quá nhiều lần từ cùng một nơi, hãy thử lại sau")
    email = b.email.lower()
    if q("SELECT 1 FROM users WHERE lower(email)=?", (email,), one=True):
        raise HTTPException(409, "Email này đã được đăng ký")
    if not q("SELECT 1 FROM users WHERE role='admin_province' AND status='active' AND (province_key IS NULL OR province_key=?)", (province_key(b.province),), one=True):
        raise HTTPException(400, "Tỉnh này chưa có quản lý tỉnh để duyệt đăng ký. Hãy liên hệ quản trị hệ thống.")
    name, prov, ward = clean(b.full_name), prov_name(b.province), clean(b.ward)
    try:
        uid = q("INSERT INTO users(full_name,name_key,phone,email,pw_hash,role,province,province_key,ward,ward_key,status,pending) VALUES(?,?,?,?,?,'admin_ward',?,?,?,?,'locked',1)",
                (name, fold(name), b.phone, email, hash_pw(b.password), prov, province_key(prov), ward, ward_key(ward)), write=True)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Email này đã được đăng ký")
    me_ = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    audit(me_, f"{me_['full_name']} đăng ký tài khoản quản lý {place(me_)} (chờ tỉnh duyệt)")
    for a in q("SELECT id FROM users WHERE role='admin_province' AND status='active' AND (province_key IS NULL OR province_key=?)", (me_["province_key"],)):
        notify(a["id"], "Có quản lý xã/phường chờ duyệt", f"{me_['full_name']} đăng ký quản lý {place(me_)}. Vào mục Người dùng để duyệt hoặc từ chối.", f"adminreg:{uid}:{a['id']}")
    return {"ok": True, "pending": True, "emailed": False}


@router.post("/auth/admin/verify")
def admin_verify(b: OtpIn, req: Request):
    """Giữ lại để giao diện cũ (còn bước nhập mã) không bị lỗi: không kiểm tra mã nữa, chỉ xác nhận đăng ký đang chờ duyệt.
    Khi giao diện đã bỏ bước nhập mã thì có thể xóa hàm này."""
    throttle("adminverify_ip", client_ip(req), 30, 3600)
    if q("SELECT 1 FROM users WHERE lower(email)=? AND pending=1", (b.email.lower(),), one=True):
        return {"ok": True, "pending": True}
    raise HTTPException(400, "Không tìm thấy đăng ký đang chờ duyệt. Hãy đăng ký lại.")


# ---------- thông tin tài khoản ----------
def my_area(u):
    if u["role"] == "admin_ward":
        return {"ward": area_for("ward", u["province"], u["ward"])["code"], "province": area_for("province", u["province"])["code"]}
    if u["role"] == "admin_province" and u["province"]:
        return {"province": area_for("province", u["province"])["code"]}
    return None


@router.get("/me")
def me(u=Depends(user)):
    n = lambda sql: q(sql, (u["id"],), one=True)["n"]
    pend = q("SELECT COUNT(*) n FROM news n JOIN users a ON a.id=n.author_id WHERE n.deleted=0 AND n.status='pending' AND (? IS NULL OR a.province_key=?)",
             (u["province_key"], u["province_key"]), one=True)["n"] if u["role"] == "admin_province" else 0
    pend_admins = q("SELECT COUNT(*) n FROM users WHERE pending=1 AND role='admin_ward' AND (? IS NULL OR province_key=?)", (u["province_key"], u["province_key"]), one=True)["n"] if u["role"] == "admin_province" else 0
    return {"id": u["id"], "name": u["full_name"], "role": u["role"], "role_vi": ROLE_VI[u["role"]], "phone": u["phone"], "email": u["email"],
            "limited": u["level"] != "full", "pending_admins": pend_admins,
            "province": u["province"], "ward": u["ward"], "place": place(u), "codes": my_area(u),
            "unread": n("SELECT COUNT(*) n FROM notifications WHERE user_id=? AND read=0"),
            "unread_chat": n("SELECT COUNT(*) n FROM dm WHERE receiver_id=? AND read=0"), "news_pending": pend}


class Where(BaseModel):
    province: str = Field(min_length=2, max_length=60)
    ward: str = Field(min_length=1, max_length=60)


@router.post("/me/location")
def set_location(b: Where, u=Depends(need("farmer"))):
    q("UPDATE users SET province=?,province_key=?,ward=?,ward_key=? WHERE id=?", (prov_name(b.province), province_key(b.province), clean(b.ward), ward_key(b.ward), u["id"]), write=True)
    audit({**u, "province_key": province_key(b.province), "ward_key": ward_key(b.ward)}, f"{u['full_name']} cập nhật nơi ở: {clean(b.ward)}, {prov_name(b.province)}")
    return {"ok": True}


# ---------- trạng thái thửa từ các cảnh độ ẩm đã có trong cache ----------
SCENE_T = re.compile(r"_(\d{8}T\d{6})_\d{8}T")


def status_of(lon, lat):
    for j in sorted(render.CACHE.glob("*__soil.json"), key=lambda p: SCENE_T.search(p.name).group(1) if SCENE_T.search(p.name) else "", reverse=True):
        try:
            v = render.value(j.stem, lon, lat)
        except Exception:
            continue
        if v is not None:
            cls, unc = render.SOIL_CLS, render.LAYERS["soil"].get("unc", 0)
            cidx = lambda x: next((k for k, c in enumerate(cls) if c["max"] is None or x < c["max"]), len(cls) - 1)
            i, lo_i, hi_i = cidx(v), cidx(v - unc), cidx(v + unc)
            c = cls[i]
            t = SCENE_T.search(j.name).group(1)
            return {"value": v, "idx": i, "name": c["name"], "act": c["action"], "state": c["state"], "do": [c["todo"], c["tip"]], "color": c["color"],
                    "unc": unc, "near": [cls[lo_i]["name"], cls[hi_i]["name"]] if lo_i != hi_i else None,
                    "scene_time": datetime.strptime(t, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).isoformat()}
    return None


def plot_row(p):
    return {"id": p["id"], "name": p["name"], "crop": p["crop"], "lon": p["lon"], "lat": p["lat"], "status": status_of(p["lon"], p["lat"]), "photo": photo_url(p["id"])}


# ---------- thời tiết tại thửa + lời khuyên ----------
_wx = {}


def weather_at(lat, lon):
    key = (round(lat, 2), round(lon, 2))
    hit = _wx.get(key)
    if hit and time.time() - hit[0] < 30 * 60:
        return hit[1]
    r = httpx.get("https://api.open-meteo.com/v1/forecast", timeout=15, params={
        "latitude": key[0], "longitude": key[1], "timezone": "Asia/Bangkok", "forecast_days": 3,
        "current": "temperature_2m,relative_humidity_2m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max"})
    r.raise_for_status()
    js = r.json()
    d, cur = js.get("daily") or {}, js.get("current") or {}
    days = [{"date": d["time"][i], "tmax": d["temperature_2m_max"][i], "tmin": d["temperature_2m_min"][i],
             "rain": d["precipitation_sum"][i] or 0, "prob": (d.get("precipitation_probability_max") or [None] * 9)[i]} for i in range(len(d.get("time") or []))]
    out = {"now": cur.get("temperature_2m"), "rh": cur.get("relative_humidity_2m"), "days": days}
    _wx[key] = (time.time(), out)
    return out


def crop_of(crop):
    k = fold(crop)
    return next((n for n in ("ca phe", "sau rieng", "cao su", "ho tieu") if n in k), "")


def temp_advice(w, crop):
    days = w["days"]
    tmax, tmin = max(x["tmax"] for x in days if x["tmax"] is not None), min(x["tmin"] for x in days if x["tmin"] is not None)
    c = crop_of(crop)
    if tmax >= 36:
        lv, title, tips = "bad", "Nắng nóng gay gắt", ["Tưới vào sáng sớm hoặc chiều mát, tránh tưới lúc nắng gắt.", "Tủ gốc bằng rơm, cỏ khô để giữ ẩm; che nắng cho cây con.", "Hoãn bón phân, phun thuốc vào buổi trưa."]
    elif tmax >= 33:
        lv, title, tips = "warn", "Trời nóng", ["Theo dõi độ ẩm đất, tưới đủ nước vào sáng sớm.", "Hạn chế làm việc ngoài ruộng từ 11 đến 15 giờ."]
    elif tmin <= 12:
        lv, title, tips = "bad", "Đêm rất lạnh", ["Che chắn cây con và cây mới trồng.", "Hạn chế tưới nước vào chiều tối."]
    elif tmin <= 15:
        lv, title, tips = "warn", "Đêm se lạnh", ["Theo dõi cây con, cây mới trồng.", "Nên tưới vào buổi sáng thay vì chiều tối."]
    else:
        lv, title, tips = "good", "Nhiệt độ thuận lợi", ["Có thể làm việc đồng áng bình thường."]
    if c == "ca phe" and tmax >= 33:
        tips.append("Cà phê: nắng nóng kéo dài dễ làm rụng quả non, nên giữ ẩm gốc và duy trì cây che bóng.")
    if c == "sau rieng" and tmax >= 35:
        tips.append("Sầu riêng: thời tiết nóng dễ làm rụng hoa và quả non, nên giữ ẩm gốc đều đặn.")
    return {"level": lv, "title": title, "tips": tips, "range": [tmin, tmax]}


def rain_advice(w, crop, soil_idx):
    days = w["days"]
    tot, mx = sum(x["rain"] for x in days), max(x["rain"] for x in days)
    probs = [x["prob"] for x in days if x["prob"] is not None]
    c = crop_of(crop)
    dry = soil_idx in (0, 1)
    if mx >= 50:
        lv, title, tips = "bad", "Có thể mưa rất to", ["Thông rãnh thoát nước, không để nước đọng quanh gốc.", "Hoãn bón phân và phun thuốc.", "Kiểm tra cây cao, cây mang quả (nguy cơ đổ ngã)."]
    elif tot >= 15:
        lv, title, tips = "warn", "Có mưa vừa", ["Có thể hoãn tưới, chờ mưa.", "Kiểm tra thoát nước ở những chỗ trũng."]
    elif tot >= 3:
        lv, title, tips = "warn" if dry else "good", "Có mưa nhỏ", ["Mưa nhỏ thường chưa đủ làm ướt sâu, hãy kiểm tra lại độ ẩm đất sau mưa."]
    else:
        lv, title, tips = ("warn" if dry else "good"), "Gần như không mưa", ["Đất đang khô, cần chủ động tưới." if dry else "Theo dõi độ ẩm đất để tưới khi cần."]
    if dry and tot >= 15:
        tips.append("Đất đang khô nhưng sắp có mưa: có thể đợi mưa rồi mới tưới để tiết kiệm nước.")
    if c == "cao su" and tot >= 3:
        tips.append("Cao su: tránh cạo mủ khi trời đang mưa hoặc cây còn ướt.")
    return {"level": lv, "title": title, "tips": tips, "total": round(tot, 1), "prob": max(probs) if probs else None}


# ---------- thửa (chủ thửa) ----------
class PlotIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    crop: str = Field(default="", max_length=40)
    lon: float = Field(ge=100, le=112)
    lat: float = Field(ge=8, le=17)


@router.get("/plots")
def plots(u=Depends(user)):
    return [{**plot_row(p), "place": place(u)} for p in q("SELECT * FROM plots WHERE owner_id=? AND deleted=0 ORDER BY id DESC", (u["id"],))]


@router.post("/plots")
def add_plot(b: PlotIn, u=Depends(full)):
    throttle("plot_add", u["id"], 30, 3600)
    if q("SELECT COUNT(*) n FROM plots WHERE owner_id=? AND deleted=0", (u["id"],), one=True)["n"] >= 50:
        raise HTTPException(400, "Bạn đã khai báo tối đa 50 thửa. Hãy xóa bớt thửa không dùng.")
    pid = q("INSERT INTO plots(owner_id,name,crop,lon,lat) VALUES(?,?,?,?,?)", (u["id"], b.name.strip(), b.crop.strip(), b.lon, b.lat), write=True)
    r = plot_row(q("SELECT * FROM plots WHERE id=?", (pid,), one=True))
    s = r["status"]
    if s and s["idx"] in ALERT_BANDS:
        notify(u["id"], f"Thửa {b.name}: {s['name']}", f"{s['act']}. {s['state']}", f"plot:{pid}:{s['scene_time']}")
    audit(u, f"{u['full_name']} thêm thửa \"{b.name.strip()}\"" + (f" (trồng {b.crop.strip()})" if b.crop.strip() else ""))
    return r


@router.get("/plots/{pid}")
def plot_detail(pid: int, u=Depends(user)):
    p = q("SELECT * FROM plots WHERE id=? AND owner_id=? AND deleted=0", (pid, u["id"]), one=True)
    if not p:
        raise HTTPException(404, "Không tìm thấy thửa này")
    r = plot_row(p)
    r["place"] = place(u)
    try:
        w = weather_at(p["lat"], p["lon"])
        r["weather"] = {"now": w["now"], "rh": w["rh"], "days": w["days"], "temp": temp_advice(w, p["crop"]),
                        "rain": rain_advice(w, p["crop"], r["status"]["idx"] if r["status"] else None)}
    except Exception:
        r["weather"] = None
    return r


@router.delete("/plots/{pid}")
def del_plot(pid: int, u=Depends(full)):
    p = q("SELECT name FROM plots WHERE id=? AND owner_id=? AND deleted=0", (pid, u["id"]), one=True)
    q("UPDATE plots SET deleted=1 WHERE id=? AND owner_id=?", (pid, u["id"]), write=True)
    p and audit(u, f"{u['full_name']} xóa thửa \"{p['name']}\"")
    return {"ok": True}


@router.get("/notifications")
def notifs(u=Depends(full)):
    out = q("SELECT id,title,body,read,created_at,dedupe_key FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50", (u["id"],))
    for r in out:      # phân loại thư: review = bài chờ duyệt, news = kết quả duyệt, alert = cảnh báo thửa, info = thông báo chung
        k = (r.pop("dedupe_key") or "").split(":")
        r["kind"], r["news_id"] = ("review" if k[2:3] == ["sub"] else "news", int(k[1])) if k[0] == "news" and k[1:2] and k[1].isdigit() else ("alert" if k[0] == "plot" else "info", None)
    return out


@router.post("/notifications/{nid}/read")
def read_notif(nid: int, u=Depends(full)):
    q("UPDATE notifications SET read=1 WHERE id=? AND user_id=?", (nid, u["id"]), write=True)
    return {"ok": True}


# ---------- nhắn tin riêng (nông dân <-> quản lý, quản lý tỉnh <-> quản lý xã/phường) ----------
REACH = {   # những người mà mỗi vai trò được nhắn tin (đặt tên tham số :pk tỉnh, :wk xã/phường, :me chính mình)
    "farmer": "(role='admin_ward' AND province_key=:pk AND ward_key=:wk) OR (role='admin_province' AND (province_key IS NULL OR province_key=:pk))",
    "admin_ward": "(role='farmer' AND province_key=:pk AND ward_key=:wk) OR (role='admin_province' AND (province_key IS NULL OR province_key=:pk))",
    "admin_province": "(role='admin_ward' AND (:pk IS NULL OR province_key=:pk)) OR (role='farmer' AND (:pk IS NULL OR province_key=:pk) "
                      "AND id IN (SELECT sender_id FROM dm WHERE receiver_id=:me UNION SELECT receiver_id FROM dm WHERE sender_id=:me))"}


def reach_args(u, **kw):
    return {"pk": u["province_key"], "wk": u["ward_key"], "me": u["id"], **kw}


def peer_view(p, unread=0, last=None, last_at=None):
    mins = None if not p["last_seen"] else max(0, int((time.time() - p["last_seen"]) // 60))
    adm = p["role"] in ADMINS
    return {"id": p["id"], "name": p["full_name"], "role": p["role"], "role_vi": ROLE_VI[p["role"]], "place": place(p) if p["role"] != "admin_province" else (p["province"] or "Toàn hệ thống"),
            "phone": p["phone"] if adm else None, "online": mins is not None and mins * 60 < ONLINE_SEC, "minutes": mins, "unread": unread, "last": last, "last_at": last_at}


@router.get("/chat/contacts")
def chat_contacts(u=Depends(full)):
    peers = q("SELECT id,full_name,role,phone,province,ward,last_seen FROM users WHERE status='active' AND id<>:me AND (" + REACH[u["role"]] + ") LIMIT 400", reach_args(u))
    agg = {r["peer"]: r for r in q(
        "SELECT peer, MAX(id) mid, SUM(unread) un FROM (SELECT CASE WHEN sender_id=:me THEN receiver_id ELSE sender_id END peer, id, "
        "(receiver_id=:me AND read=0) unread FROM dm WHERE sender_id=:me OR receiver_id=:me) GROUP BY peer", {"me": u["id"]})}
    bodies = {r["id"]: r for r in q(f"SELECT id,body,created_at FROM dm WHERE id IN ({','.join(str(a['mid']) for a in agg.values()) or '0'})")}
    out = []
    for p in peers:
        a = agg.get(p["id"])
        m = bodies.get(a["mid"]) if a else None
        out.append(peer_view(p, int(a["un"]) if a else 0, m and m["body"][:70], m and m["created_at"]))
    out.sort(key=lambda x: x["name"])                              # sắp xếp ổn định: tên, rồi tin mới nhất, rồi tin chưa đọc lên đầu
    out.sort(key=lambda x: x["last_at"] or "", reverse=True)
    out.sort(key=lambda x: -x["unread"])
    return out


def peer_of(u, pid):
    p = q("SELECT id,full_name,role,phone,province,ward,last_seen FROM users WHERE status='active' AND id=:pid AND id<>:me AND (" + REACH[u["role"]] + ")", reach_args(u, pid=pid), one=True)
    if not p:   # vẫn cho xem/nhắn nếu hai bên đã từng nhắn nhau
        p = q("SELECT id,full_name,role,phone,province,ward,last_seen FROM users WHERE status='active' AND id=? AND id IN "
              "(SELECT sender_id FROM dm WHERE receiver_id=? UNION SELECT receiver_id FROM dm WHERE sender_id=?)", (pid, u["id"], u["id"]), one=True)
    if not p:
        raise HTTPException(404, "Không nhắn tin được với người này")
    return p


class Msg(BaseModel):
    body: str = Field(min_length=1, max_length=1000)


@router.get("/chat/{pid}")
def chat_thread(pid: int, u=Depends(full)):
    p = peer_of(u, pid)
    q("UPDATE dm SET read=1 WHERE sender_id=? AND receiver_id=? AND read=0", (pid, u["id"]), write=True)
    rows = q("SELECT (sender_id=?) mine, body, created_at, bot FROM dm WHERE (sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?) ORDER BY id DESC LIMIT 200",
             (u["id"], u["id"], pid, pid, u["id"]))
    return {"peer": peer_view(p), "messages": [{**r, "mine": bool(r["mine"])} for r in rows[::-1]]}


@router.post("/chat/{pid}")
def chat_send(pid: int, b: Msg, bg: BackgroundTasks, u=Depends(full)):
    throttle("chat_send", u["id"], 40, 600)
    p = peer_of(u, pid)
    q("INSERT INTO dm(sender_id,receiver_id,body) VALUES(?,?,?)", (u["id"], pid, b.body.strip()), write=True)
    if u["role"] == "farmer" and p["role"] in ("admin_ward", "admin_province"):
        from . import assistant        # cán bộ vắng thì trợ lý tự trả lời thay (chạy nền)
        bg.add_task(assistant.autoreply, u["id"], pid)
    return {"ok": True}


# ---------- đọc tin nhắn thành giọng nói (dự phòng khi thiết bị chưa có giọng tiếng Việt) ----------
_tts_cache = {}


class Say(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


def split_text(t, n=180):
    """Cắt chữ thành các đoạn <= n ký tự, ưu tiên cắt ở dấu câu hoặc khoảng trắng."""
    out, cur = [], ""
    for w in re.split(r"(?<=[.!?,;:])\s+|\s+", t):
        while len(w) > n:
            out.append(w[:n]); w = w[n:]
        if len(cur) + len(w) + 1 > n and cur:
            out.append(cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    return out + ([cur] if cur else [])


async def tts_edge(text):
    import edge_tts
    last = None
    for _ in range(2):   # thử lại 1 lần vì đôi khi Microsoft trả về rỗng
        try:
            com = edge_tts.Communicate(text, os.getenv("TTS_VOICE") or "vi-VN-HoaiMyNeural")
            data = b"".join([c["data"] async for c in com.stream() if c["type"] == "audio"])
            if data:
                return data
        except Exception as e:
            last = e
    raise RuntimeError(f"edge-tts {getattr(edge_tts, '__version__', '?')}: {last!r}")


async def tts_google(text):
    """Dự phòng: giọng đọc của Google Dịch (không chính thức, hợp với tin ngắn)."""
    out = b""
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://translate.google.com/"}) as cl:
        for part in split_text(text):
            r = await cl.get("https://translate.google.com/translate_tts", params={"ie": "UTF-8", "q": part, "tl": "vi", "client": "tw-ob"})
            r.raise_for_status()
            out += r.content
    return out


@router.post("/tts")
async def tts(b: Say, u=Depends(full)):
    throttle("tts", u["id"], 40, 600)
    """Tạo giọng đọc tiếng Việt: thử edge-tts (giọng Microsoft Edge) trước, lỗi thì dùng giọng Google Dịch. Cả hai đều cần Internet.
    Đổi giọng edge-tts bằng TTS_VOICE trong .env, ví dụ vi-VN-NamMinhNeural."""
    text = re.sub(r"[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f]", "", clean(b.text))[:800].strip()
    if not text:
        raise HTTPException(400, "Tin nhắn chỉ có biểu tượng, không có chữ để đọc")
    key = sha(text)
    if key not in _tts_cache:
        data = None
        # Mặc định chỉ dùng edge-tts. Giọng Google Dịch (không chính thức) gửi nội dung tin nhắn sang Google nên chỉ bật khi đặt TTS_GOOGLE_FALLBACK=1
        for name, fn in (("edge-tts", tts_edge),) + ((("google", tts_google),) if os.getenv("TTS_GOOGLE_FALLBACK") else ()):
            try:
                data = await fn(text)
                if data:
                    break
            except Exception as e:
                print(f"[EcoPulse] Giọng đọc {name} lỗi: {e!r}", flush=True)
        if not data:
            raise HTTPException(502, "Máy chủ không tạo được giọng đọc lúc này")
        if len(_tts_cache) > 100:
            _tts_cache.pop(next(iter(_tts_cache)))
        _tts_cache[key] = data
    return Response(_tts_cache[key], media_type="audio/mpeg")

# ---------- quản lý: người dùng trong khu vực, thông báo, nhật ký ----------
def managed(u, t):
    if u["role"] == "admin_ward":
        return t["role"] == "farmer" and t["province_key"] == u["province_key"] and t["ward_key"] == u["ward_key"]
    return u["role"] == "admin_province" and t["role"] in ("farmer", "admin_ward") and (u["province_key"] is None or t["province_key"] == u["province_key"])


def managed_where(u):
    if u["role"] == "admin_ward":
        return "role='farmer' AND province_key=:pk AND ward_key=:wk"
    return "role IN('farmer','admin_ward') AND (:pk IS NULL OR province_key=:pk)"


@router.get("/admin/users")
def admin_users(search: str = "", u=Depends(need(*ADMINS))):
    s = f"%{fold(search)}%" if search.strip() else "%"
    rows = q(f"SELECT id,full_name,role,status,pending,province,ward,last_seen,created_at FROM users WHERE {managed_where(u)} AND name_key LIKE :s ORDER BY pending DESC, role DESC, id DESC LIMIT 300",
             {"pk": u["province_key"], "wk": u["ward_key"], "s": s})
    return [{**r, "pending": bool(r["pending"]), "role_vi": ROLE_VI[r["role"]], "place": place(r)} for r in rows]


@router.post("/admin/users/{uid}/{action}")
def admin_user(uid: int, action: str, u=Depends(need(*ADMINS))):
    t = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if action not in ("lock", "unlock", "approve", "reject") or not t or t["id"] == u["id"] or not managed(u, t):
        raise HTTPException(400, "Không thực hiện được")
    if action in ("approve", "reject"):      # chỉ quản lý tỉnh duyệt quản lý xã/phường đang chờ
        if u["role"] != "admin_province" or not t["pending"] or t["role"] != "admin_ward":
            raise HTTPException(400, "Không thực hiện được")
        if action == "approve":
            q("UPDATE users SET status='active', pending=0, fails=0, locked_until=NULL WHERE id=?", (uid,), write=True)
            area_for("province", t["province"]); area_for("ward", t["province"], t["ward"])
            audit(u, f"{u['full_name']} duyệt tài khoản quản lý của {t['full_name']} ({place(t)})", about=t)
            notify(uid, "Tài khoản đã được duyệt", "Quản lý tỉnh đã duyệt tài khoản của bạn. Bạn có thể bắt đầu sử dụng.", f"approved:{uid}")
        else:
            audit(u, f"{u['full_name']} từ chối đăng ký quản lý của {t['full_name']} ({place(t)})", about=t)
            try:
                q("DELETE FROM users WHERE id=? AND pending=1", (uid,), write=True)
            except sqlite3.IntegrityError:
                raise HTTPException(400, "Không xóa được đăng ký này")
        return {"ok": True}
    if t["pending"]:
        raise HTTPException(400, "Tài khoản đang chờ duyệt: hãy bấm Duyệt hoặc Từ chối")
    q("UPDATE users SET status=? WHERE id=?", ("locked" if action == "lock" else "active", uid), write=True)
    if action == "lock":
        q("DELETE FROM sessions WHERE user_id=?", (uid,), write=True)
    audit(u, f"{u['full_name']} {'khóa' if action == 'lock' else 'mở khóa'} tài khoản của {t['full_name']} ({ROLE_VI[t['role']].lower()}, {place(t)})", about=t)
    return {"ok": True}


@router.get("/admin/wards")
def admin_wards(u=Depends(need(*ADMINS))):
    rows = q("SELECT ward, ward_key, SUM(role='farmer') farmers FROM users WHERE status='active' AND ward_key IS NOT NULL AND ward_key<>'' AND " + managed_where(u) +
             " GROUP BY ward_key ORDER BY ward", {"pk": u["province_key"], "wk": u["ward_key"]})
    return [{"ward": r["ward"], "key": r["ward_key"], "farmers": int(r["farmers"] or 0)} for r in rows]


class Ann(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=1000)
    ward_key: str = Field(default="", max_length=60)


@router.post("/admin/announce")
def admin_announce(b: Ann, u=Depends(need(*ADMINS))):
    throttle("announce", u["id"], 10, 3600, "Bạn gửi thông báo quá nhiều, hãy thử lại sau")
    """Gửi thông báo theo vị trí: quản lý xã/phường chỉ gửi cho nông dân trong xã/phường mình;
    quản lý tỉnh gửi cho cả tỉnh (nông dân + quản lý xã/phường) hoặc chọn một xã/phường."""
    if u["role"] == "admin_ward":
        rows = q("SELECT id FROM users WHERE status='active' AND role='farmer' AND province_key=? AND ward_key=?", (u["province_key"], u["ward_key"]))
        where = place(u)
    else:
        wk = b.ward_key.strip()
        rows = q("SELECT id FROM users WHERE status='active' AND id<>:me AND role IN('farmer','admin_ward') AND (:pk IS NULL OR province_key=:pk) AND (:wk='' OR ward_key=:wk)",
                 {"me": u["id"], "pk": u["province_key"], "wk": wk})
        w = wk and q("SELECT ward FROM users WHERE ward_key=? LIMIT 1", (wk,), one=True)
        where = (w["ward"] + ", " if w else "") + (u["province"] or "toàn hệ thống")
    if not rows:
        raise HTTPException(400, "Chưa có ai trong khu vực này để nhận thông báo")
    batch = secrets.token_hex(4)
    for r in rows:
        notify(r["id"], b.title.strip(), b.body.strip(), f"adm:{batch}:{r['id']}")
    audit(u, f"{u['full_name']} gửi thông báo \"{b.title.strip()}\" tới {len(rows)} người ở {where}")
    return {"sent": len(rows)}


@router.get("/admin/sent")
def admin_sent(u=Depends(need(*ADMINS))):
    rows = q("SELECT at, detail FROM audit_log WHERE actor_id=? AND detail LIKE '%gửi thông báo%' ORDER BY id DESC LIMIT 30", (u["id"],))
    return [{"at": r["at"].replace(" ", "T") + "Z", "text": r["detail"]} for r in rows]


LEGACY = {"user.register": "đăng ký tài khoản", "user.lock": "khóa một tài khoản", "user.unlock": "mở khóa một tài khoản", "coop.apply": "gửi hồ sơ hợp tác xã",
          "coop.approve": "duyệt hồ sơ hợp tác xã", "coop.reject": "từ chối hồ sơ hợp tác xã", "coop.join": "tham gia hợp tác xã", "coop.leave": "rời hợp tác xã",
          "coop.announce": "gửi thông báo cho thành viên", "admin.announce": "gửi thông báo toàn hệ thống"}


@router.get("/admin/audit")
def admin_audit(u=Depends(need(*ADMINS))):
    if u["role"] == "admin_ward":
        where, a = "a.province_key=:pk AND a.ward_key=:wk", {"pk": u["province_key"], "wk": u["ward_key"]}
    elif u["province_key"]:
        where, a = "a.province_key=:pk", {"pk": u["province_key"]}
    else:
        where, a = "1=1", {}
    rows = q(f"SELECT a.at, a.action, a.detail, us.full_name actor, us.role FROM audit_log a LEFT JOIN users us ON us.id=a.actor_id WHERE {where} ORDER BY a.id DESC LIMIT 200", a)
    return [{"at": r["at"].replace(" ", "T") + "Z", "who": r["actor"] or "Hệ thống", "role": ROLE_VI.get(r["role"], ""),
             "text": r["detail"] if r["action"] == "log" else (LEGACY.get(r["action"], "thực hiện một thao tác") + (f" ({r['detail']})" if r["detail"] else ""))} for r in rows]


# ---------- tổng quan, kiến thức ----------
def dist_of(rows):
    n = [0] * len(render.SOIL_CLS)
    for r in rows:
        if r["status"]:
            n[r["status"]["idx"]] += 1
    return [{"name": c["name"], "color": c["color"], "n": n[i]} for i, c in enumerate(render.SOIL_CLS)]


def last_scene():
    ts = [m.group(1) for m in (SCENE_T.search(p.name) for p in render.CACHE.glob("*__soil.json")) if m]
    return datetime.strptime(max(ts), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).isoformat() if ts else None


@router.get("/overview")
def overview(u=Depends(full)):
    base = {"role": u["role"], "last": last_scene(), "latest": q("SELECT title,body FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 3", (u["id"],)),
            "codes": my_area(u), "place": place(u), "need_location": u["role"] == "farmer" and not u["ward_key"]}
    if u["role"] == "farmer":
        rows = [plot_row(p) for p in q("SELECT * FROM plots WHERE owner_id=? AND deleted=0", (u["id"],))]
    else:   # quản lý chỉ xem số liệu tổng hợp, không xem từng thửa của nông dân
        w = "p.owner_id IN (SELECT id FROM users WHERE role='farmer' AND (:pk IS NULL OR province_key=:pk)" + (" AND ward_key=:wk" if u["role"] == "admin_ward" else "") + ")"
        rows = [plot_row(p) for p in q(f"SELECT p.* FROM plots p WHERE p.deleted=0 AND {w} LIMIT 600", {"pk": u["province_key"], "wk": u["ward_key"]} if u["role"] == "admin_ward" else {"pk": u["province_key"]})]
        mw = managed_where(u)
        args = {"pk": u["province_key"], "wk": u["ward_key"]}
        base["farmers"] = q(f"SELECT COUNT(*) n FROM users WHERE role='farmer' AND status='active' AND {mw}", args, one=True)["n"]
        if u["role"] == "admin_province":
            base["ward_admins"] = q("SELECT COUNT(*) n FROM users WHERE role='admin_ward' AND status='active' AND (:pk IS NULL OR province_key=:pk)", {"pk": u["province_key"]}, one=True)["n"]
            base["by_ward"] = q("SELECT ward, SUM(role='farmer') farmers, SUM(role='admin_ward') admins FROM users WHERE status='active' AND ward_key<>'' AND ward_key IS NOT NULL AND "
                                "role IN('farmer','admin_ward') AND (:pk IS NULL OR province_key=:pk) GROUP BY province_key, ward_key ORDER BY farmers DESC LIMIT 100", {"pk": u["province_key"]})
        base["scenes"] = len(list(render.CACHE.glob("*__soil.json")))
    base.update(plots=len(rows), dist=dist_of(rows), attention=sum(1 for r in rows if r["status"] and r["status"]["idx"] in ALERT_BANDS))
    return base


@router.get("/knowledge")
def knowledge(u=Depends(full)):
    return [{k: c[k] for k in ("name", "color", "action", "state", "todo", "tip")} for c in render.SOIL_CLS]



# ---------- ảnh thửa (nông dân tự tải lên) + lịch sử môi trường ----------
import io
from fastapi.responses import FileResponse
import warnings
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 50_000_000                              # ảnh quá nhiều điểm ảnh bị từ chối (chống "bom giải nén")
warnings.simplefilter("error", Image.DecompressionBombWarning)
PHOTO_DIR = DB.parent / "plot_photos"      # data/plot_photos/<id>.jpg, nhớ sao lưu cùng file .db
PHOTO_DIR.mkdir(parents=True, exist_ok=True)
MAX_PHOTO = 8 * 1024 * 1024


async def read_limited(req, limit=MAX_PHOTO):
    """Đọc thân yêu cầu theo từng phần và dừng ngay khi vượt trần (không nạp cả file khổng lồ vào RAM)."""
    cl = req.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > limit:
        raise HTTPException(413, "Ảnh quá lớn (tối đa 8 MB)")
    buf = bytearray()
    async for chunk in req.stream():
        buf += chunk
        if len(buf) > limit:
            raise HTTPException(413, "Ảnh quá lớn (tối đa 8 MB)")
    if not buf:
        raise HTTPException(400, "Chưa nhận được ảnh")
    return bytes(buf)


def photo_url(pid):
    f = PHOTO_DIR / f"{pid}.jpg"
    return f"/plots/{pid}/photo?v={int(f.stat().st_mtime)}" if f.exists() else None


def own_plot(pid, u):
    p = q("SELECT * FROM plots WHERE id=? AND owner_id=? AND deleted=0", (pid, u["id"]), one=True)
    if not p:
        raise HTTPException(404, "Không tìm thấy thửa này")
    return p


@router.post("/plots/{pid}/photo")
async def put_photo(pid: int, req: Request, u=Depends(full)):
    """Nhận thân yêu cầu là chính file ảnh (không cần thư viện multipart). Ảnh được xoay đúng chiều, thu về tối đa 1600 px, lưu JPEG."""
    p = own_plot(pid, u)
    throttle("photo", u["id"], 30, 3600)
    raw = await read_limited(req)
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
        im.thumbnail((1600, 1600))
    except Exception:
        raise HTTPException(400, "Tệp không phải ảnh hợp lệ. Hãy dùng ảnh JPG, PNG hoặc WebP")
    tmp = PHOTO_DIR / f"{pid}.tmp"
    im.save(tmp, "JPEG", quality=85)
    tmp.replace(PHOTO_DIR / f"{pid}.jpg")
    audit(u, f"{u['full_name']} cập nhật ảnh thửa \"{p['name']}\"")
    return {"photo": photo_url(pid)}


@router.get("/plots/{pid}/photo")
def get_photo(pid: int, u=Depends(user)):
    p = q("SELECT * FROM plots WHERE id=? AND deleted=0", (pid,), one=True)
    f = PHOTO_DIR / f"{pid}.jpg"
    if not p or not f.exists():
        raise HTTPException(404, "Chưa có ảnh")
    if p["owner_id"] != u["id"]:      # chủ thửa, hoặc cán bộ quản lý đúng khu vực
        owner = q("SELECT * FROM users WHERE id=?", (p["owner_id"],), one=True)
        if u["level"] != "full" or not (owner and managed(u, owner)):
            raise HTTPException(404, "Chưa có ảnh")      # cùng thông báo với "không có ảnh" để không lộ thửa nào có ảnh
    return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.delete("/plots/{pid}/photo")
def del_photo(pid: int, u=Depends(full)):
    own_plot(pid, u)
    (PHOTO_DIR / f"{pid}.jpg").unlink(missing_ok=True)
    return {"ok": True}


_hist = {}


@router.get("/plots/{pid}/history")
def plot_history(pid: int, u=Depends(user)):
    """7 ngày gần nhất: nhiệt độ + mưa (Open-Meteo, dữ liệu mô hình) và độ ẩm đất từ các ảnh NISAR đã xử lý."""
    p = own_plot(pid, u)
    key = (round(p["lat"], 2), round(p["lon"], 2))
    hit = _hist.get(key)
    if not hit or time.time() - hit[0] > 30 * 60:
        days = []
        try:
            r = httpx.get("https://api.open-meteo.com/v1/forecast", timeout=15, params={
                "latitude": key[0], "longitude": key[1], "timezone": "Asia/Bangkok", "past_days": 7, "forecast_days": 1,
                "daily": "temperature_2m_mean,temperature_2m_max,temperature_2m_min,precipitation_sum"})
            r.raise_for_status()
            d = r.json().get("daily") or {}
            days = [{"date": d["time"][i], "tmean": d["temperature_2m_mean"][i], "tmax": d["temperature_2m_max"][i],
                     "tmin": d["temperature_2m_min"][i], "rain": d["precipitation_sum"][i] or 0}
                    for i in range(len(d.get("time") or []))][:7]       # bỏ hôm nay (chưa trọn ngày)
            _hist[key] = (time.time(), days)
        except Exception:
            days = hit[1] if hit else []
    else:
        days = hit[1]
    soil = []
    for j in render.CACHE.glob("*__soil.json"):
        m = SCENE_T.search(j.name)
        try:
            v = render.value(j.stem, p["lon"], p["lat"]) if m else None
        except Exception:
            continue
        if v is not None:
            soil.append({"t": datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).isoformat(), "v": v})
    soil.sort(key=lambda x: x["t"])
    return {"days": days, "soil": soil[-12:]}



# ---------- tin tức / kiến thức: cán bộ soạn, tỉnh kiểm duyệt, nông dân đọc ----------
from html.parser import HTMLParser
from html import escape as _esc

q("""CREATE TABLE IF NOT EXISTS news(id INTEGER PRIMARY KEY, author_id INTEGER NOT NULL REFERENCES users(id), province_key TEXT, ward_key TEXT,
  title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '', crop TEXT NOT NULL DEFAULT '', cover TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'local', status TEXT NOT NULL DEFAULT 'draft',
  review_note TEXT NOT NULL DEFAULT '', deleted INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP, published_at TEXT)""", write=True)
NEWS_IMG = DB.parent / "news_img"          # data/news_img/<16 ký tự hex>.jpg
NEWS_IMG.mkdir(parents=True, exist_ok=True)
IMG_URL = re.compile(r"^/news-img/[a-f0-9]{16}\.jpg$")
NEWS_CATS = {"Độ ẩm đất", "Cây trồng", "Thời tiết", "Hạn hán", "Biến đổi khí hậu", "Viễn thám", "Nông nghiệp"}   # khớp danh sách CATS trong app.html
NEWS_CROPS = {"", "Cà phê", "Sầu riêng", "Cao su", "Hồ tiêu"}
_OK_TAGS = {"p", "br", "b", "strong", "i", "em", "h3", "ul", "ol", "li", "a", "img", "blockquote", "div"}


class _San(HTMLParser):
    """Chỉ giữ thẻ an toàn; bỏ mọi thuộc tính trừ href (http/https hoặc đường dẫn nội bộ) và src ảnh do hệ thống lưu."""
    def __init__(s):
        super().__init__(convert_charrefs=True); s.o, s.open = [], []

    def handle_starttag(s, t, a):
        if t not in _OK_TAGS:
            return
        at = dict(a)
        if t == "img":
            src = at.get("src") or ""
            if IMG_URL.match(src):
                s.o.append(f'<img src="{src}" alt="">')
        elif t == "br":
            s.o.append("<br>")
        else:
            h = (at.get("href") or "").strip()
            s.o.append(f'<a href="{_esc(h)}" target="_blank" rel="noopener noreferrer">' if t == "a" and re.match(r"^(https?://|/(?![/\\]))", h) else f"<{t}>")
            s.open.append(t)

    def handle_endtag(s, t):
        if t in s.open:
            while s.open:
                x = s.open.pop(); s.o.append(f"</{x}>")
                if x == t:
                    break

    def handle_data(s, d):
        s.o.append(_esc(d, quote=False))


def clean_html(h):
    p = _San(); p.feed(h or ""); p.close()
    while p.open:
        p.o.append(f"</{p.open.pop()}>")
    return "".join(p.o)[:100000]


def jpeg_bytes(raw):
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
        im.thumbnail((1600, 1600))
        return im
    except Exception:
        raise HTTPException(400, "Tệp không phải ảnh hợp lệ. Hãy dùng ảnh JPG, PNG hoặc WebP")


class NewsIn(BaseModel):
    title: str = Field(min_length=1, max_length=150)
    summary: str = Field(default="", max_length=300)
    category: str = Field(default="", max_length=40)
    crop: str = Field(default="", max_length=40)
    cover: str = Field(default="", max_length=80)
    body: str = Field(default="", max_length=200000)
    source: str = Field(default="", max_length=2000)
    scope: str = Field(default="local", max_length=10)


class NewsAct(BaseModel):
    action: str = Field(max_length=20)
    note: str = Field(default="", max_length=300)


NEWS_SEL = ("SELECT n.*, a.full_name AS a_name, a.province_key AS a_pk, a.ward AS a_ward, a.province AS a_prov "
            "FROM news n JOIN users a ON a.id=n.author_id")


def news_min(body):
    return max(1, round(len(re.sub(r"<[^>]+>", " ", body or "").split()) / 200))


def news_mgd(u, n):
    """Cán bộ tỉnh quản lý các bài của xã/phường trong tỉnh mình."""
    return u["role"] == "admin_province" and (u["province_key"] is None or n["a_pk"] == u["province_key"])


def news_can(u, n):
    """Chỉ cán bộ xã/phường soạn, sửa, xóa bài của chính mình. Cán bộ tỉnh chỉ duyệt, từ chối, gỡ bài đã gửi lên."""
    own = n["author_id"] == u["id"] and u["role"] == "admin_ward"
    mgd = news_mgd(u, n)
    c = []
    if own:
        c += ["edit", "delete"]
        if n["status"] == "draft":
            c.append("submit")
    if mgd and n["status"] == "pending":
        c += ["publish", "reject"]
    if (own or mgd) and n["status"] == "published":
        c.append("unpublish")
    return c


def news_see(u, n):
    return n["author_id"] == u["id"] or (news_mgd(u, n) and n["status"] != "draft")


def news_get(nid, u, admin=True):
    n = q(NEWS_SEL + " WHERE n.id=? AND n.deleted=0", (nid,), one=True)
    if not n or (admin and not news_see(u, n)):
        raise HTTPException(404, "Không tìm thấy bài viết")
    return n


def news_pub(n, body=False):
    r = {"id": n["id"], "title": n["title"], "summary": n["summary"], "category": n["category"] if n["category"] in NEWS_CATS else "", "crop": n["crop"], "cover": n["cover"],
         "scope": n["scope"], "min": news_min(n["body"]), "place": ", ".join(x for x in (n["a_ward"], n["a_prov"]) if x), "published_at": n["published_at"]}
    if body:
        r.update(body=n["body"], source=n["source"])
    return r


def news_vals(b, u):
    if b.cover and not IMG_URL.match(b.cover):
        raise HTTPException(400, "Ảnh bìa không hợp lệ")
    cat, crop = b.category.strip(), b.crop.strip()
    if cat and cat not in NEWS_CATS:
        raise HTTPException(400, "Chuyên mục không hợp lệ")
    if crop not in NEWS_CROPS:
        raise HTTPException(400, "Loại cây không hợp lệ")
    scope = "local"
    return (b.title.strip(), b.summary.strip(), cat, crop, b.cover, clean_html(b.body), b.source.strip(), scope)


VISIBLE = "n.deleted=0 AND n.status='published' AND (n.scope='system' OR (n.province_key IS NOT NULL AND n.province_key=:pk AND (n.ward_key IS NULL OR n.ward_key=:wk)))"


@router.get("/news")
def news_list(u=Depends(full)):
    return [news_pub(n) for n in q(NEWS_SEL + f" WHERE {VISIBLE} ORDER BY n.published_at DESC LIMIT 100", {"pk": u.get("province_key"), "wk": u.get("ward_key")})]


@router.get("/news/{nid}")
def news_read(nid: int, u=Depends(full)):
    n = q(NEWS_SEL + f" WHERE n.id=:id AND {VISIBLE}", {"id": nid, "pk": u.get("province_key"), "wk": u.get("ward_key")}, one=True)
    if not n:
        raise HTTPException(404, "Không tìm thấy bài viết")
    return news_pub(n, True)


@router.get("/admin/news")
def admin_news(u=Depends(need(*ADMINS))):
    w = "n.author_id=:uid" if u["role"] == "admin_ward" else "(a.role='admin_ward' AND n.status!='draft' AND (:pk IS NULL OR a.province_key=:pk))"
    return [{"id": n["id"], "title": n["title"], "category": n["category"], "crop": n["crop"], "scope": n["scope"], "status": n["status"],
             "review_note": n["review_note"], "author": n["a_name"], "can": news_can(u, n),
             "place": ", ".join(x for x in (n["a_ward"], n["a_prov"]) if x), "updated_at": n["updated_at"]}
            for n in q(NEWS_SEL + f" WHERE n.deleted=0 AND {w} ORDER BY n.updated_at DESC", {"uid": u["id"], "pk": u["province_key"]})]


@router.get("/admin/news/{nid}")
def admin_news_one(nid: int, u=Depends(need(*ADMINS))):
    n = news_get(nid, u)
    r = {k: n[k] for k in ("id", "title", "summary", "category", "crop", "cover", "body", "source", "scope", "status", "review_note")}
    r.update(author=n["a_name"], place=", ".join(x for x in (n["a_ward"], n["a_prov"]) if x), can=news_can(u, n))
    return r


@router.post("/admin/news")
def admin_news_add(b: NewsIn, u=Depends(need("admin_ward"))):
    nid = q("INSERT INTO news(author_id,province_key,ward_key,title,summary,category,crop,cover,body,source,scope) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (u["id"], u.get("province_key"), u.get("ward_key") if u["role"] == "admin_ward" else None) + news_vals(b, u), write=True)
    audit(u, f"{u['full_name']} soạn bài viết \"{b.title.strip()}\"")
    return {"id": nid}


@router.put("/admin/news/{nid}")
def admin_news_edit(nid: int, b: NewsIn, u=Depends(need("admin_ward"))):
    n = news_get(nid, u)
    if "edit" not in news_can(u, n):
        raise HTTPException(403, "Không đủ quyền sửa bài này")
    st = "draft"      # sửa xong phải gửi duyệt lại
    q("UPDATE news SET title=?,summary=?,category=?,crop=?,cover=?,body=?,source=?,scope=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
      news_vals(b, u) + (st, nid), write=True)
    audit(u, f"{u['full_name']} sửa bài viết \"{b.title.strip()}\"")
    return {"id": nid}


@router.post("/admin/news/{nid}/status")
def admin_news_status(nid: int, b: NewsAct, u=Depends(need(*ADMINS))):
    n = news_get(nid, u)
    a = b.action
    if a not in news_can(u, n) or a in ("edit", "delete"):
        raise HTTPException(403, "Không thực hiện được thao tác này")
    if a in ("submit", "publish") and not (n["title"].strip() and (re.sub(r"<[^>]+>|\s", "", n["body"]) or "<img" in n["body"])):
        raise HTTPException(400, "Bài viết chưa có nội dung")
    new = {"submit": "pending", "publish": "published", "reject": "draft", "unpublish": "draft"}[a]
    q("UPDATE news SET status=?,review_note=?,updated_at=CURRENT_TIMESTAMP,published_at=CASE WHEN ?='published' THEN CURRENT_TIMESTAMP ELSE published_at END WHERE id=?",
      (new, b.note.strip() if a == "reject" else "", new, nid), write=True)
    if a == "submit":      # báo cho cán bộ tỉnh biết có bài chờ duyệt
        for p in q("SELECT id FROM users WHERE role='admin_province' AND status='active' AND (province_key IS NULL OR province_key=?)", (n["a_pk"],)):
            notify(p["id"], "Có bài viết chờ duyệt", f"{u['full_name']}: {n['title']}", f"news:{nid}:sub:{time.time()}")
    if n["author_id"] != u["id"] and a in ("publish", "reject"):
        notify(n["author_id"], "Bài viết đã được duyệt" if a == "publish" else "Bài viết bị từ chối", n["title"] + (f". Lý do: {b.note.strip()}" if a == "reject" and b.note.strip() else ""), f"news:{nid}:{a}:{time.time()}")
    audit(u, f"{u['full_name']} {dict(submit='gửi duyệt', publish='xuất bản', reject='từ chối', unpublish='gỡ xuống')[a]} bài viết \"{n['title']}\"")
    return {"status": new}


@router.delete("/admin/news/{nid}")
def admin_news_del(nid: int, u=Depends(need("admin_ward"))):
    n = news_get(nid, u)
    if "delete" not in news_can(u, n):
        raise HTTPException(403, "Không đủ quyền xóa bài này")
    q("UPDATE news SET deleted=1 WHERE id=?", (nid,), write=True)
    audit(u, f"{u['full_name']} xóa bài viết \"{n['title']}\"")
    return {"ok": True}


@router.post("/admin/news/image")
async def admin_news_image(req: Request, u=Depends(need("admin_ward"))):
    throttle("news_img", u["id"], 60, 3600)
    raw = await read_limited(req)
    name = secrets.token_hex(8)
    jpeg_bytes(raw).save(NEWS_IMG / f"{name}.jpg", "JPEG", quality=85)
    return {"url": f"/news-img/{name}.jpg"}


@router.get("/news-img/{name}")
def news_img(name: str, u=Depends(full)):
    f = NEWS_IMG / name
    if not re.match(r"^[a-f0-9]{16}\.jpg$", name) or not f.exists():
        raise HTTPException(404, "Không có ảnh")
    return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=31536000, immutable"})


try:      # đánh dấu tin do trợ lý tự động gửi
    q("ALTER TABLE dm ADD COLUMN bot INTEGER NOT NULL DEFAULT 0", write=True)
except Exception:
    pass
