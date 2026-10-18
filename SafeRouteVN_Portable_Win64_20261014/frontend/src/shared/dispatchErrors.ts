export type DispatchErrorCode =
  | "NOT_AT_STOP"
  | "INVALID_SCENARIO"
  | "EVENT_ALREADY_TRIGGERED"
  | "EVENT_NOT_READY"
  | "EVENT_WINDOW_EXPIRED"
  | "NO_SELECTED_ALTERNATIVE"
  | "STALE_PROPOSAL"
  | "NO_ACTIVE_PLAN"
  | "VEHICLE_UNAVAILABLE"
  | "INVALID_CURRENT_STOP"
  | "URGENT_ORDER_NOT_CANCELLABLE"
  | "INVALID_ORDER_STATE";

export class DispatchError extends Error {
  constructor(public readonly code: DispatchErrorCode, message: string) {
    super(message);
    this.name = "DispatchError";
  }
}
