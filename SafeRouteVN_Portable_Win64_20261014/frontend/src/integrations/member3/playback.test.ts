import { expect, it } from "vitest";
import { Member3Client } from "./client";
import { acceptedView } from "./acceptedTestFixture";
import { basis } from "./testFixtures";
import { playbackComplete } from "./playback";

it("uses exact server end time after a manual stop without losing submillisecond return time", () => {
  const view = acceptedView(), end = "2026-09-27T22:01:04.795337+07:00";
  const playback = { reason: "MANUAL_RESET", end_time: end, execution_view: view };
  view.current_time = "2026-09-27T15:01:04.795000Z";
  expect(playbackComplete(playback)).toBe(false);
  view.current_time = "2026-09-27T15:01:04.795337Z";
  expect(playbackComplete(playback)).toBe(true);
});
it('keeps the final arrival actionable until delivery is confirmed',()=>{
  const view=acceptedView();view.delivery_day={} as any;view.vehicles[0].awaiting_delivery='O1';
  const playback={reason:'PLAN_COMPLETE',end_time:view.current_time,execution_view:view};
  expect(playbackComplete(playback)).toBe(false);
  view.vehicles[0].awaiting_delivery=null;
  expect(playbackComplete(playback)).toBe(true);
});

export function controller(overrides: Record<string, unknown> = {}) {
  return { mode: "STEP", paused: true, fully_paused: true, in_flight: false, reason: "USER_PAUSE", speed: 1,
    controller_revision: "9007199254740993", next_tick_at: null, end_time: null, step_seconds: 60,
    cadence: "DISCRETE_BEST_EFFORT", base_tick_interval_seconds: 1, catch_up: false, auto_apply_event: false,
    auto_accept_plan: false, execution_mode: "SIMULATED_REPLAY", real_world_observation: false, ...overrides };
}
function client(data: unknown, captures: { path: string; body: unknown }[] = []) {
  return new Member3Client({ token: () => "unit-only", fetch: async (url, options) => {
    captures.push({ path: new URL(String(url)).pathname, body: options?.body ? JSON.parse(String(options.body)) : null });
    return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", status: "OK", request_id: "trace", data, diagnostics: [] }));
  } });
}
it("uses exact playback endpoint bodies and preserves a paused controller when speed changes", async () => {
  const captures: { path: string; body: unknown }[] = [];
  for (const operation of ["start", "speed", "pause"] as const) {
    const body = { request_id: `original-${operation}`, ...(operation === "pause" ? {} : { speed: 4 }), ...(operation === "start" ? { expected_revision: { head_version: "9007199254740993", generation: "0" } } : {}) };
    const c = controller();
    const data = { schema_version: "saferoute-m3-playback-control/1", controller: c, receipt: { schema_version: "saferoute-m3-playback-receipt/1", receipt_id: "playback-1", session_id: basis.session_id, operation, source: "M3_PLAYBACK_CONTROL", recorded_at: "2026-10-08T00:00:00Z", request_id: body.request_id, actor_id: "owner", controller: c } };
    const api = client(data, captures);
    const result = await Promise.resolve().then(() => operation === "start" ? api.startPlayback(basis.session_id, body as Parameters<typeof api.startPlayback>[1]) : operation === "speed" ? api.setPlaybackSpeed(basis.session_id, { request_id: body.request_id, speed: 4 }) : api.pausePlayback(basis.session_id, body.request_id)).catch(e => e);
    expect(result).toEqual(data);
    expect(captures.at(-1)).toEqual({ path: `/api/sessions/${basis.session_id}/replay/${operation === "pause" ? "playback/pause" : operation}`, body });
  }
});
it("reads paused but reserved tick without claiming fully paused", async () => {
  const data = { schema_version: "saferoute-m3-playback-controller/1", ...controller({ fully_paused: false, in_flight: true }), execution_view: acceptedView() };
  expect(await Promise.resolve().then(() => client(data).playback(basis.session_id)).catch(e => e)).toEqual(data);
});
it.each([{ in_flight: true }, { speed: 3 }, { controller_revision: "01" }, { auto_apply_event: true }, { execution_mode: "GPS" }])("rejects invalid controller contract %j", async invalid => {
  const data = { schema_version: "saferoute-m3-playback-controller/1", ...controller(invalid), execution_view: acceptedView() };
  expect(await Promise.resolve().then(() => client(data).playback(basis.session_id)).catch(e => e)).toMatchObject({ code: "INVALID_RESPONSE" });
});
it("resets to the receipt-bound new session while retaining source history identity", async () => {
  const next = { session_id: "new-owned", scenario_id: "S1", build_sha256: basis.build_sha256, catalog_sha256: "c".repeat(64), fixture_sha256: "f".repeat(64) };
  const view = acceptedView(); view.basis = { ...basis, session_id: next.session_id }; view.active_job_id = null; view.accepted_trajectory = null;
  const data = { schema_version: "saferoute-m3-replay-reset/1", source_session_id: basis.session_id, session: next, execution_view: view,
    receipt: { schema_version: "saferoute-m3-replay-receipt/1", mutation_id: "reset-1", session_id: basis.session_id, operation: "reset", status: "RESET", source: "M3_NEW_SESSION", input_basis: basis, basis: view.basis, new_session_id: next.session_id, new_session: next, recorded_at: "2026-10-08T00:00:00Z", links: { state: `/api/sessions/${next.session_id}/state`, history: `/api/sessions/${basis.session_id}/replay/history` } } };
  const captures: { path: string; body: unknown }[] = [];
  expect(await Promise.resolve().then(() => client(data, captures).resetReplay(basis.session_id, "original-reset")).catch(e => e)).toEqual(data);
  expect(captures).toEqual([{ path: `/api/sessions/${basis.session_id}/replay/reset`, body: { request_id: "original-reset" } }]);
});
