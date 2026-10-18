# Member 1 — Geo Data

Đã có code M1-00–M1-09: ingestion, graph preview, routing với via-node restrictions và loại bảo thủ trường hợp chưa hỗ trợ, travel ước lượng, weather theo vùng, safety proxy, S0–S8 có seed và QA. Run thành phố đã đạt QA; toàn bộ 76 test fixture/HTTP mock đạt. Đã có chế độ weather live, chờ người dùng chạy trên dữ liệu thành phố. Chưa nghiệm thu tích hợp consumer.

**Từ 27/09/2026, toàn TP.HCM dùng `bulk-download` + `extract-city`:** tải Geofabrik Vietnam PBF có resume/checksum rồi lọc cục bộ. Overpass theo ô dành cho kiểm tra nhỏ. Đọc phần Boundary, PBF và graph trong runbook để sử dụng luồng này.

Phạm vi đích: TP.HCM hiện hành. Boundary thật phải được xác minh; fixture test không đại diện toàn thành phố. `build-graph` tạo **graph preview**, `prepare-routing` tạo mạng đã xử lý restriction theo policy. Routing phải dùng incoming-edge state và bảng cấm rẽ. Travel là ESTIMATED, safety là PROXY, scenario là SYNTHETIC; S7/S8 còn phụ thuộc bằng chứng tuyến tìm được trên dataset thực.

- [Hướng dẫn chung: setup, pipeline, realtime, bàn giao và contract](../docs/member1_runbook.md)
- [Kế hoạch toàn bộ Member 1](../../SAFEROUTE_MEMBER1_IMPLEMENTATION_PLAN.md)

Từ thư mục `SafeRouteVN/`:

```powershell
.\.venv\Scripts\python.exe -m geo_data.cli --help
.\.venv\Scripts\python.exe -m unittest discover -s geo_data/tests -v
```

Module hiện có: `osm/`, `features/`, `weather/`, `scenario_generator.py`, `qa.py`, `pipeline.py`, `units.py`, `cli.py`, `tests/`. Config dự thảo nằm trong `geo_data/`, chưa sửa `configs/`/`shared/`. Các timestamp chuẩn hóa dùng giờ Việt Nam; quãng đường km, tốc độ km/h, thời lượng h, tiền VND, tải kg.
