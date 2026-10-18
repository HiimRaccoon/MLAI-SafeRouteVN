export type ScenarioId = "S0" | "S1" | "S2" | "S3" | "S4";
export type OrderStatus = "WAITING" | "ONBOARD" | "DELIVERED";
export type VehicleAvailability = "AVAILABLE" | "UNAVAILABLE";

export interface Location {
  id: string;
  kind?: "DEPOT" | "DELIVERY";
  orderId?: string;
  graphNodeId?: string | number;
  latitude: number;
  longitude: number;
  openingTime: string;
  closingTime: string;
}

export interface Order {
  customerName?: string;
  customerPhone?: string;
  address?: string;
  notes?: string;
  id: string;
  latitude: number;
  longitude: number;
  demandKg: number;
  priority: number;
  pickupLocationId: string;
  status: OrderStatus;
  assignedVehicleId: string | null;
  pickedUpAt: string | null;
  deliveredAt: string | null;
  deliveryRegionId: string;
  earliest: string;
  preferredDue: string;
  hardDeadline: string;
  serviceTimeHours: number;
  graphNodeId: number | string;
}

export interface Vehicle {
  driverName?: string;
  driverPhone?: string;
  licensePlate?: string;
  vehicleModel?: string;
  id: string;
  type: string;
  availability: VehicleAvailability;
  capacityKg: number;
  currentLoadKg: number;
  onboardOrderIds: string[];
  currentPosition: { latitude: number; longitude: number; graphNodeId: number | string | null };
  positionTimestamp: string | null;
  workingStart: string;
  workingEnd: string;
  [key: string]: unknown;
}

export interface GeoJsonPolygon {
  type: "Polygon";
  coordinates: number[][][];
}

export interface FixtureEvent {
  eventId: string;
  type: "URGENT_ORDER" | "VEHICLE_UNAVAILABLE" | "LOCAL_RAIN_WHAT_IF";
  timestamp: string;
  sourceType: string;
  orderPayload?: Order;
  vehicleId?: string;
  availability?: VehicleAvailability;
  polygon?: GeoJsonPolygon;
  contextDelta?: Record<string, unknown>;
  startTime?: string;
  endTime?: string;
  requiresFeatureRecompute?: boolean;
  [key: string]: unknown;
}

export interface ScenarioFixture {
  initialState: {
    currentTime: string;
    stateVersion: number;
    locations: Location[];
    orders: Order[];
    vehicles: Vehicle[];
    currentPlans: unknown[];
    [key: string]: unknown;
  };
  events: FixtureEvent[];
  contextVersion: string;
  deliveryAreaVersion: string;
  [key: string]: unknown;
}
