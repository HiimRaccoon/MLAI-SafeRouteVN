import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { ReplayControls } from "./ReplayControls";
import { MockStateEngine } from "../mocks/engine/MockStateEngine";
import { acceptedView } from "../integrations/member3/acceptedTestFixture";
import { controller } from "../integrations/member3/playbackTestFixture";
import type { DispatchApi } from "../services/api/DispatchApi";
function setup(settling = false, stale = false, completed = false, noPlan = false) {
  const snapshot = new MockStateEngine().getSnapshot(), view = acceptedView();
  snapshot.decisionState.sessionId = view.basis.session_id;
  snapshot.backend = { source: "MEMBER3_HTTP", baseUrl: "http://localhost", basis: view.basis, executionView: view, executionMode: "SIMULATED_REPLAY", realWorldObservation: false,
    capabilities: { mode: "backend", optimize: true, accept: true, applyEvent: true, replay: true, driverActions: false, forecastGeometry: true }, playback: { schema_version: "saferoute-m3-playback-controller/1", ...controller({ in_flight: settling, fully_paused: !settling }), execution_view: view } as NonNullable<typeof snapshot.backend>["playback"] };
  const api = { replayStep: vi.fn(async () => snapshot), replayStart: vi.fn(async () => snapshot), replayPause: vi.fn(async () => snapshot), replaySpeed: vi.fn(async () => snapshot), resetSession: vi.fn(async () => snapshot) } as unknown as DispatchApi;
  const invoke = async (operation: () => Promise<typeof snapshot>) => { await operation(); };
  if (completed) snapshot.backend.playback!.reason = "PLAN_COMPLETE";
  if (stale) { snapshot.backend.stale = true; snapshot.backend.error = { code: "STATE_CHANGED", message: "World changed during reads" }; snapshot.backend.capabilities!.replay = false; }
  if (noPlan) snapshot.backend.executionView.active_job_id = null;
  render(<ReplayControls api={api} snapshot={snapshot} pending={false} invoke={invoke} />);
  return { api, snapshot };
}
it("invokes owner replay commands using only supported speeds", async () => {
  const { api } = setup(), user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Step" })); expect(api.replayStep).toHaveBeenCalledOnce();
  await user.selectOptions(screen.getByLabelText("Replay speed"), "4"); expect(api.replaySpeed).toHaveBeenCalledWith(4);
  for (const speed of [16, 32, 64, 128]) { await user.selectOptions(screen.getByLabelText("Replay speed"), String(speed)); expect(api.replaySpeed).toHaveBeenLastCalledWith(speed); }
  expect(screen.getAllByRole("option").map(e => e.getAttribute("value"))).toEqual(["1", "2", "4", "8", "16", "32", "64", "128"]);
  await user.click(screen.getByRole("button", { name: "Play" })); expect(api.replayStart).toHaveBeenCalledWith(1);
});
it("displays reserved tick settlement and disables Step/Play/Reset until server fully paused", () => {
  setup(true);
  expect(screen.getByText(/Settling reserved tick/)).toBeInTheDocument();
  for (const name of ["Step", "Play"]) expect(screen.getByRole("button", { name })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Pause" })).toBeEnabled();
});
it("keeps owner Pause available during observational staleness while physical actions stay disabled", () => {
  setup(true, true);
  expect(screen.getByRole("button", { name: "Pause" })).toBeEnabled();
  for (const name of ["Step", "Play"]) expect(screen.getByRole("button", { name })).toBeDisabled();
});
it("disables Step and Play after server completion while keeping Reset available", () => {
  setup(false, false, true);
  for (const name of ["Step", "Play"]) expect(screen.getByRole("button", { name })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "Reset session" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Refresh server" })).toBeNull();
});
it('explains that Play needs an accepted route rather than merely a finished optimization',()=>{
  setup(false,false,false,true);
  expect(screen.getByRole('button',{name:'Play'})).toBeDisabled();
  expect(screen.getByText(/Chờ admin Accept tuyến/)).toBeInTheDocument();
});
