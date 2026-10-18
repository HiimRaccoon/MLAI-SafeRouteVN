import { afterEach, describe, expect, it, vi } from "vitest";
import { createPollCoordinator } from "./polling";
import { comparison } from "./testFixtures";
import type { M3ComparisonView } from "./types";
import type { DispatchSnapshot } from "../../shared/types/dispatch";
import { Member3Error } from "./errors";

function setup(worldIntervalMs?: number) {
  vi.useFakeTimers(); let visible = true; let visibility = () => {};
  const readWorld = vi.fn(async () => ({ decisionState: { sessionId: "session-test" } } as DispatchSnapshot));
  const readComparison = vi.fn(async () => ({ ...comparison(), status: "RUNNING", outcome: null } as M3ComparisonView));
  const publishWorld = vi.fn(), publishComparison = vi.fn(), onError = vi.fn();
  const coordinator = createPollCoordinator({ readWorld, readComparison, publishWorld, publishComparison, onError, now: Date.now,
    worldIntervalMs,
    schedule: (cb, ms) => { const t = setTimeout(cb, ms); return () => clearTimeout(t); }, isVisible: () => visible,
    subscribeVisibility: cb => { visibility = cb; return () => { visibility = () => {}; }; } });
  return { coordinator, readWorld, readComparison, publishWorld, publishComparison, onError, hide: () => { visible = false; visibility(); }, show: () => { visible = true; visibility(); } };
}
afterEach(() => { vi.useRealTimers(); });
describe("single coordinator", () => {
  it('observes an accepted route within two seconds of the previous idle read for remote drivers', async () => {
    const s = setup(1500);
    const old = {backend:{playback:{paused:true,in_flight:false}},decisionState:{sessionId:'test'}} as DispatchSnapshot;
    const next = {...old, planState:{activeAcceptedPlanId:'new-route'}} as DispatchSnapshot;
    s.readWorld.mockResolvedValue(old);
    s.coordinator.watchWorld('test'); await vi.advanceTimersByTimeAsync(2500);
    s.readWorld.mockResolvedValue(next);
    await vi.advanceTimersByTimeAsync(2000);
    expect(s.publishWorld).toHaveBeenLastCalledWith(next); s.coordinator.stop();
  });
  it.each(['NETWORK_ERROR','SERVICE_UNAVAILABLE','SESSION_RECOVERING','ASSIGNMENT_UNAVAILABLE','RATE_LIMITED'])('keeps observing after a prolonged %s outage', async code => {
    const s=setup();
    s.readWorld.mockRejectedValue(new Member3Error(code,'offline'));
    s.coordinator.watchWorld('test'); await vi.advanceTimersByTimeAsync(60000);
    const recovered={decisionState:{sessionId:'recovered'}} as DispatchSnapshot;
    s.readWorld.mockResolvedValue(recovered);
    await vi.advanceTimersByTimeAsync(15000);
    expect(s.publishWorld).toHaveBeenLastCalledWith(recovered); s.coordinator.stop();
  });
  it('foreground retries a transient world outage immediately but never revives a forbidden read', async () => {
    const s=setup();
    s.readWorld.mockRejectedValue(new Member3Error('NETWORK_ERROR','offline'));
    s.coordinator.watchWorld('test'); await vi.advanceTimersByTimeAsync(60000);
    const recovered={decisionState:{sessionId:'recovered'}} as DispatchSnapshot;
    s.readWorld.mockResolvedValue(recovered); s.show(); await vi.advanceTimersByTimeAsync(0);
    expect(s.publishWorld).toHaveBeenLastCalledWith(recovered);
    s.readWorld.mockRejectedValue(new Member3Error('FORBIDDEN','denied',403));
    await vi.advanceTimersByTimeAsync(2500);
    s.publishWorld.mockClear(); s.readWorld.mockResolvedValue(recovered);
    s.show(); await vi.advanceTimersByTimeAsync(60000);
    expect(s.publishWorld).not.toHaveBeenCalled(); s.coordinator.stop();
  });
  it.each([[true, false, false, 10000], [true, false, true, 15000], [false, false, false, 2500], [true, true, false, 2500]])(
    'uses bounded world polling for paused=%s inFlight=%s comparing=%s', async (paused,inFlight,comparing,interval) => {
      const s=setup();
      s.readWorld.mockResolvedValue({backend:{playback:{paused,in_flight:inFlight}}} as unknown as DispatchSnapshot);
      s.coordinator.watchWorld('session-test');
      if(comparing)s.coordinator.watchComparison('session-test','comparison-test');
      await vi.advanceTimersByTimeAsync(2500);
      expect(s.readWorld).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(Number(interval)-1);
      expect(s.readWorld).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(s.readWorld).toHaveBeenCalledTimes(2);
      s.coordinator.stop();
    }
  );
  it("polls_without_overlap_and_stops_terminal", async () => {
    const s = setup(); let release!: (v: M3ComparisonView) => void;
    s.readComparison.mockImplementationOnce(() => new Promise(resolve => { release = resolve; }));
    s.coordinator.watchWorld("session-test"); s.coordinator.watchComparison("session-test", "comparison-test");
    await vi.advanceTimersByTimeAsync(1000); expect(s.readComparison).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(7000); expect(s.readComparison).toHaveBeenCalledTimes(1);
    release(comparison() as M3ComparisonView); await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(10000); expect(s.readComparison).toHaveBeenCalledTimes(1);
    expect(s.readWorld.mock.calls.length).toBeGreaterThan(1); s.coordinator.stop();
  });
  it("aborts_stale_session_and_unmount and resumes read-only", async () => {
    const s = setup(); let release!: (v: DispatchSnapshot) => void; let signal!: AbortSignal;
    s.readWorld.mockImplementationOnce(async (_sid?: string, sig?: AbortSignal) => { signal = sig!; return new Promise(resolve => { release = resolve; }); });
    s.coordinator.watchWorld("session-test"); await vi.advanceTimersByTimeAsync(2500);
    s.hide(); expect(signal.aborted).toBe(true);
    release({ decisionState: { sessionId: "old" } } as DispatchSnapshot); await vi.advanceTimersByTimeAsync(0);
    expect(s.publishWorld).not.toHaveBeenCalled(); s.show(); await vi.advanceTimersByTimeAsync(0);
    expect(s.publishWorld).toHaveBeenCalledTimes(1); s.coordinator.stop();
    await vi.advanceTimersByTimeAsync(10000); expect(s.publishWorld).toHaveBeenCalledTimes(1);
  });
  it("bounded_retries_and_deadline", async () => {
    const s = setup(); s.readComparison.mockRejectedValue(new Member3Error("RUNTIME_BUSY", "Busy", 503));
    s.coordinator.watchComparison("session-test", "comparison-test");
    await vi.advanceTimersByTimeAsync(1000 + 1000 + 2000 + 4000);
    expect(s.readComparison).toHaveBeenCalledTimes(4);
    await vi.advanceTimersByTimeAsync(20000); expect(s.readComparison).toHaveBeenCalledTimes(4); s.coordinator.stop();
    const d = setup(); d.coordinator.watchComparison("session-test", "comparison-test");
    await vi.advanceTimersByTimeAsync(600001);
    expect(d.onError).toHaveBeenCalledWith(expect.objectContaining({ code: "COMPARISON_UNKNOWN" }));
    expect(d.publishComparison.mock.calls.at(-1)?.[0].status).toBe("RUNNING"); d.coordinator.stop();
  });
  it.each(["UNAUTHORIZED", "FORBIDDEN", "INVALID_RESPONSE", "IDEMPOTENCY_CONFLICT"])("does not retry %s", async code => {
    const s = setup(); s.readComparison.mockRejectedValue(new Member3Error(code, "Stop", 409));
    s.coordinator.watchComparison("session-test", "comparison-test"); await vi.advanceTimersByTimeAsync(10000);
    expect(s.readComparison).toHaveBeenCalledTimes(1); s.coordinator.stop();
  });
  it("fences old-session comparison replies and restarts a stopped subscription", async () => {
    const s = setup(); let release!: (v: M3ComparisonView) => void;
    s.readComparison.mockImplementationOnce(() => new Promise(resolve => { release = resolve; }));
    s.coordinator.watchComparison("old-session", "old-comparison"); await vi.advanceTimersByTimeAsync(1000);
    s.coordinator.watchComparison("new-session", "new-comparison");
    release(comparison() as M3ComparisonView); await vi.advanceTimersByTimeAsync(0);
    expect(s.publishComparison).toHaveBeenCalledTimes(1);
    expect(s.readComparison.mock.calls.at(-1)?.slice(0, 2)).toEqual(["new-session", "new-comparison"]);
    s.coordinator.stop(); s.coordinator.watchWorld("new-session"); await vi.advanceTimersByTimeAsync(2500);
    expect(s.publishWorld).toHaveBeenCalled(); s.coordinator.stop();
  });
  it("manual refresh resumes a halted watch without a new submit", async () => {
    const s = setup(); s.readComparison.mockRejectedValueOnce(new Member3Error("UNAUTHORIZED", "Expired", 401));
    s.coordinator.watchComparison("session-test", "comparison-test"); await vi.advanceTimersByTimeAsync(1000);
    await s.coordinator.refresh(); expect(s.publishComparison).toHaveBeenCalledTimes(1); s.coordinator.stop();
  });
  it("reports unknown at deadline even when a comparison GET never settles", async () => {
    const s = setup(); let signal!: AbortSignal;
    s.readComparison.mockImplementation(async (_sid?: string, _cid?: string, sig?: AbortSignal) => { signal = sig!; return new Promise(() => {}); });
    s.coordinator.watchComparison("session-test", "comparison-test");
    await vi.advanceTimersByTimeAsync(600000);
    expect(signal.aborted).toBe(true);
    expect(s.onError).toHaveBeenCalledWith(expect.objectContaining({ code: "COMPARISON_UNKNOWN" })); s.coordinator.stop();
  });
});
