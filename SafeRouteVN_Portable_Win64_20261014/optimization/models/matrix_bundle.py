"""Versioned D/T/R/G matrix contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .common import (
    ContractValidationError,
    GeoPoint,
    NodeId,
    UnitMetadata,
    freeze_json,
    require_non_empty_string,
    to_json_value,
    validate_finite_number,
    validate_node_id,
)


NumericMatrix = tuple[tuple[float, ...], ...]
Geometry = tuple[GeoPoint, ...]
GeometryMatrix = tuple[tuple[Geometry, ...], ...]
EntryProvenanceMatrix = tuple[tuple[Mapping[str, Any], ...], ...]


def _numeric_matrix(value: Sequence[Sequence[Any]], name: str, size: int) -> NumericMatrix:
    rows = tuple(
        tuple(
            validate_finite_number(cell, f"{name}[{row_index}][{column_index}]", minimum=0.0)
            for column_index, cell in enumerate(row)
        )
        for row_index, row in enumerate(value)
    )
    if len(rows) != size or any(len(row) != size for row in rows):
        raise ContractValidationError(f"{name} must be a {size}x{size} matrix")
    return rows


def _geometry_matrix(value: Sequence[Sequence[Any]], size: int) -> GeometryMatrix:
    rows: list[tuple[Geometry, ...]] = []
    for row_index, row in enumerate(value):
        geometry_row: list[Geometry] = []
        for column_index, geometry in enumerate(row):
            if not isinstance(geometry, Sequence) or isinstance(geometry, (str, bytes)):
                raise ContractValidationError(
                    f"G[{row_index}][{column_index}] must be a coordinate sequence"
                )
            geometry_row.append(
                tuple(
                    point if isinstance(point, GeoPoint) else GeoPoint.from_value(point)
                    for point in geometry
                )
            )
        rows.append(tuple(geometry_row))
    result = tuple(rows)
    if len(result) != size or any(len(row) != size for row in result):
        raise ContractValidationError(f"G must be a {size}x{size} matrix")
    return result


def _entry_provenance_matrix(
    value: Sequence[Sequence[Mapping[str, Any]]], size: int
) -> EntryProvenanceMatrix:
    if not value:
        raise ContractValidationError(
            "entry_provenance is required for every MatrixBundle"
        )
    rows: list[tuple[Mapping[str, Any], ...]] = []
    for row_index, row in enumerate(value):
        provenance_row: list[Mapping[str, Any]] = []
        for column_index, item in enumerate(row):
            if not isinstance(item, Mapping):
                raise ContractValidationError(
                    f"entry_provenance[{row_index}][{column_index}] must be a mapping"
                )
            provenance_row.append(
                freeze_json(item, f"entry_provenance[{row_index}][{column_index}]")
            )
        rows.append(tuple(provenance_row))
    result = tuple(rows)
    if len(result) != size or any(len(row) != size for row in result):
        raise ContractValidationError(
            f"entry_provenance must be a {size}x{size} matrix"
        )
    return result


@dataclass(frozen=True, slots=True)
class MatrixBundle:
    """Matrices sharing one version and one canonical decision-node order."""

    version: str
    node_order: tuple[NodeId, ...]
    distance_matrix: NumericMatrix
    time_matrix: NumericMatrix
    risk_matrix: NumericMatrix
    geometry_matrix: GeometryMatrix
    schema_version: str = "1.0.0"
    source: Mapping[str, Any] = field(default_factory=dict)
    entry_provenance: EntryProvenanceMatrix = ()
    unit_metadata: UnitMetadata = field(default_factory=UnitMetadata)

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", require_non_empty_string(self.version, "version"))
        node_order = tuple(
            validate_node_id(node, f"node_order[{index}]")
            for index, node in enumerate(self.node_order)
        )
        if not node_order:
            raise ContractValidationError("node_order must contain at least one node")
        if len(set(node_order)) != len(node_order):
            raise ContractValidationError("node_order must not contain duplicate nodes")
        object.__setattr__(self, "node_order", node_order)
        size = len(node_order)
        object.__setattr__(
            self, "distance_matrix", _numeric_matrix(self.distance_matrix, "D", size)
        )
        object.__setattr__(self, "time_matrix", _numeric_matrix(self.time_matrix, "T", size))
        object.__setattr__(self, "risk_matrix", _numeric_matrix(self.risk_matrix, "R", size))
        object.__setattr__(self, "geometry_matrix", _geometry_matrix(self.geometry_matrix, size))
        object.__setattr__(
            self,
            "schema_version",
            require_non_empty_string(self.schema_version, "schema_version"),
        )
        if not isinstance(self.source, Mapping):
            raise ContractValidationError("source must be a mapping")
        if not self.source:
            raise ContractValidationError("source must not be empty")
        object.__setattr__(self, "source", freeze_json(self.source, "source"))
        object.__setattr__(
            self,
            "entry_provenance",
            _entry_provenance_matrix(self.entry_provenance, size),
        )
        metadata = (
            self.unit_metadata
            if isinstance(self.unit_metadata, UnitMetadata)
            else UnitMetadata.from_mapping(self.unit_metadata)
        )
        object.__setattr__(self, "unit_metadata", metadata)

        for index in range(size):
            for column in range(size):
                if index == column:
                    continue
                if self.distance_matrix[index][column] == 0.0:
                    raise ContractValidationError(
                        "off-diagonal D values must be positive; zero is not an unreachable sentinel"
                    )
                if self.time_matrix[index][column] == 0.0:
                    raise ContractValidationError(
                        "off-diagonal T values must be positive; zero is not an unreachable sentinel"
                    )
                if not self.geometry_matrix[index][column]:
                    raise ContractValidationError(
                        "off-diagonal G values must contain route geometry"
                    )

    @property
    def matrix_version(self) -> str:
        """Explicit alias for the original ``version`` contract field."""

        return self.version

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MatrixBundle":
        """Build a bundle from the versioned D/T/R/G wire contract."""

        required = {"node_order", "D", "T", "R", "G"}
        missing = required - set(payload)
        if missing:
            raise ContractValidationError(
                f"MatrixBundle missing required fields: {', '.join(sorted(missing))}"
            )
        return cls(
            version=payload.get("matrix_version", payload.get("version")),
            node_order=tuple(payload["node_order"]),
            distance_matrix=tuple(tuple(row) for row in payload["D"]),
            time_matrix=tuple(tuple(row) for row in payload["T"]),
            risk_matrix=tuple(tuple(row) for row in payload["R"]),
            geometry_matrix=tuple(tuple(row) for row in payload["G"]),
            schema_version=payload.get("schema_version", "1.0.0"),
            source=payload.get("source", {}),
            entry_provenance=tuple(
                tuple(row) for row in payload.get("entry_provenance", ())
            ),
            unit_metadata=UnitMetadata.from_mapping(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the bundle using the canonical D/T/R/G field names."""

        return {
            **self.unit_metadata.to_dict(),
            "schema_version": self.schema_version,
            "matrix_version": self.matrix_version,
            "source": to_json_value(self.source),
            "node_order": list(self.node_order),
            "D": [list(row) for row in self.distance_matrix],
            "T": [list(row) for row in self.time_matrix],
            "R": [list(row) for row in self.risk_matrix],
            "G": [
                [[point.to_list() for point in geometry] for geometry in row]
                for row in self.geometry_matrix
            ],
            "entry_provenance": [
                [to_json_value(item) for item in row]
                for row in self.entry_provenance
            ],
        }
