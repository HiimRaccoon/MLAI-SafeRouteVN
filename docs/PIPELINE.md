# Pipeline

## Dữ liệu

Mạng đường OSM → đồ thị có hướng và chính sách phương tiện → đặc trưng cạnh
→ ngữ cảnh travel/risk/weather → snapshot và manifest cho runtime.

Bản phân phối tải dữ liệu đã chuẩn bị; không tải lại OSM mỗi lần mở app.
Danh mục file và SHA256 nằm trong `data-files.json`. Bộ cài dùng revision
Hugging Face và checksum được ghim trong `release-lock.json`.

## Điều phối

1. Admin mở scenario có sẵn hoặc chuẩn bị phiên từ dữ liệu nhập.
2. Backend xác thực và quản lý phiên.
3. Optimize tạo tác vụ cho worker/runtime.
4. Runtime đọc snapshot, áp dụng ràng buộc và mục tiêu từng profile.
5. Admin xem kết quả, so sánh và chọn phương án.
6. Accept công bố phương án của phiên cho Driver được cấp quyền.
7. Replay và xác nhận giao hàng cập nhật trạng thái phiên.

Không mặc định ba profile luôn tạo ra ba tuyến khác nhau. Các tuyến phụ thuộc
dữ liệu, ràng buộc và mục tiêu. Lời giải thích bằng ngôn ngữ tự nhiên không
thay đổi quyết định định tuyến của solver.

## Phát hành

1. Dừng cài đặt gốc, sao lưu đầy đủ và kiểm SHA256.
2. Sao lưu lịch sử Git hiện tại bằng Git bundle.
3. Đối chiếu từng file với manifest portable.
4. Tách source, data và runtime; loại trạng thái riêng khỏi bản phân phối.
5. Kiểm bộ cài, native preflight, API/worker và APK.
6. Upload data lên Hugging Face, ghim revision và checksum.
7. Commit thay cây source hiện tại, giữ lịch sử; phát hành runtime và APK.
8. Tải lại source và các gói để xác minh đường cài đặt công khai.

CSV tài khoản thật, khóa ký, token, cache phiên và báo cáo có dữ liệu vận hành
không được đưa vào pipeline upload công khai này.
