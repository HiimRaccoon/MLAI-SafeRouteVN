import { describe, expect, it, vi } from "vitest";
import { Member3Client } from "./client";
import { Member3Error } from "./errors";
import { basis, comparison, job } from "./testFixtures";

function response(data: unknown, status = 200, diagnostics: unknown[] = []) {
  return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", request_id: "trace-1",
    status: status < 400 ? "OK" : "ERROR", data, diagnostics }), { status });
}

describe("M3 HTTP boundary", () => {
  it('uses the timeout reason when a response stream reports a generic abort',async()=>{
    const timeout=new AbortController();
    const clock=vi.spyOn(AbortSignal,'timeout').mockReturnValue(timeout.signal);
    try {
      const client=new Member3Client({token:()=> 'unit-only',fetch:async()=>{
        const r=response({});
        r.json=async()=>{timeout.abort(new DOMException('Timed out','TimeoutError'));throw new DOMException('Aborted body','AbortError');};
        return r;
      }});
      await expect(client.identity()).rejects.toMatchObject({code:'RUNTIME_TIMEOUT'});
    } finally { clock.mockRestore(); }
  });
  it('classifies a timeout while downloading the body as retryable',async()=>{
    const fetcher: typeof fetch=async()=>{
      const r=response({});
      r.json=async()=>{throw new DOMException('body timed out','TimeoutError');};
      return r;
    };
    const client=new Member3Client({token:()=> 'unit-only',fetch:fetcher});
    await expect(client.identity()).rejects.toMatchObject({code:'RUNTIME_TIMEOUT'});
  });
  it('classifies an HTML upstream outage as retryable without accepting malformed successful JSON',async()=>{
    const client=new Member3Client({token:()=> 'unit-only',fetch:async()=>new Response('<h1>Unavailable</h1>',{status:502})});
    await expect(client.identity()).rejects.toMatchObject({code:'SERVICE_UNAVAILABLE'});
  });
  it('distinguishes a timed-out server operation from an unreachable server', async () => {
    const client = new Member3Client({token:()=> 'unit-only', fetch:async()=>{throw new DOMException('Timed out','TimeoutError');}});
    await expect(client.scenarios()).rejects.toMatchObject({code:'RUNTIME_TIMEOUT'});
  });
  it("compare_submits_current_revision_once and validates public reads", async () => {
    const receipt = { schema_version: "saferoute-m3-profile-submission/1", session_id: basis.session_id, comparison_id: "comparison-test", mode: "NEW_BATCH", input_basis: basis, profiles: ["FASTEST", "BALANCED", "SAFER"], links: { poll: "/poll", cancel: "/cancel" } };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response(receipt, 202)).mockResolvedValueOnce(response(comparison())).mockResolvedValueOnce(response(job()));
    const client = new Member3Client({ token: () => "test-only-bearer", fetch: fetcher });
    const body = { request_id: "intent-1", expected_revision: { head_version: "9007199254740993", generation: "0" } };
    await client.compareProfiles(basis.session_id, body);
    expect(fetcher.mock.calls[0][0]).toBe("http://127.0.0.1:8000/api/sessions/session-test/profiles/compare");
    expect(JSON.parse(fetcher.mock.calls[0][1]!.body as string)).toEqual(body);
    expect((await client.comparison(basis.session_id, "comparison-test")).status).toBe("COMPLETED");
    expect((await client.job(basis.session_id, "job-FASTEST")).plan_available).toBe(true);
  });
  it("propagates abort without reporting server cancellation", async () => {
    const fetcher: typeof fetch = async (_url, options) => new Promise((_resolve, reject) => options?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError"))));
    const client = new Member3Client({ token: () => "test-only-bearer", fetch: fetcher });
    const controller = new AbortController();
    const pending = client.state("session-test", controller.signal); controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });
  it("unwraps data and sends bearer only to the configured server", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({ scenarios: [{ scenario_id: "S1" }] }));
    const client = new Member3Client({ baseUrl: "http://127.0.0.1:8000/", token: () => "test-only-bearer", fetch: fetcher });
    expect(await client.scenarios()).toEqual({ scenarios: [{ scenario_id: "S1" }] });
    expect(fetcher).toHaveBeenCalledWith("http://127.0.0.1:8000/api/scenarios", expect.objectContaining({
      headers: expect.objectContaining({ Authorization: "Bearer test-only-bearer" }), redirect: "error", cache: "no-store"
    }));
  });

  it("sends the same request ID when a load is retried", async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => response({ session: { session_id: "server-session" } }, 201));
    const client = new Member3Client({ token: () => "test-only-bearer", fetch: fetcher });
    await client.loadScenario("S1", "load-stable-id");
    await client.loadScenario("S1", "load-stable-id");
    expect(fetcher.mock.calls.map(([, options]) => options?.body)).toEqual([
      '{"request_id":"load-stable-id"}', '{"request_id":"load-stable-id"}'
    ]);
  });

  it("exposes backend code, trace and diagnostics without swallowing 401", async () => {
    const diagnostics = [{ code: "UNAUTHORIZED", path: "authentication", message: "Valid bearer token required", severity: "ERROR" }];
    const client = new Member3Client({ token: () => "test-only-bearer", fetch: async () => response(null, 401, diagnostics) });
    await expect(client.scenarios()).rejects.toMatchObject({ code: "UNAUTHORIZED", status: 401, requestId: "trace-1", diagnostics });
  });

  it("allows anonymous readiness but refuses authenticated reads without a token", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({ ready: true }));
    const client = new Member3Client({ token: () => null, fetch: fetcher });
    expect(await client.ready()).toEqual({ ready: true });
    await expect(client.scenarios()).rejects.toMatchObject({ code: "AUTH_REQUIRED" });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("rejects non-M3 responses and maps network errors without exposing credentials", async () => {
    const invalid = new Member3Client({ token: () => "test-only-bearer", fetch: async () => new Response('{"data":{}}') });
    await expect(invalid.scenarios()).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
    const offline = new Member3Client({ token: () => "secret", fetch: async () => { throw new Error("secret socket detail"); } });
    await expect(offline.scenarios()).rejects.toBeInstanceOf(Member3Error);
    await expect(offline.scenarios()).rejects.toMatchObject({ code: "NETWORK_ERROR", message: expect.not.stringContaining("secret") });
  });
});
