import { TinySuppliedApi } from "../test/tinySuppliedPlan";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { MOCK_STORAGE_KEY, MockDispatchApi, type StorageLike } from "../services/api/MockDispatchApi";
import { App } from "./App";

class TestStorage implements StorageLike {
  private readonly values = new Map<string, string>();
  getItem(key: string) { return this.values.get(key) ?? null; }
  setItem(key: string, value: string) { this.values.set(key, value); }
  removeItem(key: string) { this.values.delete(key); }
}

function renderExperience(path: string, api = new MockDispatchApi({ storage: new TestStorage() })) {
  return {
    api,
    user: userEvent.setup(),
    ...render(<MemoryRouter initialEntries={[path]}><App api={api} /></MemoryRouter>)
  };
}

describe("Admin and Driver experiences", () => {
  it("keeps Urgent ON and explains why OFF is disabled after pickup", async () => {
    const api = new MockDispatchApi({ storage: new TestStorage() });
    await api.setUrgentOrderEnabled(true);
    const optimized = await api.optimize();
    await api.selectAlternative(optimized.planState.proposedAlternatives[0].id);
    const accepted = await api.acceptSelectedPlan();
    const owner = accepted.planState.acceptedPlans[0].plan.vehiclePlans.find((v) => v.orderedStops.some((s) => s.orderIds.includes("O009")))!;
    await api.pickupOrder({ vehicleId: owner.vehicleId, orderId: "O009" });
    renderExperience("/admin", api);
    const urgent = await screen.findByRole("switch", { name: /^urgent order$/i });
    expect(urgent).toHaveAttribute("aria-checked", "true");
    expect(urgent).toBeDisabled();
    expect(urgent).toHaveAttribute("title", "The urgent order cannot be cancelled after pickup.");
  });

  it("starts Urgent OFF with three S0 orders, allows ON/OFF/ON, and restores OFF on refresh", async () => {
    const storage = new TestStorage();
    const api = new MockDispatchApi({ storage });
    const view = renderExperience("/admin", api);
    const urgent = await screen.findByRole("switch", { name: /^urgent order$/i });
    expect(urgent).toHaveAttribute("aria-checked", "false");
    expect(screen.getByText(/current queue/i)).toHaveTextContent("3");
    await view.user.click(urgent);
    expect(urgent).toHaveAttribute("aria-checked", "true");
    expect(urgent).toBeEnabled();
    expect((await api.getSnapshot()).decisionState.orders).toHaveLength(4);
    await view.user.click(urgent);
    expect(urgent).toHaveAttribute("aria-checked", "false");
    expect((await api.getSnapshot()).decisionState.orders).toHaveLength(3);
    expect(document.getElementById("toggle-driver")).toBeDisabled();
    view.unmount();
    const reloaded = renderExperience("/admin", new MockDispatchApi({ storage }));
    const restored = await screen.findByRole("switch", { name: /^urgent order$/i });
    expect(restored).toHaveAttribute("aria-checked", "false");
    expect(restored).toBeEnabled();
    await reloaded.user.click(restored);
    expect(restored).toHaveAttribute("aria-checked", "true");
    expect((await new MockDispatchApi({ storage }).getSnapshot()).decisionState.orders).toHaveLength(4);
  });

  it("shows the displayed plan's static source forecast separately from the advancing demo clock", async () => {
    const api = new TinySuppliedApi({ storage: new TestStorage() });
    const initial = await api.optimize();
    await api.selectAlternative(initial.planState.proposedAlternatives[0].id);
    const { container } = renderExperience("/admin", api);
    expect(await screen.findByLabelText("Source forecast time")).toHaveTextContent("2026-09-27 21:00:00 +07:00");
    await act(async () => { await api.advanceDemoClock(10); });
    expect(screen.getByLabelText("Source forecast time")).toHaveTextContent("2026-09-27 21:00:00 +07:00");
    expect(container.querySelector(".di-provenance")).not.toBeInTheDocument();
  });
  it("keeps Admin routes OFF initially and switches each vehicle independently without persisting or mutating state", async () => {
    const storage = new TestStorage();
    const api = new TinySuppliedApi({ storage });
    const proposals = await api.optimize();
    await api.selectAlternative(proposals.planState.proposedAlternatives[0].id);
    await api.acceptSelectedPlan();
    const before = await api.getSnapshot();
    const persisted = storage.getItem(MOCK_STORAGE_KEY);
    const { user, container } = renderExperience("/admin", api);
    const v1 = await screen.findByRole("switch", { name: "Show V1 route" });
    const v2 = screen.getByRole("switch", { name: "Show V2 route" });
    const routes = () => [...container.querySelectorAll(".leaflet-overlay-pane path")];
    const markers = () => container.querySelectorAll(".saferoute-map-marker").length;
    expect(v1).toHaveAttribute("aria-checked", "false");
    expect(v2).toHaveAttribute("aria-checked", "false");
    expect(routes()).toHaveLength(0);
    await waitFor(() => expect(markers()).toBe(6));
    await user.click(v1);
    expect(routes().map((r) => r.getAttribute("stroke"))).toEqual(["#2563eb"]);
    expect(v2).toHaveAttribute("aria-checked", "false");
    await user.click(v1);
    await user.click(v2);
    expect(routes().map((r) => r.getAttribute("stroke"))).toEqual(["#16a34a", "#16a34a"]);
    await user.click(v1);
    expect(routes()).toHaveLength(3);
    expect(markers()).toBe(6);
    const controls = screen.getByRole("region", { name: "Route visibility" });
    expect(controls).toHaveTextContent("O003");
    expect(controls).toHaveTextContent("O001");
    expect(controls).not.toHaveTextContent("Return to depot");
    await user.click(v1);
    await user.click(v2);
    expect(routes()).toHaveLength(0);
    expect(markers()).toBe(6);
    expect(await api.getSnapshot()).toEqual(before);
    expect(storage.getItem(MOCK_STORAGE_KEY)).toBe(persisted);
  });

  it("does not enable Admin routes after Optimize, Select or Accept, and previews only the selected source", async () => {
    const { user, container } = renderExperience("/admin");
    const v1 = await screen.findByRole("switch", { name: "Show V1 route" });
    const v2 = screen.getByRole("switch", { name: "Show V2 route" });
    const assertOff = () => {
      expect(v1).toHaveAttribute("aria-checked", "false");
      expect(v2).toHaveAttribute("aria-checked", "false");
      expect(container.querySelectorAll(".leaflet-overlay-pane path")).toHaveLength(0);
    };
    await user.click(screen.getByRole("button", { name: /^optimize$/i }));
    await waitFor(() => expect(screen.getByRole("button", { name: /select fastest/i })).toBeEnabled());
    assertOff();
    await user.click(screen.getByRole("button", { name: /select fastest/i }));
    assertOff();
    await user.click(screen.getAllByRole("button", { name: /accept selected plan/i })[0]);
    await waitFor(() => expect(screen.getByRole("region", { name: "Route visibility" })).toHaveTextContent("Accepted route"));
    assertOff();
    await user.click(v2);
    expect(container.querySelectorAll(".leaflet-overlay-pane path")).toHaveLength(1);
    expect(screen.getByRole("region", { name: "Route visibility" })).toHaveTextContent("Accepted route");
    await user.click(screen.getByRole("button", { name: /^optimize$/i }));
    await waitFor(() => expect(screen.getByRole("button", { name: /select balanced/i })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: /select balanced/i }));
    const paths = [...container.querySelectorAll(".leaflet-overlay-pane path")];
    expect(paths).toHaveLength(2);
    expect(paths.every((p) => p.getAttribute("stroke-dasharray") === "7 6")).toBe(true);
    expect(screen.getByRole("region", { name: "Route visibility" })).toHaveTextContent("Proposed route");
    expect(v2).toHaveAttribute("aria-checked", "true");
  });

  it("resets Admin visibility on remount and new session while supporting keyboard switches", async () => {
    const api = new MockDispatchApi({ storage: new TestStorage() });
    const proposals = await api.optimize();
    await api.selectAlternative(proposals.planState.proposedAlternatives[0].id);
    await api.acceptSelectedPlan();
    const first = renderExperience("/admin", api);
    const v1 = await screen.findByRole("switch", { name: "Show V1 route" });
    v1.focus();
    await first.user.keyboard(" ");
    expect(v1).toHaveAttribute("aria-checked", "true");
    first.unmount();
    const second = renderExperience("/admin", api);
    const v2 = await screen.findByRole("switch", { name: "Show V2 route" });
    expect(v2).toHaveAttribute("aria-checked", "false");
    await second.user.click(v2);
    expect(v2).toHaveAttribute("aria-checked", "true");
    await act(async () => { await api.loadScenario("S2"); });
    await waitFor(() => expect(v2).toHaveAttribute("aria-checked", "false"));
  });
  it("does not invent vehicle ETAs for an offline proposal before Accept", async () => {
    const api = new TinySuppliedApi({ storage: new TestStorage() });
    const optimized = await api.optimize();
    await api.selectAlternative(optimized.planState.proposedAlternatives[0].id);
    const { container } = renderExperience("/admin", api);
    await screen.findAllByText(/fleet travel:/i);
    const cells = [...container.querySelectorAll(".eta-cell")];
    expect(cells).toHaveLength(2);
    expect(cells.every((cell) => cell.textContent === "—")).toBe(true);
  });
  it("labels fleet travel separately from ETA and never invents offline vehicle arrival times", async () => {
    const api = new TinySuppliedApi({ storage: new TestStorage() });
    const optimized = await api.optimize();
    await api.selectAlternative(optimized.planState.proposedAlternatives[0].id);
    await api.acceptSelectedPlan();
    const { user, container } = renderExperience("/admin", api);
    await screen.findByRole("switch", { name: "Show V1 route" });
    expect([...container.querySelectorAll(".eta-cell")].every((cell) => cell.textContent === "—")).toBe(true);
    expect(screen.getByText("Fleet travel time", { selector: ".kpi-label" })).toBeInTheDocument();
    const distanceRow = within(screen.getByRole("table")).getByText("Total distance").closest("tr")!;
    expect(within(distanceRow).getAllByRole("cell")[1]).toHaveTextContent("36.5 km");
    await user.click(screen.getByRole("button", { name: /^optimize$/i }));
    await screen.findByRole("button", { name: /select fastest/i });
    const fastest = screen.getByText("FASTEST").closest("article")!;
    await waitFor(() => expect(fastest).toHaveTextContent("Fleet travel:"));
    expect(fastest).not.toHaveTextContent("ETA:");
  });
  it("shows the whole supplied incoming leg in the Driver distance and travel forecast", async () => {
    const api = new TinySuppliedApi({ storage: new TestStorage() });
    const optimized = await api.optimize();
    await api.selectAlternative(optimized.planState.proposedAlternatives[0].id);
    const accepted = await api.acceptSelectedPlan();
    await api.pickupOrder({ vehicleId: "V1", orderId: "O002" });
    const route = accepted.planState.acceptedPlans[0].plan.vehiclePlans.find((v) => v.vehicleId === "V1")!;
    const stop = route.orderedStops.find((s) => s.kind === "DELIVERY")!;
    const incoming = route.routeSegments.filter((segment) => segment.toStopId === stop.id);
    expect(incoming.length).toBe(2);
    const { container } = renderExperience("/driver", api);
    await screen.findByRole("button", { name: /delivered/i });
    expect(container.querySelector(".drv-metric-km")).toHaveTextContent(`${incoming.reduce((total, s) => total + s.distanceKm, 0).toFixed(1)} km`);
    expect(container.querySelector(".drv-metric-min")).toHaveTextContent(`${incoming.reduce((total, s) => total + s.durationMinutes, 0).toFixed(1)} min`);
  });
  it("does not subtract schematic fuel estimates from offline route costs", async () => {
    const api = new TinySuppliedApi({ storage: new TestStorage() });
    const initial = await api.optimize();
    await api.selectAlternative(initial.planState.proposedAlternatives[0].id);
    await api.acceptSelectedPlan();
    const world = await api.getSnapshot();
    // Unsupported S0 rain exercises the schematic/road metric boundary.
    await api.triggerFixtureEvent(world.demo.availableEvents.find((event) => event.type === "LOCAL_RAIN_WHAT_IF")!.id);
    const proposed = await api.optimize();
    await api.selectAlternative(proposed.planState.proposedAlternatives[0].id);
    renderExperience("/admin", api);
    const table = await screen.findByRole("table");
    const row = within(table).getByText("Cost (different scopes)").closest("tr")!;
    const cells = within(row).getAllByRole("cell");
    expect(cells[1]).toHaveTextContent("route estimate");
    expect(cells[2]).toHaveTextContent("fuel estimate");
    expect(cells[3]).toHaveTextContent("—");
  });
  it("shows absent offline on-time metrics as a dash and labels supplied route cost", async () => {
    class OfflineMetricsApi extends MockDispatchApi {
      override async optimize() {
        const snapshot = await super.optimize();
        for (const proposal of snapshot.planState.proposedAlternatives) {
          proposal.content!.provenance.source = "Member 2 offline runtime";
          proposal.content!.metrics.onTimeRate = null;
          proposal.content!.metrics.exposureScore = 234;
        }
        return snapshot;
      }
    }
    const { user } = renderExperience("/admin", new OfflineMetricsApi({ storage: new TestStorage() }));
    await user.click(await screen.findByRole("button", { name: /^optimize$/i }));
    const fastest = screen.getByText("FASTEST").closest("article")!;
    expect(within(fastest).getByText(/route cost:/i)).toBeInTheDocument();
    expect(within(fastest).getByText("2.34")).toBeInTheDocument();
    expect(fastest).not.toHaveTextContent("null%");
    expect(document.body).not.toHaveTextContent("null%");
  });
  it("shows fleet capacity from the S0 vehicles only", async () => {
    renderExperience("/admin");

    expect(await screen.findByText(/total capacity:\s*30 kg/i)).toBeInTheDocument();
  });

  it("exposes the working map order editor instead of a placeholder order form", async () => {
    renderExperience("/admin");
    expect(await screen.findByRole("combobox", { name: "Place order on map" })).toBeInTheDocument();
    expect(screen.queryByDisplayValue("ORD-0152")).not.toBeInTheDocument();
  });

  it("labels simulated dispatch honestly and shows no invented notification count", async () => {
    renderExperience("/admin");
    await screen.findByText(/current queue/i);
    expect(screen.getByText("Demo status")).toBeInTheDocument();
    expect(within(screen.getByRole("button", { name: "System notifications" })).queryByText("1")).not.toBeInTheDocument();
  });

  it("reflects triggered events and reset from the shared snapshot", async () => {
    const api = new MockDispatchApi({ storage: new TestStorage() });
    renderExperience("/admin", api);
    await screen.findByText(/current queue/i);
    const rainSwitch = document.getElementById("toggle-rain");
    expect(rainSwitch).toHaveAttribute("aria-checked", "false");

    await api.triggerFixtureEvent("S4-E1");
    await waitFor(() => expect(rainSwitch).toHaveAttribute("aria-checked", "true"));
    await api.resetDemoSession();
    await waitFor(() => expect(rainSwitch).toHaveAttribute("aria-checked", "false"));
  });

  it("shows proposal fuel and risk metrics returned by the API", async () => {
    class CustomMetricsApi extends MockDispatchApi {
      override async optimize() {
        const snapshot = await super.optimize();
        snapshot.planState.proposedAlternatives[0].content!.metrics.fuelCostVnd = 123000;
        snapshot.planState.proposedAlternatives[0].content!.metrics.exposureScore = 234;
        return snapshot;
      }
    }
    const { user } = renderExperience("/admin", new CustomMetricsApi({ storage: new TestStorage() }));
    await user.click(await screen.findByRole("button", { name: /^optimize$/i }));

    const fastest = screen.getByText("FASTEST").closest("article");
    expect(fastest).not.toBeNull();
    expect(within(fastest!).getByText(/123K VND/)).toBeInTheDocument();
    expect(within(fastest!).getByText("2.34x")).toBeInTheDocument();
  });

  it("does not invent before metrics when no plan has been accepted", async () => {
    const { user } = renderExperience("/admin", new TinySuppliedApi({ storage: new TestStorage() }));
    await user.click(await screen.findByRole("button", { name: /^optimize$/i }));
    await user.click(screen.getByRole("button", { name: /select fastest/i }));

    const comparison = screen.getByRole("table");
    const fuelRow = within(comparison).getByText("Route cost (est.)").closest("tr");
    expect(fuelRow).not.toBeNull();
    expect(within(fuelRow!).getAllByRole("cell")[1]).toHaveTextContent("—");
    expect(within(fuelRow!).getAllByRole("cell")[2]).toHaveTextContent("91,182 VND");
    expect(within(fuelRow!).getAllByRole("cell")[3]).toHaveTextContent("—");
  });

  it("does not visually select an alternative before Dispatcher chooses one", async () => {
    renderExperience("/admin");

    const fastest = await screen.findByText("FASTEST");

    expect(fastest.closest("article")).not.toHaveClass("selected");
  });

  it("lets Dispatcher optimize, select and accept a proposed plan", async () => {
    const { user } = renderExperience("/admin");

    await user.click(await screen.findByRole("button", { name: /^(optimize|tối ưu)$/i }));
    const balanced = await screen.findByRole("button", { name: /select balanced|chọn balanced/i });
    await user.click(balanced);
    const [acceptBtn] = screen.getAllByRole("button", { name: /accept selected plan|điều phối phương án đã chọn/i });
    await user.click(acceptBtn);

    await waitFor(() => expect(screen.getByRole("region", { name: "Route visibility" })).toHaveTextContent("Accepted route"));
    expect(screen.queryByText(/plan accepted/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/dispatched to drivers/i)).not.toBeInTheDocument();
    expect(document.querySelector(".di-provenance")).not.toBeInTheDocument();
  });

  it("shows the exact empty state for Driver before any plan is accepted", async () => {
    renderExperience("/driver");

    expect(await screen.findByText(/no dispatch plan has been assigned yet/i)).toBeInTheDocument();
  });

  it("shows the current stop for Driver after Dispatcher accepts a plan", async () => {
    const api = new MockDispatchApi({ storage: new TestStorage() });
    const optimized = await api.optimize();
    await api.selectAlternative(optimized.planState.proposedAlternatives[0].id);
    await api.acceptSelectedPlan();
    renderExperience("/driver", api);

    expect(await screen.findByRole("heading", { name: /current stop|điểm dừng hiện tại/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /picked up|lấy hàng/i })).toBeInTheDocument();
  });

  it("opens a Driver order using the keyboard", async () => {
    const api = new MockDispatchApi({ storage: new TestStorage() });
    const optimized = await api.optimize();
    await api.selectAlternative(optimized.planState.proposedAlternatives[1].id);
    await api.acceptSelectedPlan();
    const { user } = renderExperience("/driver", api);

    const order = await screen.findByRole("button", { name: /Trần Thị Hoa/i });
    order.focus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("heading", { name: "Order Details" })).toBeInTheDocument();
  });

  it("shows demo clock time in the Driver phone shell", async () => {
    const api = new MockDispatchApi({ storage: new TestStorage() });
    const initial = await api.getSnapshot();
    const { container } = renderExperience("/driver", api);
    await screen.findByText(/no dispatch plan has been assigned yet/i);
    expect(container.querySelector(".drv-status-time")).toHaveTextContent(initial.demoClock.now.slice(11, 16));

    const advanced = await api.advanceDemoClock(10);
    await waitFor(() => expect(container.querySelector(".drv-status-time")).toHaveTextContent(advanced.demoClock.now.slice(11, 16)));
  });

  it("notifies an open Driver experience when another action accepts a route", async () => {
    const api = new MockDispatchApi({ storage: new TestStorage() });
    renderExperience("/driver", api);
    await screen.findByText(/no dispatch plan has been assigned yet/i);
    const optimized = await api.optimize();
    await api.selectAlternative(optimized.planState.proposedAlternatives[0].id);
    await api.acceptSelectedPlan();

    expect(await screen.findByText(/route has been updated/i)).toBeInTheDocument();
  });
});
