"""Workflow state shared across the ISAAC reflectivity tools.

Public API mirrors :mod:`ndip_state.state`.
"""

from .state import (
    SCHEMA_VERSION,
    build_state,
    empty_state,
    load_state,
    main,
    merge_stage,
    overall_status,
    record_error,
    save_state,
)

__all__ = [
    "SCHEMA_VERSION",
    "build_state",
    "empty_state",
    "load_state",
    "main",
    "merge_stage",
    "overall_status",
    "record_error",
    "save_state",
]
