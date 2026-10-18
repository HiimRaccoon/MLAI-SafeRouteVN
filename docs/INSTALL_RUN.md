# Cài đặt và vận hành

## Yêu cầu

Windows 10/11 x64; Internet cho lần tải đầu; nên có ít nhất 10 GB dung lượng trống
cho source, gói nén, dữ liệu giải nén và phiên phát sinh. Bộ cài này sử dụng
GitHub/Hugging Face công khai, không yêu cầu đăng nhập của người nhận.

## Cài lần đầu

Giải nén source của đúng release rồi bấm `CAI_DAT_SAFEROUTE.bat`.
Bộ cài tải hai gói, kiểm SHA256 trước khi giải nén, kiểm toàn bộ manifest rồi
khởi tạo native solver. Đợi `INSTALL_OK`.

Nếu có lỗi tải, hash hoặc preflight, đọc thông báo và dừng. Không sửa hash
để vượt kiểm tra. Có thể chạy lại bộ cài sau khi xử lý nguyên nhân;
gói tải về có checksum đúng được dùng lại.

Để cài từ các gói đã tải sẵn, đặt `runtime.zip` và `data.zip` cùng thư mục:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 -PackageDirectory 'D:\SafeRoutePackages'
```

## Chạy hằng ngày

Bấm `CHAY_SAFEROUTE.bat`, đợi READY. Mở S0 → Optimize → chọn → Accept.
Không chạy BAT bên trong trình xem ZIP. Không chuyển thư mục khi app đang chạy.

Dừng bằng cửa sổ launcher hoặc:

```powershell
.\CHAY_SAFEROUTE.bat --stop
```

`/ready` của backend phải trả `data.ready=true` để chứng minh API/worker sẵn sàng.
Cổng mở hoặc `/health` chưa đủ. Cổng API/UI được launcher chọn và lưu dưới
`portable-state`; xem URL và log do launcher in ra.

## Internet và Driver

Tile bản đồ và lấy thời tiết mới cần Internet. Driver từ mạng ngoài cần
Tailscale Funnel trên máy quản trị. Bộ cài không đăng nhập hoặc bật Funnel.
Gateway local dùng `127.0.0.1:8788`; HTTPS công khai dùng cổng 443.

Giữ đúng hostname trong `deployment/funnel.json` và APK. Hostname khác cần
cấu hình và build phù hợp, xem `ANDROID_ACCOUNTS.md`.

## Phát triển từ source

Source, dependency lockfile và công cụ build nằm trong repository. Cài trước
để khôi phục fixture và dependencies. Runtime portable vẫn dùng source Python
và frontend tại thư mục ứng dụng.

Nếu sửa file đã được manifest ghim, cần quy trình build/inventory mới.
`backend/scripts/build_portable.py` mô tả build Windows với Python embedded,
Node, wheelhouse và VC runtime được ghim; full build của script đó còn yêu cầu
các đầu vào release lịch sử ghi trong source, không được coi là có sẵn chỉ vì
đã clone repository. Bộ cài hiện tại phục hồi runtime đã phát hành, không tự
tái tạo các wheelhouse lịch sử.

Kiểm tra wrapper cài đặt:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-installer.ps1
```

## Dữ liệu riêng

`portable-state` chứa token và phiên của từng cài đặt.
`SafeRouteVN_Driver_Accounts` ở cạnh thư mục ứng dụng chứa tài khoản thật và khóa ký.
Hai thư mục được loại khỏi Git. Sao lưu riêng trước khi thay máy hoặc build lại APK.
