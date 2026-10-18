export const REPLAY_SPEEDS = [1, 2, 4, 8, 16, 32, 64, 128] as const;
export type ReplaySpeed = typeof REPLAY_SPEEDS[number];
