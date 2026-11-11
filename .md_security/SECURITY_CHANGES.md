# Thay đổi bảo mật (đợt 1)

## Đưa vào dự án
Chép đè các file cùng tên: `backend/hydro.py`, `backend/main.py`, `backend/render.py`, `backend/assistant.py`,
`backend/weather.py`, `static/app.html`, `tools/make_admin.py`; thêm mới `tools/export_safe_db.py`, `tests/test_security.py`.
`env.example` và `gitignore` là bản cập nhật: **đổi tên thành `.env.example` và `.gitignore`** (có dấu chấm đầu).
Chạy thử: `pip install pytest` rồi `python -m pytest tests -q` (20 test, dùng DB tạm, không đụng dữ liệu thật).

## Việc cần làm khi triển khai
1. **Lần chạy đầu sau khi cập nhật**, DB tự thêm cột `users.pending`, `sessions.level` và **xóa toàn bộ phiên đăng nhập một lần**
   (mọi người đăng nhập lại; phiên "Quên mật khẩu" cũ từng có đủ quyền nên không giữ lại).
2. Đặt `TRUSTED_PROXIES` đúng (xem `env.example`). Sai số này thì giới hạn theo IP hoặc không chặn được, hoặc chặn nhầm cả xã dùng chung IP.
3. `tools/make_admin.py` đổi cách dùng: `python tools/make_admin.py EMAIL "Tên" "Tỉnh"`, mật khẩu được hỏi khi chạy.
4. Đừng gửi file DB thật cho ai. Cần chia sẻ thì chạy `python tools/export_safe_db.py` để lấy bản đã xóa dữ liệu cá nhân.
   DB trong file rar bạn gửi còn bảng cũ `users_v1`, `messages`, `coops` có số điện thoại và mật khẩu băm.
5. Vào MapTiler giới hạn key theo tên miền (key này công khai qua `/api/config`).

## Hành vi mới
- **Quên mật khẩu = chế độ chỉ xem.** Phiên này chỉ xem "Thửa của tôi" (danh sách, chi tiết, lịch sử, ảnh của chính mình).
  Chặn: thêm/sửa/xóa thửa và ảnh, thông báo, chat, kiến thức, Bác Nông, đọc to, bản đồ, tổng quan. Giao diện chỉ hiện mục "Thửa của tôi"
  và một khung nhập mật khẩu (`POST /auth/upgrade`) để mở đầy đủ. Đăng nhập bằng mật khẩu thật thu hồi mọi phiên chỉ xem của tài khoản đó.
  Chủ tài khoản nhận thông báo "Có người vào tài khoản bằng Quên mật khẩu" (tối đa 1 lần/giờ).
  Muốn cho phép sửa thửa ở chế độ này: đổi `Depends(full)` thành `Depends(user)` ở `add_plot`, `del_plot`, `put_photo`, `del_photo` (và hiện lại nút trong `app.html`).
- **Quản lý xã/phường phải được tỉnh duyệt.** Sau khi xác nhận email, tài khoản ở trạng thái chờ (không đăng nhập, không hiện trong danh bạ).
  Quản lý tỉnh nhận thông báo, thấy chấm đỏ ở mục "Người dùng", bấm Duyệt hoặc Từ chối. Tỉnh chưa có quản lý tỉnh thì không nhận đăng ký.
  Tỉnh vẫn do quản trị viên tạo bằng `make_admin.py`.

## Lỗi đã vá
| Mục | Vá |
|---|---|
| XSS lưu trữ ở tab Kiến thức | `esc()` cho `data-topic`; server chỉ nhận chuyên mục/cây trồng trong danh sách; `href="/\evil.com"` bị chặn |
| Quên mật khẩu mở toàn quyền | Phiên hạn chế (xem trên) |
| Ai cũng thành quản lý xã/phường | Tỉnh duyệt (xem trên) |
| `/api/render` cho mọi tài khoản | Chỉ cán bộ bắt đầu cảnh mới, tối đa `MAX_RENDER_JOBS` cảnh cùng lúc, 20 lượt/giờ/người; nông dân chỉ xem cảnh đã xử lý |
| Path traversal `/api/value` | id/layer kiểm tra bằng regex ASCII và danh sách lớp; kiểm tra thêm trong `render.py` |
| Rò token Earthdata sang `evilnasa.gov` | So khớp `nasa.gov` hoặc `*.nasa.gov`; URL tải về phải đúng host ASF, https, không có user:pass |
| `/tiles` mở cả thư mục cache | Chỉ phục vụ `*.png` |
| Không giới hạn tần suất | Đăng nhập (IP, IP+tên), đăng ký, quên mật khẩu, name-free, OTP, gửi mã admin, chat, thông báo hàng loạt, ảnh, TTS, geo, AI (có trần toàn hệ thống) |
| Khóa tài khoản người khác | Ngưỡng khóa tài khoản 5 → 10 lần sai; phần lớn chặn theo IP+tên trước |
| Dò tài khoản bằng thời gian | Luôn băm mật khẩu giả khi không có tài khoản; giới hạn độ dài mật khẩu đăng nhập |
| Thiếu security headers | `nosniff`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, HSTS (khi bật Secure), CSP `frame-ancestors/base-uri/object-src/form-action`, `Cache-Control: no-store` cho JSON |
| Cấu hình mặc định lỏng | Tự nhận Railway: bật cookie Secure, tắt `/docs`. Phiên hết hạn được dọn |
| Upload ảnh | Đọc theo luồng và dừng ở 8 MB (kể cả chunked); từ chối ảnh quá 50 triệu điểm ảnh; tối đa 50 thửa/người; ảnh của người khác trả 404 thay vì 403 |
| Lộ thông tin lỗi | ASF/xử lý cảnh chỉ trả thông điệp tiếng Việt, chi tiết kỹ thuật ghi ở log máy chủ |
| TTS gửi tin nhắn sang Google | Giọng Google mặc định tắt (`TTS_GOOGLE_FALLBACK=1` để bật); TTS cần đăng nhập đầy đủ |
| `make_admin.py` lộ mật khẩu trong lịch sử lệnh | Hỏi bằng `getpass`; reset mật khẩu còn đăng xuất mọi thiết bị |

## Chưa làm (cần quyết định hoặc thêm dữ liệu)
- **Đặt lại mật khẩu nông dân.** Nông dân thật sự quên mật khẩu sẽ ở chế độ chỉ xem mãi. Cách gọn: cán bộ xã bấm "Cấp mã đặt lại", nông dân nhập tên + mã 6 số.
- **CSP có `script-src`.** Giao diện còn `onclick=`/`onerror=` và script inline nên chưa đặt được; cần chuyển sang file `.js` riêng và `addEventListener`.
- **SRI cho script CDN** (`three.js` trong `app.html`, các script trong `index.html`/`landing.html`): chưa kiểm tra `index.html`, `landing.html`.
- **Ghim phiên bản `requirements.txt`**: chạy `pip freeze` trên máy đang chạy tốt rồi dán vào.
- **CSRF**: hiện dựa vào `SameSite=Lax` và API JSON. Nếu sau này bản mobile/web ở tên miền khác dùng cookie, cần thêm kiểm tra `Origin`.
- Kiểm thử mới chạy trên DB tạm và giao diện giả lập (jsdom), chưa thử trên Railway thật.
