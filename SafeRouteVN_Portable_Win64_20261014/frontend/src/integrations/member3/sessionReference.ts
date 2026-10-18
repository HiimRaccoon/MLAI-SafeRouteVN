import type { M3Session, M3Basis } from "./types";
import type { ScenarioId } from "../../shared/types/scenario";
import { parseBasis } from "./revision";
import { Member3Error } from "./errors";
export interface PointerStorage { getItem(key: string): string | null; setItem(key: string, value: string): void }
export interface SessionPointer { schemaVersion: 1; session?: M3Session; pending?: { scenarioId: ScenarioId; requestId: string; replaceSessionId?:string }; comparison?: { id: string; inputBasis: M3Basis }; previewJobId?: string }
export interface ReferenceOptions { baseUrl: string; storage?: PointerStorage; subscribeChanges?(callback: (key: string | null) => void): () => void }
export function createSessionReference(options: ReferenceOptions) {
  const key = `saferoute.member3.session.v1:${options.baseUrl}`;
  function sanitize(raw: unknown): SessionPointer | null {
    const value = raw as SessionPointer;
    if (!value || value.schemaVersion !== 1) return null;
    const pointer: SessionPointer = { schemaVersion: 1 };
    if (value.session) {
      if (!validSession(value.session)) return null;
      const s = value.session;
      pointer.session = { session_id: s.session_id, scenario_id: s.scenario_id, build_sha256: s.build_sha256, catalog_sha256: s.catalog_sha256, fixture_sha256: s.fixture_sha256, ...(s.run_id ? {run_id:s.run_id} : {}) };
    }
    if (value.pending && /^S[0-4]$/.test(value.pending.scenarioId) && identifier(value.pending.requestId)) {
      if (value.pending.replaceSessionId !== undefined && !identifier(value.pending.replaceSessionId)) return null;
      pointer.pending = { scenarioId: value.pending.scenarioId, requestId: value.pending.requestId,
        ...(value.pending.replaceSessionId ? {replaceSessionId:value.pending.replaceSessionId} : {}) };
    }
    if (value.comparison && pointer.session && identifier(value.comparison.id)) {
      const b = parseBasis(value.comparison.inputBasis);
      if (b.session_id !== pointer.session.session_id || b.build_sha256 !== pointer.session.build_sha256) return null;
      pointer.comparison = { id: value.comparison.id, inputBasis: { session_id: b.session_id, build_sha256: b.build_sha256, root_sha256: b.root_sha256, head_sha256: b.head_sha256, head_version: b.head_version, generation: b.generation, source_sha256: b.source_sha256, context_version: b.context_version, overlay_sha256: b.overlay_sha256 } };
    }
    if (identifier(value.previewJobId)) pointer.previewJobId = value.previewJobId;
    return pointer;
  }
  function read(): SessionPointer | null { try { return sanitize(JSON.parse(options.storage?.getItem(key) ?? "null")); } catch { return null; } }
  return { key, read,
    write(pointer: SessionPointer) {
      const clean = sanitize(pointer);
      if (!clean || !options.storage) throw new Member3Error("COMMAND_STORAGE_UNAVAILABLE", "Cannot persist session reference.");
      try { options.storage.setItem(key, JSON.stringify(clean)); } catch { throw new Member3Error("COMMAND_STORAGE_UNAVAILABLE", "Cannot persist session reference."); }
    },
    subscribe(callback: (pointer: SessionPointer | null) => void) {
      const notify = (changed: string | null) => { if (changed === key || changed === null) callback(read()); };
      if (options.subscribeChanges) return options.subscribeChanges(notify);
      if (typeof window === "undefined") return () => {};
      const listener = (event: StorageEvent) => { if (!event.storageArea || event.storageArea === options.storage) notify(event.key); };
      window.addEventListener("storage", listener); return () => window.removeEventListener("storage", listener);
    }
  };
}
function identifier(value: unknown): value is string { return typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$/.test(value); }
export function validSession(value: unknown): value is M3Session {
  const s = value as M3Session;
  return Boolean(s && identifier(s.session_id) && (s.run_id ? /^S[0-8]$/ : /^S[0-4]$/).test(s.scenario_id) && (s.run_id === undefined || /^run-[a-f0-9]{32}$/.test(s.run_id)) && [s.build_sha256, s.catalog_sha256, s.fixture_sha256].every(v => typeof v === "string" && /^[a-f0-9]{64}$/.test(v)));
}
