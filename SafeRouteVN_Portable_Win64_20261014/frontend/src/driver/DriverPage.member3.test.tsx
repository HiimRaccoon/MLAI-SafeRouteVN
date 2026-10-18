import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { DispatchProvider } from "../app/DispatchContext";
import { DriverPage } from "./DriverPage";
import { MockStateEngine } from "../mocks/engine/MockStateEngine";
import { acceptedView } from "../integrations/member3/acceptedTestFixture";
import { adaptAcceptedExecution } from "../integrations/member3/executionViewAdapter";
import type { DispatchApi } from "../services/api/DispatchApi";

vi.mock("./DriverMap", () => ({ DriverMap: () => null }));
function setup(accepted = true, customize?: (snapshot: ReturnType<MockStateEngine['getSnapshot']>, api: DispatchApi)=>void) {
  const snapshot = new MockStateEngine().getSnapshot(), view = acceptedView();
  view.current_time = "2026-09-27T21:12:00+07:00";
  view.delivered_prefix = ["O002"];
  view.vehicles[0].current_load_kg = 7;
  view.vehicles[0].onboard_order_ids = ["O001"];
  delete view.vehicles[0].position_timestamp;
  if (!accepted) { view.active_job_id = null; view.accepted_trajectory = null; }
  Object.assign(snapshot, adaptAcceptedExecution(view));
  snapshot.backend = { source: "MEMBER3_HTTP", baseUrl: "http://localhost:8000", executionMode: "SIMULATED_REPLAY", realWorldObservation: false, basis: view.basis, executionView: view };
  const api: DispatchApi = { getSnapshot: async () => snapshot, subscribe: () => () => {}, loadScenario: vi.fn(), optimize: vi.fn(), selectAlternative: vi.fn(), acceptSelectedPlan: vi.fn(), triggerFixtureEvent: vi.fn(), setUrgentOrderEnabled: vi.fn(), pickupOrder: vi.fn(), deliverOrder: vi.fn(), advanceDemoClock: vi.fn(), resetDemoSession: vi.fn() };
  customize?.(snapshot,api);
  render(<DispatchProvider api={api}><DriverPage /></DispatchProvider>);
  return { snapshot, api };
}
it("renders server time, onboard custody, load and delivered prefix without manual physical buttons", async () => {
  const { snapshot } = setup();
  expect(await screen.findByText("Server time: 2026-09-27T21:12:00+07:00")).toBeInTheDocument();
  expect(screen.getByText("Load: 7 kg · Onboard: O001")).toBeInTheDocument();
  expect(screen.getByText("Delivered prefix: O002")).toBeInTheDocument();
  expect(screen.getByText("Position observed: unavailable")).toBeInTheDocument();
  expect(JSON.parse(screen.getByText("Server time: 2026-09-27T21:12:00+07:00").closest("main")!.dataset.basis!)).toEqual(snapshot.backend!.basis);
  expect(screen.queryByRole("button", { name: /^(pick up|pickup|mark delivered)/i })).not.toBeInTheDocument();
  expect(snapshot.backend!.executionView.delivered_prefix).toEqual(["O002"]);
});
it('confirms consecutive orders and resumes replay after each confirmation',async()=>{
  let nextOrder='O002';
  const {api}=setup(true,(snapshot,api)=>{
    const b=snapshot.backend!,v=b.executionView.vehicles[0];
    v.awaiting_delivery='O001';
    b.playback={fully_paused:true,speed:4} as any;
    api.deliverOrder=vi.fn(async({orderId,vehicleId})=>{
      expect(vehicleId).toBe('V1');expect(orderId).toBe(v.awaiting_delivery);
      v.awaiting_delivery=null;return structuredClone(snapshot);
    });
    api.replayStart=vi.fn(async()=>{v.awaiting_delivery=nextOrder;nextOrder='O003';return structuredClone(snapshot);});
  });
  fireEvent.click(await screen.findByRole('button',{name:'Deliver · O001'}));
  await waitFor(()=>expect(screen.getByRole('button',{name:'Deliver · O002'})).toBeEnabled());
  fireEvent.click(screen.getByRole('button',{name:'Deliver · O002'}));
  await waitFor(()=>expect(screen.getByRole('button',{name:'Deliver · O003'})).toBeEnabled());
  expect(api.deliverOrder).toHaveBeenCalledTimes(2);
  expect(api.replayStart).toHaveBeenCalledWith(4);
});
it("keeps an empty accepted-route view while still showing observed custody after suffix suspension", async () => {
  setup(false);
  expect(await screen.findByText("Load: 7 kg · Onboard: O001")).toBeInTheDocument();
  expect(screen.getByText("No dispatch plan has been assigned yet.")).toBeInTheDocument();
  expect(screen.queryByText(/All orders have been delivered/)).not.toBeInTheDocument();
});

it("uses server IDs/placeholders instead of demo customer and driver identities", async()=>{setup();await screen.findByText("Server time: 2026-09-27T21:12:00+07:00");expect(screen.queryAllByText(/Nguy\u1ec5n V\u0103n A/)).toHaveLength(0);expect(screen.queryAllByText(/Tr\u1ea7n Th\u1ecb Hoa/)).toHaveLength(0);expect(screen.queryAllByText("0912 345 678")).toHaveLength(0);expect(screen.getAllByText(/V1/).length).toBeGreaterThan(0);});
