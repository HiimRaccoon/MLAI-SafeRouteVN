import type { MapSegment } from "../../shared/components/mapScene";
import type { Member2DecisionAlternative, Member2ExecutionView } from "./types";

/** Projects supplied forecast EDGE geometry only; it never links stops or creates routes. */
export function executionForecastSegments(view: Member2ExecutionView): MapSegment[] {
  return view.forecastEdges.features.map((feature, index) => ({
    id: `${feature.properties.vehicleId}:${feature.properties.edgeId}:${index}`,
    vehicleId: feature.properties.vehicleId,
    coordinates: feature.geometry.coordinates,
    completed: false,
    geometrySource: "MEMBER2_SUPPLIED"
  }));
}

/** The alternative supplies the complete polyline; stopSequence is never a geometry source. */
export function decisionAlternativeSegments(alternative: Member2DecisionAlternative): MapSegment[] {
  return Object.entries(alternative.routeGeometry).map(([vehicleId, geometry]) => ({
    id: `${alternative.profile}:${vehicleId}`,
    vehicleId,
    coordinates: geometry.coordinates,
    completed: false,
    geometrySource: "MEMBER2_SUPPLIED"
  }));
}
