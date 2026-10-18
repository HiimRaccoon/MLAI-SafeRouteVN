import type { DispatchSnapshot } from "../../shared/types/dispatch";
import type { M3ComparisonView } from "./types";
import { Member3Error } from "./errors";
import { terminalComparison } from "./jobViewAdapter";

export interface PollCoordinator { watchWorld(sessionId: string): void; watchComparison(sessionId: string, comparisonId: string): void; refresh(): Promise<void>; stop(): void }
export interface PollOptions {
  readWorld(sid: string, signal: AbortSignal): Promise<DispatchSnapshot>;
  readComparison(sid: string, cid: string, signal: AbortSignal): Promise<M3ComparisonView>;
  publishWorld(snapshot: DispatchSnapshot): void; publishComparison(view: M3ComparisonView): void; onError(error: unknown): void;
  now(): number; schedule(callback: () => void, delayMs: number): () => void;
  isVisible(): boolean; subscribeVisibility(callback: () => void): () => void; comparisonDeadlineMs?: number; worldIntervalMs?: number;
}
export function transientM3Error(error: unknown): boolean {
  return error instanceof Member3Error && ["NETWORK_ERROR", "RUNTIME_BUSY", "RUNTIME_TIMEOUT", "REQUEST_IN_PROGRESS", "REQUEST_LEASE_LOST", "METADATA_UNAVAILABLE", "QUEUE_UNAVAILABLE", "STATE_CHANGED", "SERVICE_UNAVAILABLE", "SESSION_RECOVERING", "BACKEND_NOT_READY", "ASSIGNMENT_UNAVAILABLE", "RATE_LIMITED"].includes(error.code);
}
interface Lane { cancel?: () => void; controller?: AbortController; promise?: Promise<void>; retries: number; epoch: number; halted: boolean }
export function createPollCoordinator(options: PollOptions): PollCoordinator {
  let session: string | undefined, comparisonId: string | undefined, deadline = 0, stopped = false;
  let unsubscribe: (() => void) | undefined;
  let cancelDeadline: (() => void) | undefined;
  const world: Lane = { retries: 0, epoch: 0, halted: false }, comparison: Lane = { retries: 0, epoch: 0, halted: false };
  function abort(lane: Lane) { lane.epoch++; lane.cancel?.(); lane.cancel = undefined; lane.controller?.abort(); }
  function attach() { stopped = false; unsubscribe ??= options.subscribeVisibility(visibility); }
  function armDeadline() {
    cancelDeadline?.(); cancelDeadline = undefined;
    if (stopped || !comparisonId || comparison.halted || !options.isVisible()) return;
    cancelDeadline = options.schedule(() => {
      cancelDeadline = undefined; abort(comparison); comparison.halted = true;
      options.onError(new Member3Error("COMPARISON_UNKNOWN", "Comparison watch deadline reached; server outcome remains unknown. Refresh to resume read-only monitoring."));
    }, Math.max(0, deadline - options.now()));
  }
  function arm(lane: Lane, ms: number) {
    if (stopped || !options.isVisible() || lane.halted || lane.promise || lane.cancel || !session || lane === comparison && !comparisonId) return;
    lane.cancel = options.schedule(() => { lane.cancel = undefined; void run(lane); }, ms);
  }
  function run(lane: Lane): Promise<void> {
    if (lane.promise) return lane.promise;
    if (stopped || !options.isVisible() || !session || lane === comparison && !comparisonId) return Promise.resolve();
    const sid = session, cid = comparisonId, epoch = lane.epoch, controller = new AbortController(); lane.controller = controller;
    if (lane === comparison && options.now() >= deadline) {
      lane.halted = true; options.onError(new Member3Error("COMPARISON_UNKNOWN", "Comparison watch deadline reached; server outcome remains unknown. Refresh to resume read-only monitoring.")); return Promise.resolve();
    }
    let nextDelay = lane === world ? 2500 : 1000;
    const valid = () => !stopped && epoch === lane.epoch && sid === session && !controller.signal.aborted;
    lane.promise = (async () => {
      try {
        if (lane === world) {
          const snapshot = await options.readWorld(sid, controller.signal);
          if (snapshot.backend) {
            const playback = snapshot.backend.playback;
            // Idle worlds and pending optimization do not move vehicles. Keep
            // observing them without competing with the solver every 2.5 s.
            nextDelay = options.worldIntervalMs ?? (playback && (!playback.paused || playback.in_flight) ? 2500 : comparisonId ? 15000 : 10000);
          }
          if (valid()) options.publishWorld(snapshot);
        } else {
          const view = await options.readComparison(sid, cid!, controller.signal);
          if (valid() && cid === comparisonId) { options.publishComparison(view); if (terminalComparison(view)) { comparisonId = undefined; lane.halted = true; cancelDeadline?.(); cancelDeadline = undefined; } }
        }
        if (valid()) lane.retries = 0;
      } catch (error) {
        if (valid()) {
          options.onError(error);
          // Observations can retry indefinitely; a network outage must not disable
          // route updates for the rest of the driver's shift. Mutations are separate.
          if (transientM3Error(error) && (lane === world || lane.retries < 3)) {
            nextDelay = Math.min(15000, 1000 * 2 ** Math.min(lane.retries, 4));
            lane.retries = Math.min(lane.retries + 1, 4);
          }
          else { lane.halted = true; if (lane === comparison) { cancelDeadline?.(); cancelDeadline = undefined; } }
        }
      } finally {
        lane.promise = undefined; lane.controller = undefined;
        // A stale read must settle before the next read of this kind starts.
        if (epoch !== lane.epoch) arm(lane, 0); else arm(lane, nextDelay);
      }
    })(); return lane.promise;
  }
  function visibility() {
    abort(world); abort(comparison);
    cancelDeadline?.(); cancelDeadline = undefined;
    if (options.isVisible()) { armDeadline(); arm(world, 0); arm(comparison, 0); }
  }
  return {
    watchWorld(sid) {
      attach(); if (session !== sid) { abort(world); abort(comparison); cancelDeadline?.(); cancelDeadline = undefined; session = sid; comparisonId = undefined; world.halted = false; world.retries = 0; }
      if (world.halted) { world.halted = false; world.retries = 0; }
      arm(world, 2500);
    },
    watchComparison(sid, cid) {
      attach(); if (session !== sid) { abort(world); abort(comparison); session = sid; world.halted = false; }
      if (comparisonId !== cid || comparison.halted) { abort(comparison); comparisonId = cid; deadline = options.now() + (options.comparisonDeadlineMs ?? 600000); comparison.retries = 0; comparison.halted = false; }
      armDeadline();
      arm(comparison, 1000);
    },
    async refresh() {
      attach(); abort(world); abort(comparison);
      await Promise.all([world.promise, comparison.promise]);
      world.halted = comparison.halted = false; world.retries = comparison.retries = 0;
      deadline = options.now() + (options.comparisonDeadlineMs ?? 600000);
      armDeadline();
      await run(world); if (comparisonId) await run(comparison);
    },
    stop() { stopped = true; abort(world); abort(comparison); cancelDeadline?.(); cancelDeadline = undefined; unsubscribe?.(); unsubscribe = undefined; }
  };
}
