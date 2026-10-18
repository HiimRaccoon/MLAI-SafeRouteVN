# APK và tài khoản Driver

## Cài APK

Tải `SafeRouteVN-Driver-Funnel.apk` từ GitHub Releases cùng tag.
Source Android nằm tại `PACKAGING/SafeRouteVNFunnel`.
Source hiện tại khai báo versionName 2.3.2, versionCode 7, Android 6.0 trở lên.

APK truy cập hostname Funnel cố định trong `MainActivity.java`.
Đổi hostname cần cập nhật cấu hình gateway, các kiểm tra origin liên quan
(bao gồm giới hạn domain trong `deployment/funnel_runtime.py`) và build lại APK.
Điện thoại không cần cài Tailscale cho luồng Funnel này.

## Tạo và quản lý tài khoản

1. Chạy SafeRoute và mở scenario trong Admin để có phiên READY.
2. Bấm `TAO_TAI_KHOAN.bat`, nhập Driver ID, ví dụ TX01.
3. Công cụ tạo/cập nhật `SafeRouteVN_Driver_Accounts/drivers-funnel.csv`.
4. Quản trị viên cung cấp riêng `login_code` cho đúng tài xế.

`examples/drivers-funnel.example.csv` chỉ có tên cột, không có tài khoản dùng được.
CSV thật nằm ngoài thư mục ứng dụng và được `.gitignore` loại khỏi repository.
Không đưa mã đăng nhập vào URL, ảnh chụp, log công khai hoặc APK.

Tool chọn phiên READY gần nhất thuộc dispatcher khi không truyền `--session`.
Để chỉ định chính xác phiên, dùng CLI `deployment/funnel_runtime.py issue`
với `--local-root`, `--origin`, `--driver-id` và `--session`.
Tài khoản do tool cấp có thời hạn 30 ngày. Đặt `enabled=0` trong CSV riêng
để thu hồi; supervisor đồng bộ lại auth store khi ứng dụng đang chạy.

## Build APK

Build cần Android Studio JBR ở `C:/Program Files/Android/Android Studio/jbr`,
Android SDK trong `ANDROID_HOME` hoặc vị trí mặc định, Build Tools 36.0.0
và Android platform 36. SDK chỉ cần khi build lại APK; bộ cài SafeRoute không
cài SDK. Script hiện tại dùng trực tiếp công cụ SDK, không tải Gradle.

Từ thư mục repository:

```powershell
& '.\SafeRouteVN_Portable_Win64_20261014\portable\python-backend\python.exe' '.\PACKAGING\SafeRouteVNFunnel\test_policy.py'
& '.\SafeRouteVN_Portable_Win64_20261014\portable\python-backend\python.exe' '.\PACKAGING\SafeRouteVNFunnel\build.py'
```

Output ở `PACKAGING/SafeRouteVNFunnel/dist`.
Khóa ký ở `SafeRouteVN_Driver_Accounts/signing-funnel`.
Nếu không có khóa, script tạo khóa mới; APK mới đó không cập nhật đè APK đã
ký bằng khóa cũ. Muốn tiếp tục bản cũ, phục hồi đúng khóa và password từ
backup riêng trước khi build. Không đưa khóa hoặc password lên GitHub/HF.

## Kiểm tra thiết bị

Kiểm đăng nhập, nhận tuyến sau Accept, chuyển scenario/Import, đưa app ra nền
rồi mở lại, và mạng Wi-Fi/4G trên điện thoại thật. Kết quả Java policy hoặc
máy ảo không thay thế các kiểm tra này.
