import type { CustomerPresentation, DriverPresentation } from "../shared/presentation";
export type { CustomerPresentation, DriverPresentation } from "../shared/presentation";

const customerCatalog: Record<string, CustomerPresentation> = {
  O001: {
    name: "Nguyễn Văn Minh",
    phone: "0389 123 456",
    address: "12 Đường số 10, P. Thảo Điền, TP. Thủ Đức, TP.HCM",
    timeWindow: "14:00 - 15:00",
    notes: "Gọi trước khi giao 10 phút"
  },
  O002: {
    name: "Trần Thị Hoa",
    phone: "0912 345 678",
    address: "245 Điện Biên Phủ, P. 15, Q. Bình Thạnh, TP.HCM",
    timeWindow: "15:30 - 16:00",
    notes: "Gửi lễ tân tầng trệt nếu vắng mặt"
  },
  O003: {
    name: "Phạm Minh Quân",
    phone: "0903 888 999",
    address: "Đường số 17, P. Hiệp Bình Chánh, TP. Thủ Đức, TP.HCM",
    timeWindow: "17:30 - 18:00",
    notes: "Cổng màu xám, bấm chuông"
  },
  O009: {
    name: "Lê Thanh Tùng",
    phone: "0977 112 233",
    address: "Kha Vạn Cân, P. Linh Chiểu, TP. Thủ Đức, TP.HCM",
    timeWindow: "16:30 - 17:00",
    notes: "Đơn hàng khẩn cấp - ưu tiên giao sớm"
  }
};

const driverCatalog: Record<string, DriverPresentation> = {
  V1: {
    id: "V1",
    name: "Nguyễn Văn A",
    phone: "0988 123 456",
    vehicleModel: "Honda Wave Alpha",
    licensePlate: "59-X1 234.56",
    rating: "4.9 ★",
    avatar: "A"
  },
  V2: {
    id: "V2",
    name: "Trần Thị B",
    phone: "0977 654 321",
    vehicleModel: "Yamaha Sirius",
    licensePlate: "59-Y2 789.10",
    rating: "4.8 ★",
    avatar: "B"
  },
  V3: {
    id: "V3",
    name: "Lê Văn C",
    phone: "0966 321 987",
    vehicleModel: "Honda Air Blade",
    licensePlate: "59-Z3 456.78",
    rating: "4.7 ★",
    avatar: "C"
  }
};

export const DEPOT_PRESENTATION = {
  id: "DEPOT",
  name: "Kho Trung Tâm SafeRoute",
  address: "123 Quốc Lộ 13, P. Hiệp Bình Chánh, TP. Thủ Đức, TP.HCM",
  phone: "028 3720 0000"
};

export function getCustomerInfo(orderId: string): CustomerPresentation {
  if (customerCatalog[orderId]) {
    return customerCatalog[orderId];
  }
  return {
    name: `Khách hàng ${orderId}`,
    phone: "0901 000 000",
    address: `Địa chỉ giao hàng ${orderId}, TP. Thủ Đức, TP.HCM`,
    timeWindow: "14:00 - 18:00"
  };
}

export function getDriverInfo(vehicleId: string): DriverPresentation {
  if (driverCatalog[vehicleId]) {
    return driverCatalog[vehicleId];
  }
  return {
    id: vehicleId,
    name: `Tài xế ${vehicleId}`,
    phone: "0909 000 000",
    vehicleModel: "Xe máy",
    licensePlate: "59-Z9 999.99",
    rating: "4.9 ★",
    avatar: vehicleId.slice(-1)
  };
}
