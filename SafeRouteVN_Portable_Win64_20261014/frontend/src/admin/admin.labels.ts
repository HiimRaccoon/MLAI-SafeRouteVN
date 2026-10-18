/**
 * UI display labels for Admin Dashboard (English).
 * All display strings are centralised here for easy i18n maintenance.
 * Do NOT change key names — they are referenced across AdminPage.tsx.
 */

export const LABELS = {
  /* App Header */
  brandName: "SafeRoute VN",
  brandSlogan: "Smarter Logistics. Safer Communities.",
  demoTimeChip: "Demo time",
  notificationsLabel: "System notifications",
  adminName: "Admin",
  adminRole: "Dispatcher",

  /* Order Entry */
  orderEntry: "Order Entry",
  addOrder: "+ Add Order",
  addOrderTitle: "Feature pending Member 3 API integration",
  orderIdLabel: "Order ID",
  pickupLocationLabel: "Pickup Location",
  deliveryLocationLabel: "Delivery Location",
  readyTimeLabel: "Ready Time",
  deliveryTimeWindowLabel: "Delivery Time Window",
  weightLabel: "Weight",
  priorityLabel: "Priority",
  primaryLabel: "Primary",
  notesLabel: "Notes (optional)",
  currentQueue: (n: number) => `Current Queue (${n} orders)`,
  priorityNormal: "Normal",
  priorityUrgent: "Urgent",

  /* Fleet / Preferences */
  fleetPreferences: "Fleet / Preferences",
  driversVehicles: "Drivers / Vehicles",
  vehicleType: "Vehicle Type",
  capacity: "Capacity",
  vehicleTypeValue: "Motorcycle",
  costImportance: "Cost Importance",
  punctualityImportance: "Punctuality Importance",
  safetyImportance: "Safety Importance",
  availableChip: (avail: number, total: number) => `${avail} / ${total} available`,

  /* Context & Events */
  contextEvents: "Context & Events",
  decisionEpoch: "Decision Epoch",
  urgentOrder: "Urgent Order",
  driverUnavailable: "Driver Unavailable",
  localRain: "Local Rain (What-if)",
  demoScenario: "Demo Scenario",
  optimize: "Optimize",
  reoptimize: "Re-optimize",
  acceptSelectedPlan: "Accept Selected Plan",

  /* Demo Dispatch Control */
  liveDispatchTitle: "Demo Dispatch Control",
  liveDispatchSubtitle: "Simulated vehicle tracking and route optimization",
  statusBeforeOptimize: "DEMO DISPATCH (BEFORE OPTIMIZE)",
  statusAfterOptimize: "DEMO DISPATCH (AFTER OPTIMIZE)",
  mapTab: "Map",
  heavyRainBadge: "Heavy rain in 2 areas",

  /* KPI Labels */
  kpiTotalOrders: "TOTAL ORDERS",
  kpiActiveVehicles: "ACTIVE VEHICLES",
  kpiEstTravelTime: "EST. TOTAL TRAVEL TIME",
  kpiOnTimeRate: "ON-TIME DELIVERY RATE",
  kpiFuelCost: "FUEL COST (EST.)",
  kpiSafetyExposure: "RELATIVE EXPOSURE PROXY",
  kpiAllAvailable: "All available",
  kpiVehicleIssue: "Vehicle issue",
  kpiOptimized: (n: number) => `${n} optimized`,

  /* Map Legend */
  legendDepot: "Depot",
  legendCustomerStop: "Customer Stop",
  legendVehicle: "Vehicle (M1, M2, M3)",
  legendPlannedRoute: "Planned Route",
  legendUnassignedOrder: "Unassigned Order",
  legendRainArea: "Rain Area",

  /* Before / After Comparison */
  comparisonTitle: "Before / After Event Comparison",
  comparisonDemoData: "Demo data",
  colMetric: "METRIC",
  colBefore: "BEFORE (CURRENT)",
  colAfter: "AFTER (OPTIMIZED)",
  colChange: "CHANGE",
  metricTravelTime: "Total travel time",
  metricDistance: "Total distance",
  metricFuelCost: "Fuel cost (est.)",
  metricOnTimeRate: "On-time delivery rate",
  metricRiskExposure: "Relative exposure proxy",
  notSelected: "\u2014",

  /* Decision Intelligence */
  decisionIntelligence: "Decision Intelligence",
  decisionSubtitle: "3 alternative plans based on different optimization goals",
  notOptimized: "Not optimized",
  alternativesCount: "3 alternatives",
  recommendedTag: "Recommended",
  viewDetails: "View Details",
  metricETA: "ETA",
  metricFuel: "Fuel cost",
  metricOnTime: "On-time rate",
  metricRisk: "Exposure proxy",

  /* Profile descriptions */
  profileDescriptions: {
    FASTEST: "Minimize total travel time",
    BALANCED: "Best overall performance",
    SAFER: "Minimize exposure proxy",
  } as Record<string, string>,

  /* Driver / Vehicle Status */
  driverVehicleStatus: "Driver / Vehicle Status",
  liveStatus: "Demo status",
  colNum: "#",
  colVehicle: "VEHICLE",
  colDriver: "DRIVER",
  colCurrentStop: "CURRENT STOP",
  colETA: "ETA",
  colStatus: "STATUS",
  statusEnRoute: "En route",
  statusDelayed: "Delayed",
  statusIdle: "Idle",
  statusUnavailable: "Unavailable",
  depotLabel: "Depot",

  /* Provenance */
  demoDataLabel: "Demo data",
  scenarioLabel: "Scenario",
  versionLabel: "Version",
  provenanceNote: "Decision and route geometry are pre-prepared demo data for Phase 1. Ready to integrate Member 2 & 3 APIs.",

  /* Scenario Options */
  scenarios: {
    S0: "S0 \u2014 Initial Plan (Depot, 3 orders, 2 vehicles)",
    S1: "S1 \u2014 Normal Delivery Day (8 orders, 2 vehicles)",
    S2: "S2 \u2014 Urgent Order (O009)",
    S3: "S3 \u2014 Vehicle Unavailable (V1 breakdown)",
    S4: "S4 \u2014 Local Heavy Rain (Thu Duc flood zone)",
  } as Record<string, string>,
} as const;
