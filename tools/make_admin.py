"""Tạo hoặc đặt lại tài khoản QUẢN LÝ TỈNH ngay, không phụ thuộc vào .env.

Chạy (đứng ở thư mục dự án EcoPulse):
    python tools/make_admin.py EMAIL ["Tên hiển thị"] ["Tên tỉnh"]
Ví dụ:
    python tools/make_admin.py minh@example.com "Nguyễn Văn Minh" "Lâm Đồng"

Mật khẩu được HỎI KHI CHẠY (không hiện trên màn hình, không nằm trong lịch sử lệnh).
Khi chạy tự động (CI, script) có thể truyền qua biến môi trường HN_NEW_PASSWORD.

- Bỏ trống "Tên tỉnh" thì tài khoản thấy toàn bộ các tỉnh (dùng cho chủ hệ thống).
- Email chưa có   -> tạo mới.   Email đã có -> nâng lên quản lý tỉnh, đặt lại mật khẩu, mở khóa, đăng xuất mọi thiết bị.
Đăng nhập trên web bằng đúng email và mật khẩu này.
"""
import getpass
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import hydro as h  # noqa: E402


def ask_password():
    pw = os.getenv("HN_NEW_PASSWORD")
    if pw:
        return pw
    pw = getpass.getpass("Mật khẩu mới (từ 8 ký tự): ")
    if pw != getpass.getpass("Nhập lại mật khẩu: "):
        sys.exit("Hai lần nhập không khớp.")
    return pw


def main():
    a = sys.argv[1:]
    if len(a) < 1:
        sys.exit(__doc__)
    email = a[0].strip().lower()
    name = a[1].strip() if len(a) > 1 else "Quản trị"
    prov = a[2].strip() if len(a) > 2 else ""
    if not re.fullmatch(h.EMAIL_RE, email):
        sys.exit("Email chưa đúng định dạng.")
    pw = ask_password()
    if len(pw) < 8:
        sys.exit("Mật khẩu nên từ 8 ký tự trở lên.")
    pk = h.province_key(prov) if prov else None
    vals = (name, h.fold(name), h.hash_pw(pw), prov or None, pk)
    row = h.q("SELECT id FROM users WHERE lower(email)=?", (email,), one=True)
    if row:
        h.q("UPDATE users SET role='admin_province', full_name=?, name_key=?, pw_hash=?, province=?, province_key=?, ward=NULL, ward_key=NULL, status='active', pending=0, fails=0, locked_until=NULL WHERE id=?",
            vals + (row["id"],), write=True)
        h.q("DELETE FROM sessions WHERE user_id=?", (row["id"],), write=True)
        print(f"Đã nâng {email} lên quản lý tỉnh, đặt lại mật khẩu, mở khóa và đăng xuất mọi thiết bị.")
    else:
        h.q("INSERT INTO users(email,full_name,name_key,pw_hash,province,province_key,role) VALUES(?,?,?,?,?,?,'admin_province')", (email,) + vals, write=True)
        print(f"Đã tạo quản lý tỉnh mới: {email}")
    if prov:
        print("Mã tham gia của tỉnh:", h.area_for("province", prov)["code"])
    print("Cơ sở dữ liệu:", h.DB)


if __name__ == "__main__":
    main()
