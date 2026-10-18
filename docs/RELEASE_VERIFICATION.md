# Xác minh bản phát hành MLAI_7 — 18/10/2026

## Đã kiểm tra tại máy build

- Backup MLAI_7 → MLAI_8: 51.453 file, khớp tập file, kích thước và SHA256.
- Git bundle của repository trước khi thay source: kiểm tra hợp lệ, giữ lịch sử.
- 19.003 file trong manifest portable: khớp byte và SHA256.
- Tách 592 file source, 126 file dữ liệu, 18.285 file runtime/thư viện.
- Bộ cài từ gói local: `INSTALL_OK`, `G0_TECHNICAL_PASS`, dữ liệu đầu vào
  `COMPLETE_VERIFIED`, CP-SAT native smoke `OPTIMAL`.
- Kiểm ZIP hợp lệ, checksum sai, bốn kiểu đường dẫn thoát thư mục và tên file
  trùng không phân biệt hoa/thường: PASS.
- Launcher staging: Backend `/ready` có `data.ready=true`, Admin HTTP 200.
- S0: load → Optimize → forecast hợp lệ → Accept → replay step: PASS.
- Công cụ cấp tài khoản tạo CSV riêng cho tài khoản kiểm thử: PASS.
- Gateway test: kiểm host, token, phiên, thao tác và static boundary: PASS.
- Ba kiểm thử đồng bộ/revoke CSV và phục hồi supervisor: PASS.
- Android: 16 Java policy checks PASS; APK 2.3.2/versionCode 7;
  chữ ký v1/v2/v3 hợp lệ.
- Archive CRC và đối chiếu tài khoản thật, admin token, mật khẩu khóa ký:
  không tìm thấy giá trị riêng trong gói data/runtime/APK.
- Đối chiếu 620 file Git với các bí mật đang lưu riêng: không tìm thấy.
- Rà soát độc lập bộ cài, tài liệu, BAT và file loại trừ: không có lỗi quan trọng.

Lần pytest đầu bị chặn quyền thư mục tạm; chạy lại với quyền và thư mục
test thích hợp đã đạt 3/3. Đây là lỗi môi trường của lần chạy đầu.

Data Hugging Face được ghim tại revision
`f13b287a3028832ab7ce0aa1e99b72c6511bd6df`.
Hash gói nằm trong `release-lock.json` và `SHA256SUMS.txt` của release.

## Phạm vi và giới hạn

Đây là kiểm tra bản phân phối trên máy Windows hiện có. Chưa phải xác minh
trên máy Windows sạch, điện thoại thật hoặc đo Wi-Fi/4G của bản phát hành này.
Không tuyên bố toàn bộ test suite của ứng dụng đã chạy.

Gateway Funnel được tạm vô hiệu riêng trong staging khi chạy smoke local,
để không đưa phiên kiểm thử lên public hostname. Cấu hình được phục hồi
nguyên byte sau kiểm tra; Gateway được kiểm riêng bằng test của nó.

Quy trình cài lại từ GitHub/Hugging Face công khai được xác minh sau khi các
asset đã publish; kết quả cuối cùng ghi trong release notes.
