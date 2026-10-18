"""Optional display-only fields; never change routing coordinates or custody."""
ORDER_FIELDS = {'customerName': 160, 'customerPhone': 64, 'address': 500, 'notes': 2000}
VEHICLE_FIELDS = {'driverName': 160, 'driverPhone': 64, 'licensePlate': 40, 'vehicleModel': 120}


def display_metadata(source, fields):
    if not isinstance(source, dict):
        raise ValueError("Thông tin hiển thị cần nằm trong object đơn hàng hoặc xe.")
    result = {}
    for key, maximum in fields.items():
        value = source.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 and c not in '\n\t' for c in value):
            raise ValueError(f'{key}: cáº§n chuá»—i tá»‘i Ä‘a {maximum} kÃ½ tá»±.')
        if value.strip():
            result[key] = value.strip()
    return result
