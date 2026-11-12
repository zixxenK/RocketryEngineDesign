"""Shared, unit-aware data structures for propulsion references."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pint import UnitRegistry

ureg = UnitRegistry()
Q_ = ureg.Quantity


@dataclass(frozen=True)
class MachStation:
    name: str
    mach: float
    confidence: str = "reference"


@dataclass(frozen=True)
class MachComparison:
    name: str
    reference_mach: float
    result_mach: float
    delta_mach: float
    reference_confidence: str


@dataclass(frozen=True)
class RPA4Reference:
    case_id: str
    source_pdf: str
    fuel_name: str | None
    oxidizer_name: str | None
    mixture_ratio: float
    chamber_pressure: Any
    throat_diameter: Any
    total_mass_flow: Any
    oxidizer_mass_flow: Any
    fuel_mass_flow: Any
    ideal_performance: dict[str, Any]
    delivered_performance: dict[str, Any]
    geometry: dict[str, Any]
    station_conditions: dict[str, Any]
    mach_stations: tuple[MachStation, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RPA4Inputs:
    """Normalized input values imported from an RPA 4 JSON CFG."""

    source_file: str
    version: float
    case_name: str
    propellants: dict[str, tuple[tuple[str, float], ...]]
    mixture_ratio: float
    chamber_pressure: Any
    number_of_chambers: int
    throat_diameter: Any
    nozzle_inlet_area_ratio: float
    nozzle_exit_area_ratio: float
    chamber_length_value: float | None
    chamber_length_unit: str | None
    chamber_contraction_angle: Any
    r1_to_rt: float
    r2_to_r2max: float
    nozzle_throat_radius_to_rt: float
    nozzle_convergence_angle: Any
    nozzle_exit_angle: Any
    ambient_pressure_from: Any
    ambient_pressure_to: Any
    ambient_pressure_point_count: int
    equilibrium_flow_shifting: bool
    reaction_efficiency_estimated: bool
    bell_nozzle_efficiency_estimated: bool
    nozzle_efficiency_applied: bool
    multiphase: bool
