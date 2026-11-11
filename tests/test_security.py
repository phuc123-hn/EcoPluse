"""Kiểm thử các bản vá bảo mật. Chạy:  python -m pytest tests -q
Dùng DB và thư mục cache tạm, không đụng dữ liệu thật."""
import importlib
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def env(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("HN_DB", str(Path(d) / "t.db"))
    monkeypatch.setenv("CACHE_DIR", str(Path(d) / "cache"))
    # Đặt rỗng (không xóa) để load_dotenv() không nạp lại giá trị từ .env thật của bạn: test phải độc lập với .env
    for k in ("PRODUCTION", "COOKIE_SECURE", "HN_ADMIN_EMAIL", "HN_ADMIN_PHONE", "HN_ADMIN_PASSWORD", "SMTP_HOST", "RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID"):
        monkeypatch.setenv(k, "")
    monkeypatch.setenv("TRUSTED_PROXIES", "0")
    for m in [m for m in sys.modules if m == "backend" or m.startswith("backend.")]:
        del sys.modules[m]
    main = importlib.import_module("backend.main")
    hydro = importlib.import_module("backend.hydro")
    render = importlib.import_module("backend.render")
    mails = []
    monkeypatch.setattr(hydro, "send_mail", lambda to, subject, text, html=None, logo=None: mails.append(text) or False)
    from fastapi.testclient import TestClient

    def client():
        return TestClient(main.app)

    return type("E", (), dict(main=main, hydro=hydro, render=render, mails=mails, client=staticmethod(client)))


def add_user(h, name, role, province="Gia Lai", ward="Pleiku", email=None, pw="matkhau123", pending=0):
    return h.q("INSERT INTO users(full_name,name_key,email,pw_hash,role,province,province_key,ward,ward_key,status,pending) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (name, h.fold(name), email, h.hash_pw(pw), role, province, h.province_key(province), ward, h.ward_key(ward),
                "locked" if pending else "active", pending), write=True)


def login(c, ident, pw="matkhau123"):
    r = c.post("/auth/login", json={"ident": ident, "password": pw})
    assert r.status_code == 200, r.text
    return c


# ---------- 1. Quên mật khẩu = phiên hạn chế ----------
def test_forgot_is_limited_and_upgrade(env):
    add_user(env.hydro, "Nguyen Van A", "farmer")
    c = env.client()
    assert c.post("/auth/forgot", json={"name": "Nguyen Van A"}).json()["limited"] is True
    assert c.get("/me").json()["limited"] is True
    assert c.get("/plots").status_code == 200                          # xem thửa được
    for method, path, body in [("get", "/chat/contacts", None), ("get", "/news", None), ("get", "/notifications", None),
                               ("get", "/overview", None), ("get", "/knowledge", None), ("post", "/plots", {"name": "x", "lon": 108, "lat": 14}),
                               ("post", "/ai/chat", {"messages": [{"role": "user", "content": "hi"}]}), ("post", "/tts", {"text": "xin chao"}),
                               ("get", "/api/scenes", None)]:
        r = getattr(c, method)(path, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (path, r.status_code, r.text[:100])
    assert c.post("/auth/upgrade", json={"password": "sai-mat-khau"}).status_code == 401
    assert c.post("/auth/upgrade", json={"password": "matkhau123"}).status_code == 200
    assert c.get("/me").json()["limited"] is False
    assert c.get("/chat/contacts").status_code == 200


def test_real_login_revokes_limited_sessions(env):
    add_user(env.hydro, "Tran Thi B", "farmer")
    attacker = env.client()
    attacker.post("/auth/forgot", json={"name": "Tran Thi B"})
    assert attacker.get("/me").status_code == 200
    login(env.client(), "Tran Thi B")
    assert attacker.get("/me").status_code == 401                      # phiên "quên mật khẩu" bị thu hồi


def test_limited_cannot_read_other_users_photo_or_plots(env):
    h = env.hydro
    a = add_user(h, "Nong Dan A", "farmer"); b = add_user(h, "Nong Dan B", "farmer")
    pb = h.q("INSERT INTO plots(owner_id,name,crop,lon,lat) VALUES(?,?,?,?,?)", (b, "thua B", "ca phe", 108.0, 14.0), write=True)
    c = env.client(); c.post("/auth/forgot", json={"name": "Nong Dan A"})
    assert c.get(f"/plots/{pb}").status_code in (403, 404)
    assert c.get(f"/plots/{pb}/photo").status_code == 404


# ---------- 2. Quản lý xã/phường phải được tỉnh duyệt ----------
def register_ward_admin(env, email="xa@example.com"):
    c = env.client()
    body = {"full_name": "Le Van Xa", "phone": "0901234567", "email": email, "province": "Gia Lai", "ward": "Pleiku", "password": "matkhau123"}
    assert c.post("/auth/admin/start", json=body).status_code == 200
    code = re.search(r"(\d{6})", env.mails[-1]).group(1)
    r = c.post("/auth/admin/verify", json={"email": email, "code": code})
    assert r.status_code == 200 and r.json()["pending"] is True
    return c


def test_ward_admin_needs_province_approval(env):
    h = env.hydro
    prov = add_user(h, "Quan Ly Tinh", "admin_province", email="tinh@example.com")
    c = register_ward_admin(env)
    assert c.get("/me").status_code == 401                              # chưa có phiên
    r = c.post("/auth/login", json={"ident": "xa@example.com", "password": "matkhau123"})
    assert r.status_code == 403 and "chờ" in r.json()["detail"]
    assert c.post("/auth/login", json={"ident": "xa@example.com", "password": "sai"}).status_code == 401   # không lộ trạng thái khi sai mật khẩu
    # quản lý tỉnh nhận thông báo, thấy người chờ duyệt và duyệt
    p = env.client(); login(p, "tinh@example.com")
    assert p.get("/me").json()["pending_admins"] == 1
    users = p.get("/admin/users").json()
    wid = next(u["id"] for u in users if u["pending"])
    assert p.post(f"/admin/users/{wid}/unlock").status_code == 400      # không "mở khóa" để lách duyệt
    assert p.post(f"/admin/users/{wid}/approve").status_code == 200
    login(c, "xa@example.com")
    assert c.get("/me").json()["role"] == "admin_ward"


def test_ward_admin_cannot_approve_and_reject_deletes(env):
    h = env.hydro
    add_user(h, "Quan Ly Tinh", "admin_province", email="tinh@example.com")
    add_user(h, "Quan Ly Xa Cu", "admin_ward", email="cu@example.com")
    register_ward_admin(env)
    wid = h.q("SELECT id FROM users WHERE pending=1", one=True)["id"]
    w = env.client(); login(w, "cu@example.com")
    assert w.post(f"/admin/users/{wid}/approve").status_code in (400, 403)
    p = env.client(); login(p, "tinh@example.com")
    assert p.post(f"/admin/users/{wid}/reject").status_code == 200
    assert h.q("SELECT 1 FROM users WHERE id=?", (wid,), one=True) is None


def test_ward_admin_registration_needs_a_province_admin(env):
    c = env.client()
    body = {"full_name": "Le Van Xa", "phone": "0901234567", "email": "xa@example.com", "province": "Gia Lai", "ward": "Pleiku", "password": "matkhau123"}
    assert c.post("/auth/admin/start", json=body).status_code == 400


# ---------- 3. XSS ----------
def test_news_category_is_allowlisted(env):
    h = env.hydro
    add_user(h, "Quan Ly Xa", "admin_ward", email="xa@example.com")
    c = env.client(); login(c, "xa@example.com")
    evil = {"title": "t", "category": 'x" onmouseover="alert(1)', "crop": "", "body": "<p>ok</p>"}
    assert c.post("/admin/news", json=evil).status_code == 400
    assert c.post("/admin/news", json={**evil, "category": "Thời tiết", "crop": "<b>"}).status_code == 400
    assert c.post("/admin/news", json={**evil, "category": "Thời tiết"}).status_code == 200


def test_clean_html_blocks_dangerous_links(env):
    clean = env.hydro.clean_html
    for bad in ['<a href="javascript:alert(1)">x</a>', '<a href="//evil.com">x</a>', '<a href="/\\evil.com">x</a>',
                '<img src=x onerror=alert(1)>', "<script>alert(1)</script>", '<a href="data:text/html;base64,AAA">x</a>']:
        out = clean(bad)
        assert "javascript:" not in out and "onerror" not in out and "<script" not in out and "//evil" not in out and "\\evil" not in out, out
    assert 'href="https://ok.vn/a"' in clean('<a href="https://ok.vn/a">x</a>')
    assert 'href="/map"' in clean('<a href="/map">x</a>')


def test_app_html_escapes_topic_attribute():
    html = (ROOT / "static" / "app.html").read_text(encoding="utf-8")
    assert 'data-topic="${t}"' not in html and 'data-topic="${esc(t)}"' in html


# ---------- 4. render / value / tiles ----------
def test_value_rejects_path_traversal(env):
    h = env.hydro; add_user(h, "Nong Dan", "farmer")
    c = env.client(); login(c, "Nong Dan")
    for bad in ["../x", "..%2F..%2Fetc", "a/b", "a b", "é"]:
        assert c.get("/api/value", params={"id": bad, "layer": "sm", "lng": 108, "lat": 14}).status_code == 400, bad
    layer = next(iter(env.render.LAYERS))
    assert c.get("/api/value", params={"id": "scene_1", "layer": "../../x", "lng": 108, "lat": 14}).status_code == 400
    assert c.get("/api/value", params={"id": "scene_1", "layer": layer, "lng": 108, "lat": 14}).status_code == 200


def test_render_guards(env, monkeypatch):
    h, m, r = env.hydro, env.main, env.render
    add_user(h, "Nong Dan", "farmer"); add_user(h, "Quan Ly Xa", "admin_ward", email="xa@example.com")
    started = []
    monkeypatch.setattr(r, "start", lambda fid, url, layer, retry=False: started.append(fid) or {"status": "running"})
    layer = next(iter(r.LAYERS)); ok_url = "https://nisar.asf.earthdatacloud.nasa.gov/x/y.h5"
    f = env.client(); login(f, "Nong Dan")
    assert f.get("/api/render", params={"id": "s1", "url": ok_url, "layer": layer}).status_code == 403       # nông dân không bắt đầu cảnh mới
    a = env.client(); login(a, "xa@example.com")
    for bad in ["https://evil.com/x.h5", "https://nisar.asf.earthdatacloud.nasa.gov.evil.com/x.h5", "http://nisar.asf.earthdatacloud.nasa.gov/x.h5",
                "https://nisar.asf.earthdatacloud.nasa.gov@evil.com/x.h5", "https://user:pw@nisar.asf.earthdatacloud.nasa.gov/x.h5"]:
        assert a.get("/api/render", params={"id": "s1", "url": bad, "layer": layer}).status_code == 400, bad
    assert a.get("/api/render", params={"id": "s1", "url": ok_url, "layer": layer}).status_code == 200
    r.JOBS["a__" + layer] = {"status": "running"}; r.JOBS["b__" + layer] = {"status": "running"}
    assert a.get("/api/render", params={"id": "s2", "url": ok_url, "layer": layer}).status_code == 429       # đã đủ 2 cảnh đang chạy
    assert started == ["s1"]


def test_token_not_sent_to_lookalike_host(env):
    import requests
    s = env.render._Session() if hasattr(env.render, "_Session") else None
    if s is None:
        pytest.skip("không có _Session")
    for host, keep in [("nisar.asf.earthdatacloud.nasa.gov", True), ("urs.earthdata.nasa.gov", True), ("evilnasa.gov", False), ("nasa.gov.evil.com", False)]:
        prep = requests.Request("GET", f"https://{host}/x", headers={"Authorization": "Bearer T"}).prepare()
        resp = requests.Request("GET", "https://nisar.asf.earthdatacloud.nasa.gov/a").prepare()
        s.rebuild_auth(prep, resp)
        assert ("Authorization" in prep.headers) == keep, host


def test_tiles_only_png(env):
    c = env.client(); cache = env.render.CACHE
    (cache / "a_b.png").write_bytes(b"\x89PNG\r\n\x1a\n"); (cache / "x.json").write_text("{}"); (cache / "manual" / "f.h5").write_bytes(b"h5")
    assert c.get("/tiles/a_b.png").status_code == 200
    assert c.get("/tiles/x.json").status_code == 404
    assert c.get("/tiles/manual/f.h5").status_code == 404
    assert c.get("/tiles/..%2Fhydronisar.db").status_code == 404


# ---------- 5. giới hạn tần suất ----------
def test_login_is_rate_limited_per_ip_and_name(env):
    add_user(env.hydro, "Nong Dan", "farmer")
    c = env.client()
    codes = [c.post("/auth/login", json={"ident": "Nong Dan", "password": "sai"}).status_code for _ in range(10)]
    assert codes[:8] == [401] * 8 and codes[8] == 429
    other = env.client()
    assert other.post("/auth/login", json={"ident": "Nong Dan", "password": "matkhau123"}).status_code == 429   # cùng IP + cùng tên vẫn bị chặn tạm


def test_forgot_and_namefree_throttled(env):
    add_user(env.hydro, "Nong Dan", "farmer")
    c = env.client()
    assert [c.post("/auth/forgot", json={"name": "Nong Dan"}).status_code for _ in range(8)][-1] == 429
    c2 = env.client()
    assert [c2.get("/auth/name-free", params={"name": f"ten {i}"}).status_code for i in range(62)][-1] == 429


def test_client_ip_uses_trusted_proxy_hops(env, monkeypatch):
    h = env.hydro
    class R:  # giả Request
        def __init__(s, xff): s.headers = {"x-forwarded-for": xff} if xff else {}; s.client = type("C", (), {"host": "9.9.9.9"})()
    monkeypatch.setattr(h, "TRUSTED_PROXIES", 1)
    assert h.client_ip(R("6.6.6.6, 1.2.3.4")) == "1.2.3.4"             # phần giả mạo ở đầu bị bỏ qua
    monkeypatch.setattr(h, "TRUSTED_PROXIES", 2)
    assert h.client_ip(R("6.6.6.6, 1.2.3.4, 10.0.0.1")) == "1.2.3.4"
    monkeypatch.setattr(h, "TRUSTED_PROXIES", 0)
    assert h.client_ip(R("6.6.6.6")) == "9.9.9.9"


# ---------- 6. tiêu đề bảo mật / cấu hình ----------
def test_security_headers_and_no_store(env):
    c = env.client()
    r = c.get("/app")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in r.headers["content-security-policy"]
    assert c.get("/auth/name-free", params={"name": "abc"}).headers["cache-control"] == "no-store"


def test_prod_autodetect(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    for k in ("COOKIE_SECURE", "PRODUCTION", "TRUSTED_PROXIES"):
        monkeypatch.setenv(k, "")
    for m in [m for m in sys.modules if m == "backend" or m.startswith("backend.")]:
        del sys.modules[m]
    monkeypatch.setenv("HN_DB", str(Path(tempfile.mkdtemp()) / "p.db")); monkeypatch.setenv("CACHE_DIR", tempfile.mkdtemp())
    main = importlib.import_module("backend.main"); h = importlib.import_module("backend.hydro")
    assert h.IS_PROD and h.COOKIE_SECURE and h.TRUSTED_PROXIES == 1
    assert main.app.docs_url is None


# ---------- 7. ảnh tải lên ----------
def test_photo_upload_limits(env):
    import io
    from PIL import Image
    h = env.hydro; u = add_user(h, "Nong Dan", "farmer")
    pid = h.q("INSERT INTO plots(owner_id,name,crop,lon,lat) VALUES(?,?,?,?,?)", (u, "t", "x", 108.0, 14.0), write=True)
    c = env.client(); login(c, "Nong Dan")
    buf = io.BytesIO(); Image.new("RGB", (50, 50), "green").save(buf, "JPEG")
    assert c.post(f"/plots/{pid}/photo", content=buf.getvalue()).status_code == 200
    assert c.post(f"/plots/{pid}/photo", content=b"x" * (9 * 1024 * 1024)).status_code == 413
    def gen():                                                          # thân chunked không có Content-Length
        for _ in range(10):
            yield b"x" * (1024 * 1024)
    assert c.post(f"/plots/{pid}/photo", content=gen()).status_code == 413
    assert c.post(f"/plots/{pid}/photo", content=b"khong phai anh").status_code == 400


def test_plot_cap(env):
    h = env.hydro; u = add_user(h, "Nong Dan", "farmer")
    c = env.client(); login(c, "Nong Dan")
    for i in range(50):
        h.q("INSERT INTO plots(owner_id,name,crop,lon,lat) VALUES(?,?,?,?,?)", (u, f"t{i}", "x", 108.0, 14.0), write=True)
    assert c.post("/plots", json={"name": "them", "lon": 108, "lat": 14}).status_code == 400
