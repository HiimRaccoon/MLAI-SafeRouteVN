import { render } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { DriverMap } from "./DriverMap";
import { LeafletCanvas } from "../shared/components/LeafletCanvas";
import { MockStateEngine } from "../mocks/engine/MockStateEngine";
import { acceptedView } from "../integrations/member3/acceptedTestFixture";
import { adaptAcceptedExecution } from "../integrations/member3/executionViewAdapter";
import { basis } from "../integrations/member3/testFixtures";

vi.mock("../shared/components/LeafletCanvas", () => ({ LeafletCanvas: vi.fn(() => null) }));
it("sends every native Driver leg including return with the per-leg palette to Leaflet", () => {
  const snapshot = new MockStateEngine().getSnapshot(); const view = acceptedView();
  Object.assign(snapshot, adaptAcceptedExecution(view));
  snapshot.backend = { source: "MEMBER3_HTTP", baseUrl: "http://localhost:8000", executionMode: "SIMULATED_REPLAY", realWorldObservation: false, basis, executionView: view };
  const edge = snapshot.planState.acceptedExecution!.segments[0];
  snapshot.planState.acceptedExecution!.segments = [1, 2, 3].map(number => ({ ...edge, vehicleId: "V2", id: `edge-${number}`, legId: `V2:leg:${number}`, returnToDepot: number === 3 }));
  const before = structuredClone(snapshot);
  render(<DriverMap snapshot={snapshot} vehicleId="V2" initialMode="LEGS" />);
  const segments = vi.mocked(LeafletCanvas).mock.lastCall![0].scene.accepted;
  expect(segments.map(s => s.color)).toEqual(["#16a34a", "#14532d", "#65a30d"]);
  expect(segments[2].returnToDepot).toBe(true);
  expect(segments.map(s => s.coordinates)).toEqual(before.planState.acceptedExecution!.segments.map(s => s.coordinates));
  expect(snapshot).toEqual(before);
});
