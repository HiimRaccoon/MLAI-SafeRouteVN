"""Candidate path contract for future path generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .common import (
    ContractValidationError,
    EdgeReference,
    GeoPoint,
    NodeId,
    UnitMetadata,
    require_non_empty_string,
    validate_finite_number,
    validate_node_id,
)


@dataclass(frozen=True, slots=True)
class CandidatePath:
    """One directed candidate street path between two decision nodes."""

    path_id: str
    nodes: tuple[NodeId, ...]
    edges: tuple[EdgeReference, ...]
    geometry: tuple[GeoPoint, ...]
    distance: float
    travel_time: float
    risk_score: float
    unit_metadata: UnitMetadata = field(default_factory=UnitMetadata)

    def __post_init__(self) -> None:
        object.__setattr__(self, "path_id", require_non_empty_string(self.path_id, "path_id"))
        nodes = tuple(
            validate_node_id(node, f"nodes[{index}]")
            for index, node in enumerate(self.nodes)
        )
        if not nodes:
            raise ContractValidationError("nodes must contain at least one node")
        object.__setattr__(self, "nodes", nodes)

        edges = tuple(
            edge if isinstance(edge, EdgeReference) else EdgeReference.from_value(edge)
            for edge in self.edges
        )
        if len(edges) != max(0, len(nodes) - 1):
            raise ContractValidationError("edges must contain one edge per adjacent node pair")
        for index, edge in enumerate(edges):
            if edge.u != nodes[index] or edge.v != nodes[index + 1]:
                raise ContractValidationError(
                    f"edges[{index}] does not match nodes[{index}:{index + 2}]"
                )
        object.__setattr__(self, "edges", edges)

        geometry = tuple(
            point if isinstance(point, GeoPoint) else GeoPoint.from_value(point)
            for point in self.geometry
        )
        if not geometry:
            raise ContractValidationError("geometry must contain at least one coordinate")
        object.__setattr__(self, "geometry", geometry)

        for field_name in ("distance", "travel_time", "risk_score"):
            object.__setattr__(
                self,
                field_name,
                validate_finite_number(getattr(self, field_name), field_name, minimum=0.0),
            )
        metadata = (
            self.unit_metadata
            if isinstance(self.unit_metadata, UnitMetadata)
            else UnitMetadata.from_mapping(self.unit_metadata)
        )
        object.__setattr__(self, "unit_metadata", metadata)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CandidatePath":
        """Build a candidate path from contract data."""

        required = {
            "path_id",
            "nodes",
            "edges",
            "geometry",
            "distance",
            "travel_time",
            "risk_score",
        }
        missing = required - set(payload)
        if missing:
            raise ContractValidationError(
                f"CandidatePath missing required fields: {', '.join(sorted(missing))}"
            )
        return cls(
            path_id=payload["path_id"],
            nodes=tuple(payload["nodes"]),
            edges=tuple(EdgeReference.from_value(edge) for edge in payload["edges"]),
            geometry=tuple(GeoPoint.from_value(point) for point in payload["geometry"]),
            distance=payload["distance"],
            travel_time=payload["travel_time"],
            risk_score=payload["risk_score"],
            unit_metadata=UnitMetadata.from_mapping(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the candidate path in a JSON-compatible representation."""

        return {
            **self.unit_metadata.to_dict(),
            "path_id": self.path_id,
            "nodes": list(self.nodes),
            "edges": [edge.to_dict() for edge in self.edges],
            "geometry": [point.to_list() for point in self.geometry],
            "distance": self.distance,
            "travel_time": self.travel_time,
            "risk_score": self.risk_score,
        }
