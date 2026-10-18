import { describe, expect, it } from "vitest";
import { expectedRevision, parseBasis, sameBasis, isAcceptableJob } from "./revision";
import { basis, job } from "./testFixtures";

describe("server revision", () => {
  it("preserves_int64_revision_strings", () => {
    const b = { ...basis, head_version: "9007199254740993", generation: "9223372036854775807" };
    expect(JSON.stringify(expectedRevision(parseBasis(b)))).toBe('{"head_version":"9007199254740993","generation":"9223372036854775807"}');
    for (const value of ["01", 1, "-1", "9223372036854775808"]) expect(() => parseBasis({ ...b, generation: value })).toThrow();
  });
  it("full_basis_detects_generation_and_hash_changes", () => {
    expect(sameBasis(basis, { ...basis })).toBe(true);
    for (const field of Object.keys(basis)) expect(sameBasis(basis, { ...basis, [field]: field === "generation" ? "1" : "changed" })).toBe(false);
  });
  it("requires a current certified witness for accept", () => {
    expect(isAcceptableJob(job(), basis)).toBe(true);
    expect(isAcceptableJob(job(), { ...basis, generation: "1" })).toBe(false);
    expect(isAcceptableJob({ ...job(), plan_available: false }, basis)).toBe(false);
  });
});
