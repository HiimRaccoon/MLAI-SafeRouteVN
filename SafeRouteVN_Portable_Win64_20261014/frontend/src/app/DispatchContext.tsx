import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { DispatchSnapshot } from "../shared/types/dispatch";
import type { DispatchApi } from "../services/api/DispatchApi";
import { createDispatchApi } from "../services/api/createDispatchApi";
import { Member3Error } from "../integrations/member3/errors";
import { DispatchError, type DispatchErrorCode } from "../shared/dispatchErrors";
import { dispatchPresentation } from "../shared/presentation";
import { DispatchConfigurationError } from "../config/dispatchMode";

let defaultApi: Promise<DispatchApi> | undefined;
function getDefaultApi() { return defaultApi ??= createDispatchApi(); }

interface DispatchContextValue {
  api: DispatchApi;
  snapshot: DispatchSnapshot | null;
  pending: boolean;
  error: string | null;
  invoke(operation: () => Promise<DispatchSnapshot>): Promise<void>;
}
const DispatchContext = createContext<DispatchContextValue | null>(null);
const errorMessages: Record<DispatchErrorCode, string> = {
  NOT_AT_STOP: "Vehicle has not arrived. Play or advance the simulation to reach this stop.",
  INVALID_SCENARIO: "Invalid scenario. Check coordinates, IDs, custody and capacity.",
  EVENT_ALREADY_TRIGGERED: "Only one event can be triggered in this demo round.",
  EVENT_NOT_READY: "This event or plan is not ready.",
  EVENT_WINDOW_EXPIRED: "The event window has expired.",
  NO_SELECTED_ALTERNATIVE: "Select a plan before dispatching.",
  STALE_PROPOSAL: "This proposal is outdated. Re-optimize to continue.",
  NO_ACTIVE_PLAN: "No dispatch plan has been assigned yet.",
  VEHICLE_UNAVAILABLE: "This vehicle is unavailable.",
  INVALID_CURRENT_STOP: "This order is not at the current stop.",
  URGENT_ORDER_NOT_CANCELLABLE: "The urgent order cannot be cancelled after pickup. Reset the demo to start a new round.",
  INVALID_ORDER_STATE: "This order cannot be updated in its current state."
};
function toMessage(error: unknown): string {
  if (error instanceof Member3Error) return `${error.code}: ${error.message}${error.requestId ? ` (request ${error.requestId})` : ""}`;
  if (error instanceof DispatchError) return errorMessages[error.code];
  if (error instanceof DispatchConfigurationError) return error.message;
  return "The dispatch action could not be completed.";
}

export function DispatchProvider({ api: injectedApi, children }: { api?: DispatchApi; children: ReactNode }) {
  const [api, setApi] = useState<DispatchApi | undefined>(injectedApi);
  const [snapshot, setSnapshot] = useState<DispatchSnapshot | null>(null);
  const [pending, setPending] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const epoch = useRef(0);
  useEffect(() => {
    const current = ++epoch.current;
    let active = true, unsubscribe: (() => void) | undefined;
    const valid = () => active && epoch.current === current;
    setApi(injectedApi); setSnapshot(null); setPending(true); setError(null);
    void (async () => {
      try {
        const initialized = await (injectedApi ? Promise.resolve(injectedApi) : getDefaultApi());
        if (!valid()) return;
        setApi(initialized);
        unsubscribe = initialized.subscribe(next => {
          if (!valid()) return;
          setSnapshot(next);
          if (next.backend) setError(next.backend.error ? `${next.backend.error.code}: ${next.backend.error.message}` : null);
        });
        const next = await initialized.getSnapshot();
        if (valid()) { setSnapshot(next); if (next.backend?.error) setError(`${next.backend.error.code}: ${next.backend.error.message}`); }
      } catch (reason) { if (valid()) setError(toMessage(reason)); }
      finally { if (valid()) setPending(false); }
    })();
    return () => { active = false; ++epoch.current; unsubscribe?.(); };
  }, [injectedApi]);

  const value = useMemo<DispatchContextValue | null>(() => api ? ({
    api, snapshot, pending, error,
    async invoke(operation) {
      const current = epoch.current;
      setPending(true); setError(null);
      try { const next = await operation(); if (epoch.current === current) setSnapshot(next); }
      catch (reason) { if (epoch.current === current) setError(toMessage(reason)); }
      finally { if (epoch.current === current) setPending(false); }
    }
  }) : null, [api, error, pending, snapshot]);
  if (!value) return <main><span role={error ? "alert" : "status"}>{error ?? "Starting dispatch workspace…"}</span></main>;
  return <DispatchContext.Provider value={value}>{children}</DispatchContext.Provider>;
}
export function useDispatch() {
  const context = useContext(DispatchContext);
  if (!context) throw new Error("useDispatch requires DispatchProvider.");
  return context;
}

export function useDispatchPresentation() {
  const { api, snapshot } = useDispatch();
  return dispatchPresentation(api, snapshot);
}
