export class DispatchConfigurationError extends Error {
  constructor(message: string) { super(message); this.name = "DispatchConfigurationError"; }
}
export type DispatchMode = "backend" | "mock";
export function dispatchMode(value?: string): DispatchMode {
  if (value === undefined) return "backend";
  if (value === "backend" || value === "mock") return value;
  throw new DispatchConfigurationError("VITE_DISPATCH_MODE must be backend or mock.");
}
