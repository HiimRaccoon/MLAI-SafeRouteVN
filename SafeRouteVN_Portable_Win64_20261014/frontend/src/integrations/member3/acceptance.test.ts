import { describe, expect, it } from "vitest";
import { Member3Client } from "./client";
import { basis } from "./testFixtures";
import { acceptedView } from "./acceptedTestFixture";

function receipt() {
  return { schema_version: "saferoute-m3-plan-acceptance/1", acceptance_id: "accept-test", session_id: basis.session_id, job_id: "job-return", status: "ACCEPTED",
    input_basis: { ...basis }, basis: { ...basis, generation: "1" }, recorded_at: "2026-10-07T12:00:00+00:00",
    links: { state: `/api/sessions/${basis.session_id}/state`, job: `/api/sessions/${basis.session_id}/jobs/job-return` } };
}
function client(data: unknown) {
  return new Member3Client({ token: () => "test-only-bearer", fetch: async () => new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", status: "OK", request_id: "trace", data, diagnostics: [] })) });
}
describe("acceptance HTTP boundary", () => {
  it("accepts historical receipts with a newer execution basis and preserves exact revision strings", async () => {
    const r = receipt(); r.input_basis.head_version = r.basis.head_version = "9007199254740993";
    const view = acceptedView(); view.basis = { ...r.basis, head_version: "9007199254740995", generation: "4" };
    const response = await client({ schema_version: "saferoute-m3-acceptance-view/1", receipt: r, execution_view: view }).accept(basis.session_id, "job-return", {
      request_id: "same-intent", expected_revision: { head_version: "9007199254740993", generation: "0" } });
    expect(response.receipt.input_basis.head_version).toBe("9007199254740993"); expect(response.execution_view.basis.generation).toBe("4");
  });
  it.each(["session", "job", "generation", "hash", "timestamp", "links", "execution-session", "geometry"])("rejects tampered %s in Accept responses", async kind => {
    const r = receipt(); const view = acceptedView();
    if (kind === "session") r.session_id = "other-session";
    if (kind === "job") r.job_id = "other-job";
    if (kind === "generation") r.basis.generation = "2";
    if (kind === "hash") r.basis.head_sha256 = "f".repeat(64);
    if (kind === "timestamp") r.recorded_at = "invalid";
    if (kind === "links") r.links.state = "/other";
    if (kind === "execution-session") view.basis = { ...basis, session_id: "other-session" };
    if (kind === "geometry") {
      const routes = view.accepted_trajectory!.vehicle_routes as Array<{ actions: Record<string, unknown>[] }>;
      routes[0].actions[0].geometry = [[181, 91], [182, 92]];
    }
    await expect(client({ schema_version: "saferoute-m3-acceptance-view/1", receipt: r, execution_view: view }).accept(basis.session_id, "job-return", {
      request_id: "intent", expected_revision: { head_version: "0", generation: "0" } })).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
  });
  it("rejects cross-session and duplicate history entries instead of hydrating records", async () => {
    for (const entries of [[{ ...receipt(), session_id: "other" }], [receipt(), receipt()], [{ ...receipt(), status: "PENDING" }]]) {
      await expect(client({ schema_version: "saferoute-m3-acceptance-audit/1", session_id: basis.session_id, acceptances: entries }).acceptances(basis.session_id)).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
    }
  });
});
