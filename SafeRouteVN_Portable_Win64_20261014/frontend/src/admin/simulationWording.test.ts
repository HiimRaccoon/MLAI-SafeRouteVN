import { describe, expect, it } from "vitest";
import { LABELS } from "./admin.labels";

describe("simulation wording", () => {
  it("labels replay and exposure as demo/proxy information", () => {
    expect(LABELS.liveDispatchTitle).toMatch(/demo/i);
    expect(LABELS.liveDispatchSubtitle).toMatch(/simulat/i);
    expect(LABELS.kpiSafetyExposure).toMatch(/proxy/i);
    expect(LABELS.metricRiskExposure).toMatch(/proxy/i);
    expect(LABELS.metricRisk).toMatch(/proxy/i);
    expect(LABELS.profileDescriptions.SAFER).not.toMatch(/accident probability/i);
  });
});
