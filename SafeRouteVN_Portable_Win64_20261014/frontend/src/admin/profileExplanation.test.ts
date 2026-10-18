import { expect, it } from "vitest";
import { explainProfile, physicalRouteKey } from "./profileExplanation";
import { adaptJobForecast, parseJobForecast } from "../integrations/member3/forecastViewAdapter";
import { forecastView } from "../integrations/member3/forecastTestFixture";
import { basis } from "../integrations/member3/testFixtures";

function proposal() {
  return adaptJobForecast(parseJobForecast(forecastView()), { basis, jobId: "job-return", profile: "SAFER", comparisonId: "comparison", vehicleIds: ["V1"], deliveredPrefix: [] })!;
}
it("identifies equal physical routes independently of the job/profile label", () => {
  const safer=proposal(), fastest=structuredClone(safer);fastest.id="fastest";
  if (fastest.origin?.kind === "MEMBER3") fastest.origin.profile="FASTEST";
  expect(physicalRouteKey(safer)).toBe(physicalRouteKey(fastest));
  const explained=explainProfile(safer,[safer,fastest])!;
  expect(explained.identical).toEqual(["FASTEST"]);
  expect(explained.facts.join(" ")).toContain("Cùng tuyến");
  expect(explained.facts.join(" ")).toContain("chưa chứng minh tối ưu toàn cục");
});
it("explains measured time/risk differences without treating exposure as accident probability", () => {
  const safer=proposal(), fastest=structuredClone(safer);fastest.id="fastest";
  if (fastest.origin?.kind === "MEMBER3") fastest.origin.profile="FASTEST";
  fastest.nativeForecast!.metrics.values.total_travel_time_s=1;
  fastest.nativeForecast!.metrics.values.total_exposure=4;
  const text=explainProfile(safer,[safer,fastest])!.facts.join(" ");
  expect(text).toContain("lâu hơn 1.00 giây");expect(text).toContain("giảm 2.00000 (50.00%)");
  expect(text).toContain("proxy");
});
