# SafeRouteVN — bản phát hành MLAI_7

Ứng dụng điều phối giao hàng trên Windows x64, gồm giao diện Admin/Driver,
backend, runtime tối ưu tuyến, Driver Gateway và source Android APK.

## Cài đặt và chạy

1. Tải **Source code (zip)** tại [release MLAI_7](https://github.com/VietNguyen-21/Apex-SafezrouteVN/releases/tag/mlai7-20261018), giải nén toàn bộ.
2. Bấm **CAI_DAT_SAFEROUTE.bat**, chờ `INSTALL_OK`.
3. Bấm **CHAY_SAFEROUTE.bat**, đợi READY và trang Admin mở.
4. Thử **S0 → Optimize → chọn phương án → Accept**.

Bộ cài tải runtime đã đóng gói từ GitHub Releases và dữ liệu từ Hugging Face,
kiểm SHA256 rồi khởi tạo cài đặt riêng. Không cần cài riêng Python, Node.js,
pip hoặc npm để chạy bản portable. Lần tải đầu cần Internet.

Hai BAT có chức năng riêng: `CAI_DAT_SAFEROUTE.bat` dùng khi cài;
`CHAY_SAFEROUTE.bat` dùng hằng ngày. Dừng ứng dụng trước khi chạy lại bộ cài.

## APK và tài khoản

- Tải [SafeRouteVN-Driver-Funnel.apk](https://github.com/VietNguyen-21/Apex-SafezrouteVN/releases/download/mlai7-20261018/SafeRouteVN-Driver-Funnel.apk).
- Chạy Admin, mở một scenario rồi bấm `TAO_TAI_KHOAN.bat` để cấp tài khoản Driver.
- Xem [hướng dẫn APK và tài khoản](docs/ANDROID_ACCOUNTS.md).
- APK hiện tại gắn với hostname Funnel của bản MLAI_7. Đổi máy chủ/hostname cần cấu hình và build tương ứng.

## Kiến trúc và tài liệu

| Thành phần | Nơi lưu |
|---|---|
| Source, lockfile, cấu hình, schema, tài liệu và BAT | Repository GitHub này |
| Python/Node/thư viện đã đóng gói và APK | GitHub Releases cùng tag |
| Snapshot, scenario, fixture dữ liệu | [Hugging Face dataset](https://huggingface.co/datasets/nguyenviet21/saferoutevn-snapshots) |
| Phiên chạy, tài khoản thật, token và khóa ký | Máy quản trị, không đưa vào Git |

- [Kiến trúc](docs/ARCHITECTURE.md)
- [Pipeline dữ liệu, tối ưu và phát hành](docs/PIPELINE.md)
- [Cài đặt, vận hành và phát triển](docs/INSTALL_RUN.md)
- [APK, build và tài khoản](docs/ANDROID_ACCOUNTS.md)
- [Danh mục dữ liệu và hash](docs/data-files.json)
- [Kết quả xác minh bản phát hành](docs/RELEASE_VERIFICATION.md)

Chuyển động xe là mô phỏng/replay, không phải GPS trực tiếp.
Kiểm thử trên máy phát triển hoặc máy ảo không thay thế kiểm thử máy Windows mới
và điện thoại thật qua Wi-Fi/4G.
