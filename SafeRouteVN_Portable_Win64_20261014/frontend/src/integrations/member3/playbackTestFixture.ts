import type { M3PlaybackState } from "./types";
export function controller(overrides: Partial<M3PlaybackState> = {}): M3PlaybackState {
  return { mode: "STEP", paused: true, fully_paused: true, in_flight: false, reason: "USER_PAUSE", speed: 1,
    controller_revision: "9007199254740993", next_tick_at: null, end_time: null, step_seconds: 60,
    cadence: "DISCRETE_BEST_EFFORT", base_tick_interval_seconds: 1, catch_up: false, auto_apply_event: false,
    auto_accept_plan: false, execution_mode: "SIMULATED_REPLAY", real_world_observation: false, ...overrides };
}
