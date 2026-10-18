"""Versioned static M1 S1/S5/S6 bounded witness solver entry point.

The search implementation is shared with the reviewed S1 solver, but this
contract has its own wire/version and never relabels a fixture as S1.  It is a
bounded witness search, not OR-Tools and not an optimality proof.
"""

from __future__ import annotations

from typing import Any

from optimization.models.decision_state import DecisionState

from .member1_s0_graph import Member1RoadGraph
from .member1_s1 import (
    S1SearchLimits,
    StaticSolverContract,
    _solve_static,
)


SCHEMA_VERSION = "member1-static-s1-s5-s6-solution/1"
SOLVER_VERSION = "member1-bounded-turn-state-static-search/1"
SUPPORTED_SCENARIOS = frozenset({"S1", "S5", "S6"})
STATIC_CONTRACT = StaticSolverContract(
    schema_version=SCHEMA_VERSION,
    solver_version=SOLVER_VERSION,
    supported_scenarios=SUPPORTED_SCENARIOS,
    solution_prefix="m1-static",
)


def solve_static_initial_state(
    state: DecisionState,
    graph: Member1RoadGraph,
    *,
    limits: S1SearchLimits | None = None,
) -> dict[str, Any]:
    """Search a checkable initial-state witness for M1 S1, S5, or S6."""

    return _solve_static(state, graph, limits=limits, contract=STATIC_CONTRACT)


__all__ = [
    "SCHEMA_VERSION",
    "SOLVER_VERSION",
    "SUPPORTED_SCENARIOS",
    "S1SearchLimits",
    "solve_static_initial_state",
]
