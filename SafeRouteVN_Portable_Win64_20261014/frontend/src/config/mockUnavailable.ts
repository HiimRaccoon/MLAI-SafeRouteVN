import type { DispatchApi } from "../services/api/DispatchApi";
import { DispatchConfigurationError } from "./dispatchMode";
export function createMockDispatchApi(): DispatchApi {
  throw new DispatchConfigurationError("This backend build has no offline demo. Use npm run dev:mock or npm run build:mock for explicit mock mode.");
}
