import { act, render, screen } from "@testing-library/react";
import L from "leaflet";
import { describe, expect, it, vi } from "vitest";
import { MockStateEngine } from "../../mocks/engine/MockStateEngine";
import { DispatchMap } from "./DispatchMap";
import { createAdminMapPresentation } from "../../admin/adminMapPresentation";

describe("DispatchMap", () => {
  it("identifies sparse mock routes as schematic rather than road geometry", () => {
    const engine = new MockStateEngine();
    // S0 rain has no matched road pack in the released M2 runtime.
    engine.triggerFixtureEvent("S4-E1");
    const optimized = engine.optimize();
    const proposal = optimized.planState.proposedAlternatives[0];
    const selected = engine.selectAlternative(proposal.id);
    render(<DispatchMap snapshot={selected} scene={createAdminMapPresentation(selected, proposal, ["V1", "V2"]).scene} />);
    expect(screen.getByRole("note")).toHaveTextContent(/schematic demo routes.*do not follow roads/i);
  });

  it("previews selected geometry without overlaying the active accepted route", () => {
    const engine = new MockStateEngine();
    const optimized = engine.optimize();
    const selected = optimized.planState.proposedAlternatives[1];
    engine.selectAlternative(selected.id);
    engine.acceptSelectedPlan();
    const next = engine.optimize();
    const proposal = next.planState.proposedAlternatives[0];
    const snapshot = engine.selectAlternative(proposal.id);
    render(<DispatchMap snapshot={snapshot} scene={createAdminMapPresentation(snapshot, proposal, ["V1", "V2"]).scene} />);

    expect(screen.queryByLabelText(/accepted route|lộ trình đã điều phối/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/proposed.*not dispatched|lộ trình đề xuất/i)).toBeInTheDocument();
  });

  it("renders the real rain polygon only while S4 is active", () => {
    const engine = new MockStateEngine();
    engine.loadScenario("S4");
    const withRain = engine.triggerFixtureEvent("S4-E1");

    render(<DispatchMap snapshot={withRain} />);

    expect(screen.getByLabelText(/simulated rain area|vùng mưa mô phỏng/i)).toBeInTheDocument();
  });

  it("keeps map overlays and shows a warning when OSM tiles fail", () => {
    const actualTileLayer = L.tileLayer;
    let layer: L.TileLayer | null = null;
    const spy = vi.spyOn(L, "tileLayer").mockImplementation((...args) => {
      layer = actualTileLayer(...args);
      return layer;
    });
    try {
      const engine = new MockStateEngine();
      const optimized = engine.optimize();
      engine.selectAlternative(optimized.planState.proposedAlternatives[0].id);
      const accepted = engine.acceptSelectedPlan();
      render(<DispatchMap snapshot={accepted} scene={createAdminMapPresentation(accepted, undefined, ["V1", "V2"]).scene} />);
      act(() => { layer?.fire("tileerror"); });
      expect(screen.getByText(/map tiles unavailable/i)).toBeInTheDocument();
      expect(screen.getByLabelText("Accepted route")).toBeInTheDocument();
    } finally {
      spy.mockRestore();
    }
  });
});
