"""Tạo BẢN SAO DB đã xóa dữ liệu cá nhân để gửi người khác xem / nhờ gỡ lỗi (đừng bao giờ gửi file DB thật).

Chạy:
    python tools/export_safe_db.py [đường_dẫn_db_nguồn] [file_đầu_ra]
Mặc định đọc DB đang dùng (HN_DB hoặc data/hydronisar.db) và ghi ra data/hydronisar.safe.db.

Giữ lại: cấu trúc bảng, số lượng, vai trò, tỉnh/xã, loại cây, tên chuyên mục.
Xóa/thay: tên, email, số điện thoại, mật khẩu băm, phiên đăng nhập, mã OTP, tin nhắn, nhật ký, tọa độ thửa (làm tròn ~10 km), ảnh (không nằm trong DB).
Mọi tài khoản trong bản sao có mật khẩu: "demo-password-123".
"""
import os
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    from backend import hydro as h
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(h.DB)
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "data" / "hydronisar.safe.db"
    if not src.exists():
        sys.exit(f"Không thấy DB: {src}")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    shutil.copyfile(src, out)
    c = sqlite3.connect(out)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    demo_pw = h.hash_pw("demo-password-123")
    if "users" in tables:
        for (uid, role) in c.execute("SELECT id, role FROM users").fetchall():
            nm = {"farmer": "Nong dan", "admin_ward": "Quan ly xa", "admin_province": "Quan ly tinh"}.get(role, "Nguoi dung") + f" {uid}"
            c.execute("UPDATE users SET full_name=?, name_key=?, email=?, phone=NULL, pw_hash=?, fails=0, locked_until=NULL WHERE id=?",
                      (nm, h.fold(nm), f"user{uid}@example.invalid" if role != "farmer" else None, demo_pw, uid))
    for t in ("sessions", "otps", "dm", "notifications", "audit_log", "users_v1", "messages", "coops", "coop_members"):   # users_v1/messages/coops là bảng cũ còn sót, có số điện thoại và mật khẩu băm
        if t in tables:
            c.execute(f"DELETE FROM {t}")
    if "plots" in tables:
        c.execute("UPDATE plots SET name='Thua ' || id, lon=round(lon, 1), lat=round(lat, 1)")
    if "news" in tables:
        c.execute("UPDATE news SET title='Bai viet ' || id, summary='', body='', source='', cover=''")
    c.commit()
    c.execute("VACUUM")
    c.close()
    print("Đã tạo bản sao an toàn:", out)
    print("Kiểm tra lại bằng mắt trước khi gửi. Ảnh thửa (data/plots/*.jpg) KHÔNG nằm trong bản sao này.")


if __name__ == "__main__":
    main()
