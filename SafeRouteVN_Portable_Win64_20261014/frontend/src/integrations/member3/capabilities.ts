export interface DispatchCapabilities { mode: "backend" | "mock"; optimize: boolean; accept: boolean; applyEvent: boolean; replay: boolean; driverActions: boolean; forecastGeometry: boolean }
// Server ownership is enforced by HTTP; do not infer roles from token contents.
export function phase2Capabilities(fresh: boolean): DispatchCapabilities {
  return { mode: "backend", optimize: fresh, accept: false, applyEvent: false, replay: false, driverActions: false, forecastGeometry: false };
}
export function canControlPlayback(snapshot: import("../../shared/types/dispatch").DispatchSnapshot): boolean {
  const backend = snapshot.backend;
  if (!backend?.playback || backend.mutationPending || backend.basis.session_id !== snapshot.decisionState.sessionId) return false;
  return !backend.stale || !backend.error && backend.playbackConverging === true || ["STATE_CHANGED", "RUNTIME_BUSY", "RUNTIME_TIMEOUT", "NETWORK_ERROR", "METADATA_UNAVAILABLE", "REPLAY_CONVERGING"].includes(backend.error?.code ?? "");
}
