import { arrive } from "../test/arrive";
import { tinySuppliedEngine } from "../test/tinySuppliedPlan";
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DriverMap } from "./DriverMap";
import { createMapScene } from "../shared/components/mapScene";
import L from "leaflet";

describe("DriverMap", () => {
  it('keeps the destination marker without a persistent label in next-delivery mode', () => {
    const engine = tinySuppliedEngine();
    engine.selectAlternative(engine.getSnapshot().planState.proposedAlternatives[0].id);
    const snapshot = engine.acceptSelectedPlan();
    const vehicle = snapshot.planState.acceptedPlans[0].plan.vehiclePlans.find(v => v.vehicleId === 'V2')!;
    for (const id of vehicle.orderedStops[0].orderIds) engine.pickupOrder({vehicleId:'V2',orderId:id});
    engine.advanceDemoClock(.01);
    const {container} = render(<DriverMap initialMode="NEXT" snapshot={engine.getSnapshot()} vehicleId="V2"/>);
    expect(container.querySelector('.leaflet-tooltip')).toBeNull();
    expect(container.querySelectorAll('.saferoute-marker--order')).toHaveLength(1);
    fireEvent.change(screen.getByLabelText('Route display'), {target:{value:'ALL'}});
    expect(container.querySelector('.leaflet-tooltip')).toBeNull();
  });
  it("keeps V2 colors after delivery, dims the completed leg, and never previews unaccepted proposals", () => {
    const engine = tinySuppliedEngine();
    const initial = engine.getSnapshot();
    engine.selectAlternative(initial.planState.proposedAlternatives[0].id);
    const accepted = engine.acceptSelectedPlan();
    const vehicle = accepted.planState.acceptedPlans[0].plan.vehiclePlans.find((v) => v.vehicleId === "V2")!;
    for (const orderId of vehicle.orderedStops[0].orderIds) engine.pickupOrder({ vehicleId: "V2", orderId });
    const delivery = vehicle.orderedStops.find((stop) => stop.kind === "DELIVERY")!;
    arrive(engine);
    for (const orderId of delivery.orderIds) engine.deliverOrder({ vehicleId: "V2", orderId });
    const snapshot = engine.optimize();
    engine.selectAlternative(snapshot.planState.proposedAlternatives[1].id);
    const selected = engine.getSnapshot();
    const original = L.polyline;
    const layers: L.Polyline[] = [];
    const spy = vi.spyOn(L, "polyline").mockImplementation((...args) => {
      const layer = original(...args); layers.push(layer); return layer;
    });
    try {
      render(<DriverMap initialMode="LEGS" snapshot={selected} vehicleId="V2" />);
      expect(layers.map((layer) => [layer.options.color, layer.options.opacity])).toEqual([["#16a34a", 0.35], ["#14532d", 0.9]]);
      const legs = vehicle.orderedStops.filter((stop) => stop.kind === "DELIVERY");
      for (const [index, stop] of legs.entries()) {
        expect(layers[index].toGeoJSON(false).geometry).toEqual({ type: "MultiLineString", coordinates: vehicle.routeSegments.filter((segment) => segment.toStopId === stop.id).map((segment) => segment.geometry.coordinates) });
      }
    } finally { spy.mockRestore(); }
  });

  it("uses V2 leg colors in the multicolor display mode, preserving every supplied edge and hiding depot return", () => {
    const engine = tinySuppliedEngine();
    const proposal = engine.getSnapshot().planState.proposedAlternatives[0];
    engine.selectAlternative(proposal.id);
    const snapshot = engine.acceptSelectedPlan();
    const before = structuredClone(snapshot);
    const vehicle = snapshot.planState.acceptedPlans[0].plan.vehiclePlans.find((v) => v.vehicleId === "V2")!;
    const original = L.polyline;
    const layers: L.Polyline[] = [];
    const spy = vi.spyOn(L, "polyline").mockImplementation((...args) => {
      const layer = original(...args); layers.push(layer); return layer;
    });
    try {
      const { container } = render(<DriverMap initialMode="LEGS" snapshot={snapshot} vehicleId="V2" />);
      expect(layers.map((layer) => layer.options.color)).toEqual(["#16a34a", "#14532d"]);
      const deliveryStops = vehicle.orderedStops.filter((stop) => stop.kind === "DELIVERY");
      for (const [index, stop] of deliveryStops.entries()) {
        expect(layers[index].toGeoJSON(false).geometry).toEqual({ type: "MultiLineString", coordinates: vehicle.routeSegments.filter((s) => s.toStopId === stop.id).map((s) => s.geometry.coordinates) });
      }
      expect(screen.getByLabelText("Route legs")).toHaveTextContent("1 → O003");
      expect(screen.getByLabelText("Route legs")).toHaveTextContent("2 → O001");
      expect(container.querySelector<HTMLElement>(".saferoute-marker--vehicle")!.style.background).toBe("rgb(22, 163, 74)");
      expect(snapshot).toEqual(before);
    } finally { spy.mockRestore(); }
  });
  it("dims completed route segments from execution progress", () => {
    const engine = tinySuppliedEngine();
    const optimized = engine.getSnapshot();
    engine.selectAlternative(optimized.planState.proposedAlternatives[0].id);
    const accepted = engine.acceptSelectedPlan();
    const active = accepted.planState.acceptedPlans.find(
      (plan) => plan.id === accepted.planState.activeAcceptedPlanId
    )!;
    const firstStop = active.plan.vehiclePlans.find((plan) => plan.vehicleId === "V1")!.orderedStops[0];
    for (const orderId of firstStop.orderIds) engine.pickupOrder({ vehicleId: "V1", orderId });
    arrive(engine);
    engine.deliverOrder({ vehicleId: "V1", orderId: "O002" });

    const snapshot = engine.getSnapshot();
    const { container } = render(<DriverMap initialMode="LEGS" snapshot={snapshot} />);
    expect(screen.getByLabelText("Accepted route")).toBeInTheDocument();
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
    expect(container.querySelector(".leaflet-container")).toBeInTheDocument();
    const scene = createMapScene(snapshot, undefined, "V1");
    expect(scene.accepted.filter((segment) => segment.completed).length).toBeGreaterThan(0);
    expect(scene.accepted.filter((segment) => !segment.completed).length).toBeGreaterThan(0);
  });
});
