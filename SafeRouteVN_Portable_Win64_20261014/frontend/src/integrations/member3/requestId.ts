import { Member3Error } from "./errors";
export interface PendingCommand { requestId: string; sessionId: string | null; operation: string; resourceId?: string; body: Record<string, unknown>; inputBasis?: import("./types").M3Basis; eventType?: import("./types").M3EventType }
export interface PendingCommandStore { begin(intent: Omit<PendingCommand, "requestId">): PendingCommand; read(): PendingCommand | null; complete(requestId: string): void }
export interface CommandStorage { getItem(key: string): string | null; setItem(key: string, value: string): void }
export function createPendingCommandStore(storage: CommandStorage | undefined, origin: string): PendingCommandStore {
  const key = `saferoute.member3.command.v1:${origin}`;
  let pending: PendingCommand | null = null;
  try {
    const saved = JSON.parse(storage?.getItem(key) ?? "null");
    if (saved && typeof saved.requestId === "string" && saved.requestId.length > 0 && typeof saved.operation === "string" &&
        (saved.sessionId === null || typeof saved.sessionId === "string") && saved.body && typeof saved.body === "object" && !Array.isArray(saved.body)) pending = saved;
  } catch { /* Malformed intent is never world authority. */ }
  const clone = <T>(value: T): T => structuredClone(value);
  function persist(value: PendingCommand | null) {
    if (!storage) throw new Member3Error("COMMAND_STORAGE_UNAVAILABLE", "Durable storage is required before sending a backend command.");
    try { storage.setItem(key, JSON.stringify(value)); }
    catch { throw new Member3Error("COMMAND_STORAGE_UNAVAILABLE", "Cannot persist the backend command; restore browser storage and retry."); }
  }
  return {
    read: () => clone(pending),
    begin(intent) {
      if (pending) {
        const { requestId: _id, ...old } = pending;
        if (JSON.stringify(old) !== JSON.stringify(intent)) throw new Member3Error("PENDING_COMMAND", "Resolve the existing command before starting another intent.");
        persist(pending); return clone(pending);
      }
      const next = { ...clone(intent), requestId: `m4-${crypto.randomUUID()}` };
      persist(next); pending = next; return clone(pending);
    },
    complete(requestId) { if (pending?.requestId === requestId) { persist(null); pending = null; } }
  };
}
