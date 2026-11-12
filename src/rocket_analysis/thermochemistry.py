"""Ideal CEA and configurable Cantera screening adapters.

These routines provide thermochemical screening outputs. They do not model
injectors, cooling, structural margins, combustion instability, or measured
hot-fire performance.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CEAIdealScreen:
    model: str
    propellants: dict[str, str]
    mixture_ratio: float
    chamber_pressure_mpa: float
    exit_area_ratio: float
    chamber_temperature_k: float
    exit_pressure_bar: float
    exit_mach: float
    exit_area_ratio_calculated: float
    characteristic_velocity_m_s: float
    ideal_exit_specific_impulse_m_s: float
    ideal_vacuum_specific_impulse_m_s: float
    status: str = "ideal_equilibrium_screen"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CanteraEquilibriumScreen:
    model: str
    mechanism: str
    mixture_ratio: float
    chamber_pressure_pa: float
    equilibrium_temperature_k: float
    mean_molecular_weight_kg_kmol: float
    gamma_cp_cv: float
    density_kg_m3: float
    inlet_mixture_enthalpy_j_kg: float
    equilibrium_mass_fractions: dict[str, float]
    status: str = "equilibrium_state_only_no_nozzle_performance"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_cea_ideal_screen(
    *,
    mixture_ratio: float,
    chamber_pressure_mpa: float,
    exit_area_ratio: float,
    fuel_temperature_k: float = 298.1,
    oxidizer_temperature_k: float = 90.2,
    fuel_name: str = "RP-1",
    oxidizer_name: str = "O2(L)",
) -> CEAIdealScreen:
    """Solve an ideal, equilibrium CEA rocket case for RP-1/LOX conditions.

    NASA CEA's bundled RP-1 and liquid-oxygen reactant data are used at the
    supplied inlet temperatures. The final solution is the requested nozzle
    exit area ratio. Returned specific impulses are ideal outputs only.
    """
    values = {
        "mixture_ratio": mixture_ratio,
        "chamber_pressure_mpa": chamber_pressure_mpa,
        "exit_area_ratio": exit_area_ratio,
        "fuel_temperature_k": fuel_temperature_k,
        "oxidizer_temperature_k": oxidizer_temperature_k,
    }
    if any(not math.isfinite(float(value)) for value in values.values()):
        raise ValueError("CEA inputs must be finite")
    if mixture_ratio <= 0 or chamber_pressure_mpa <= 0 or exit_area_ratio <= 1:
        raise ValueError("O/F and chamber pressure must be positive; exit area ratio must exceed 1")
    if fuel_temperature_k <= 0 or oxidizer_temperature_k <= 0:
        raise ValueError("Propellant temperatures must be positive")

    try:
        import cea
        import numpy as np
    except ImportError as error:  # pragma: no cover - package dependency guard
        raise RuntimeError("NASA CEA screening requires the 'cea' package") from error

    reactant_names = [fuel_name, oxidizer_name]
    reactants = cea.Mixture(reactant_names)
    products = cea.Mixture(reactant_names, products_from_reactants=True)
    fuel_weights = np.array([1.0, 0.0])
    oxidizer_weights = np.array([0.0, 1.0])
    weights = reactants.of_ratio_to_weights(oxidizer_weights, fuel_weights, mixture_ratio)
    inlet_temperatures = np.array([fuel_temperature_k, oxidizer_temperature_k])
    chamber_enthalpy = (
        reactants.calc_property(cea.ENTHALPY, weights, inlet_temperatures) / cea.R
    )
    solver = cea.RocketSolver(products, reactants=reactants)
    solution = cea.RocketSolution(solver)
    # CEA expects chamber pressure in bar; the adapter accepts MPa.
    chamber_pressure_bar = chamber_pressure_mpa * 10.0
    solver.solve(
        solution,
        weights,
        chamber_pressure_bar,
        supar=[exit_area_ratio],
        iac=True,
        hc=chamber_enthalpy,
    )

    def last_finite(name: str) -> float:
        array = np.asarray(getattr(solution, name), dtype=float).reshape(-1)
        finite = array[np.isfinite(array)]
        if finite.size == 0:
            raise RuntimeError(f"CEA returned no finite values for {name}")
        return float(finite[-1])

    return CEAIdealScreen(
        model="NASA CEA 3.3.4 RocketSolver; ideal equilibrium expansion",
        propellants={"fuel": fuel_name, "oxidizer": oxidizer_name},
        mixture_ratio=float(mixture_ratio),
        chamber_pressure_mpa=float(chamber_pressure_mpa),
        exit_area_ratio=float(exit_area_ratio),
        chamber_temperature_k=float(np.asarray(solution.T, dtype=float)[0]),
        exit_pressure_bar=last_finite("P"),
        exit_mach=last_finite("Mach"),
        exit_area_ratio_calculated=last_finite("ae_at"),
        characteristic_velocity_m_s=last_finite("c_star"),
        ideal_exit_specific_impulse_m_s=last_finite("Isp"),
        ideal_vacuum_specific_impulse_m_s=last_finite("Isp_vacuum"),
    )


def run_cantera_equilibrium_screen(
    *,
    mechanism: str,
    fuel_species_mass_fractions: dict[str, float],
    oxidizer_species_mass_fractions: dict[str, float],
    mixture_ratio: float,
    chamber_pressure_pa: float,
    fuel_temperature_k: float,
    oxidizer_temperature_k: float,
) -> CanteraEquilibriumScreen:
    """Calculate an adiabatic HP equilibrium state using a user model.

    Species maps are *mass fractions* of each incoming stream and must be
    mapped explicitly for the selected Cantera mechanism. This function does
    not assume an RP-1 surrogate or calculate nozzle performance.
    """
    try:
        import cantera as ct
    except ImportError as error:  # pragma: no cover - package dependency guard
        raise RuntimeError("Cantera screening requires the 'cantera' package") from error

    scalars = (mixture_ratio, chamber_pressure_pa, fuel_temperature_k, oxidizer_temperature_k)
    if any(not math.isfinite(float(value)) for value in scalars):
        raise ValueError("Cantera inputs must be finite")
    if (
        mixture_ratio <= 0
        or chamber_pressure_pa <= 0
        or fuel_temperature_k <= 0
        or oxidizer_temperature_k <= 0
    ):
        raise ValueError("O/F, pressure, and inlet temperatures must be positive")

    def validate_stream(label: str, composition: dict[str, float]) -> dict[str, float]:
        if not composition:
            raise ValueError(f"{label} species mapping must not be empty")
        normalized = {str(name): float(fraction) for name, fraction in composition.items()}
        if any(not math.isfinite(value) or value < 0 for value in normalized.values()):
            raise ValueError(f"{label} mass fractions must be finite and non-negative")
        total = sum(normalized.values())
        if total <= 0 or not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-6):
            raise ValueError(f"{label} mass fractions must sum to 1")
        return {name: fraction / total for name, fraction in normalized.items()}

    fuel = validate_stream("Fuel", fuel_species_mass_fractions)
    oxidizer = validate_stream("Oxidizer", oxidizer_species_mass_fractions)
    gas = ct.Solution(mechanism)
    species_names = set(gas.species_names)
    unknown = sorted((set(fuel) | set(oxidizer)) - species_names)
    if unknown:
        raise ValueError(
            f"Species not found in Cantera mechanism {mechanism!r}: {', '.join(unknown)}"
        )

    gas.TPY = fuel_temperature_k, chamber_pressure_pa, fuel
    fuel_enthalpy = float(gas.enthalpy_mass)
    gas.TPY = oxidizer_temperature_k, chamber_pressure_pa, oxidizer
    oxidizer_enthalpy = float(gas.enthalpy_mass)
    inlet_enthalpy = (fuel_enthalpy + mixture_ratio * oxidizer_enthalpy) / (1.0 + mixture_ratio)
    combined = {
        species: (fuel.get(species, 0.0) + mixture_ratio * oxidizer.get(species, 0.0))
        / (1.0 + mixture_ratio)
        for species in set(fuel) | set(oxidizer)
    }
    gas.TPY = fuel_temperature_k, chamber_pressure_pa, combined
    gas.HP = inlet_enthalpy, chamber_pressure_pa
    gas.equilibrate("HP")
    products = {
        name: float(fraction)
        for name, fraction in zip(gas.species_names, gas.Y, strict=True)
        if fraction > 1e-10
    }
    return CanteraEquilibriumScreen(
        model="Cantera HP equilibrium; user-configured mechanism and stream mapping",
        mechanism=mechanism,
        mixture_ratio=float(mixture_ratio),
        chamber_pressure_pa=float(chamber_pressure_pa),
        equilibrium_temperature_k=float(gas.T),
        mean_molecular_weight_kg_kmol=float(gas.mean_molecular_weight),
        gamma_cp_cv=float(gas.cp_mass / gas.cv_mass),
        density_kg_m3=float(gas.density),
        inlet_mixture_enthalpy_j_kg=float(inlet_enthalpy),
        equilibrium_mass_fractions=products,
    )

