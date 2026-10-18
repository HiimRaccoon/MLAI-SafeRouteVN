import type { DispatchApi } from "./DispatchApi";
import { BackendDispatchApi } from "./BackendDispatchApi";
import { Member3Client } from "../../integrations/member3/client";
import { dispatchMode } from "../../config/dispatchMode";

export async function createDispatchApi(env: { VITE_DISPATCH_MODE?: string; VITE_M3_BASE_URL?: string } = {
  VITE_DISPATCH_MODE: import.meta.env.VITE_DISPATCH_MODE,
  VITE_M3_BASE_URL: import.meta.env.VITE_M3_BASE_URL
}): Promise<DispatchApi> {
  if (dispatchMode(env.VITE_DISPATCH_MODE) === "backend") {
    return new BackendDispatchApi({ client: new Member3Client({ baseUrl: env.VITE_M3_BASE_URL,
      localProxy: import.meta.env.DEV && import.meta.env.VITE_LOCAL_PROXY === '1' }) });
  }
  const { createMockDispatchApi } = await import("@dispatch-mock-entry");
  return createMockDispatchApi();
}
