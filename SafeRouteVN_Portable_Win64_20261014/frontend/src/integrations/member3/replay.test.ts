import { expect, it } from "vitest";
import { Member3Client } from "./client";
import { basis } from "./testFixtures";
import { acceptedView } from "./acceptedTestFixture";
import type { M3ReplayReceipt } from "./types";

function receipt(): M3ReplayReceipt {
  return { schema_version: "saferoute-m3-replay-receipt/1", mutation_id: "mutation-1", session_id: basis.session_id,
    operation: "apply_event", status: "APPLIED", source: "M2_PUBLIC_SDK", input_basis: { ...basis, head_version: "9007199254740993" },
    basis: { ...basis, head_version: "9007199254740994", head_sha256: "e".repeat(64) },
    recorded_at: "2026-10-08T00:00:00+07:00", event_id: "wire-event", event_type: "URGENT_ORDER", event_sha256: "f".repeat(64),
    links: { state: `/api/sessions/${basis.session_id}/state`, history: `/api/sessions/${basis.session_id}/replay/history` } };
}
function client(data: unknown, captures: unknown[] = []) {
  return new Member3Client({ token: () => "unit-only-bearer", fetch: async (_url, options) => {
    if (options?.body) captures.push(JSON.parse(String(options.body)));
    return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", status: "OK", request_id: "trace-replay", data, diagnostics: [] }));
  } });
}
it("rehydrates confirmed event receipts with exact int64 strings and historical basis", async () => {
  const data = { schema_version: "saferoute-m3-replay-history/1", session_id: basis.session_id, history: [receipt()] };
  const result = await Promise.resolve().then(() => client(data).replayHistory(basis.session_id)).catch(error => error);
  expect(result).toEqual(data);
});
it('accepts audited automatic deliveries and rejects a changed custody identity',async()=>{
  const r=receipt();r.operation='deliver';r.status='DISPATCHED';r.vehicle_id='V2';r.order_id='O002';
  delete r.event_id;delete r.event_type;delete r.event_sha256;
  const data={schema_version:'saferoute-m3-replay-history/1',session_id:basis.session_id,history:[r]};
  expect(await client(data).replayHistory(basis.session_id)).toEqual(data);
  delete r.vehicle_id;
  await expect(client(data).replayHistory(basis.session_id)).rejects.toMatchObject({code:'INVALID_RESPONSE'});
});
it.each(["session", "generation", "head", "hash", "event", "status", "source", "links", "duplicate"])("rejects %s tampering in replay history", async tamper => {
  const r = receipt(); const data = { schema_version: "saferoute-m3-replay-history/1", session_id: basis.session_id, history: [r] };
  if (tamper === "session") r.session_id = "other";
  if (tamper === "generation") r.basis.generation = "1";
  if (tamper === "head") r.basis.head_version = "9007199254740995";
  if (tamper === "hash") r.basis.source_sha256 = "0".repeat(64);
  if (tamper === "event") r.event_type = "unknown" as typeof r.event_type;
  if (tamper === "status") r.status = "NOOP";
  if (tamper === "source") r.source = "M3_MANUAL_CONTROL";
  if (tamper === "links") r.links.history = "/other";
  if (tamper === "duplicate") data.history.push(r);
  const result = await Promise.resolve().then(() => client(data).replayHistory(basis.session_id)).catch(error => error);
  expect(result).toMatchObject({ code: "INVALID_RESPONSE" });
});
it("steps through typed HTTP using exact expected revision and server-resolved fresh view", async () => {
  const r = receipt(); r.operation = "advance"; r.status = "ADVANCED"; r.target_time = "2026-09-27T21:15:00+07:00";
  delete r.event_id; delete r.event_type; delete r.event_sha256;
  const view = acceptedView(); view.basis = { ...r.basis, head_version: "9007199254740997" };
  const data = { schema_version: "saferoute-m3-replay-view/1", receipt: r, execution_view: view };
  const captures: unknown[] = []; const body = { request_id: "step-original", expected_revision: { head_version: "9007199254740993", generation: "0" }, target_time: r.target_time };
  const result = await Promise.resolve().then(() => client(data, captures).step(basis.session_id, body)).catch(error => error);
  expect(result).toEqual(data); expect(captures).toEqual([body]);
});
