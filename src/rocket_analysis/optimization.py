"""Discrete ranking of completed RPA4 cases against a fixed design baseline."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from rocket_analysis.models import RPA4Inputs
from rocket_analysis.shared import SharedRPA4Case

OBJECTIVE = "maximize estimated-delivered vacuum specific impulse"
OBJECTIVE_UNIT = "s"


@dataclass(frozen=True)
class RankedRPA4Case:
    case_name: str
    mixture_ratio: float
    objective_value: float
    validation_status: str
    config_path: str
    report_path: str


@dataclass(frozen=True)
class OptimizationExclusion:
    case_name: str
    reason: str


@dataclass(frozen=True)
class RPA4OptimizationResult:
    objective: str
    objective_unit: str
    fixed_conditions: dict[str, Any]
    ranking: tuple[RankedRPA4Case, ...]
    exclusions: tuple[OptimizationExclusion, ...]

    @property
    def winner(self) -> RankedRPA4Case:
        return self.ranking[0]


def _normalized_blend(inputs: RPA4Inputs, role: str) -> tuple[tuple[str, float], ...]:
    return tuple(
        sorted(
            (name.strip().casefold(), float(fraction))
            for name, fraction in inputs.propellants[role]
        )
    )


def _fixed_condition_difference(candidate: RPA4Inputs, baseline: RPA4Inputs) -> str | None:
    for role in ("fuel", "oxidizer"):
        left, right = _normalized_blend(candidate, role), _normalized_blend(baseline, role)
        if len(left) != len(right) or any(
            name_a != name_b or not math.isclose(frac_a, frac_b, rel_tol=0.0, abs_tol=1e-8)
            for (name_a, frac_a), (name_b, frac_b) in zip(left, right, strict=True)
        ):
            return f"{role} blend differs from the baseline"

    quantity_conditions = (
        ("chamber pressure", candidate.chamber_pressure, baseline.chamber_pressure, "MPa"),
        ("throat diameter", candidate.throat_diameter, baseline.throat_diameter, "mm"),
    )
    for label, candidate_value, baseline_value, unit in quantity_conditions:
        left = float(candidate_value.to(unit).magnitude)
        right = float(baseline_value.to(unit).magnitude)
        if not math.isclose(left, right, rel_tol=0.0, abs_tol=1e-6):
            return f"{label} differs from the baseline"

    scalar_conditions = (
        ("RPA4 version", candidate.version, baseline.version),
        ("number of chambers", candidate.number_of_chambers, baseline.number_of_chambers),
        (
            "nozzle inlet area ratio",
            candidate.nozzle_inlet_area_ratio,
            baseline.nozzle_inlet_area_ratio,
        ),
        (
            "nozzle exit area ratio",
            candidate.nozzle_exit_area_ratio,
            baseline.nozzle_exit_area_ratio,
        ),
        ("chamber length", candidate.chamber_length_value, baseline.chamber_length_value),
        ("chamber length unit", candidate.chamber_length_unit, baseline.chamber_length_unit),
        (
            "chamber contraction angle",
            float(candidate.chamber_contraction_angle.to("degree").magnitude),
            float(baseline.chamber_contraction_angle.to("degree").magnitude),
        ),
        ("R1/Rt", candidate.r1_to_rt, baseline.r1_to_rt),
        ("R2/R2max", candidate.r2_to_r2max, baseline.r2_to_r2max),
        ("Rn/Rt", candidate.nozzle_throat_radius_to_rt, baseline.nozzle_throat_radius_to_rt),
        (
            "nozzle convergence angle",
            float(candidate.nozzle_convergence_angle.to("degree").magnitude),
            float(baseline.nozzle_convergence_angle.to("degree").magnitude),
        ),
        (
            "nozzle exit angle",
            float(candidate.nozzle_exit_angle.to("degree").magnitude),
            float(baseline.nozzle_exit_angle.to("degree").magnitude),
        ),
        (
            "equilibrium shifting mode",
            candidate.equilibrium_flow_shifting,
            baseline.equilibrium_flow_shifting,
        ),
        (
            "reaction-efficiency setting",
            candidate.reaction_efficiency_estimated,
            baseline.reaction_efficiency_estimated,
        ),
        (
            "bell-nozzle-efficiency setting",
            candidate.bell_nozzle_efficiency_estimated,
            baseline.bell_nozzle_efficiency_estimated,
        ),
        (
            "nozzle-efficiency setting",
            candidate.nozzle_efficiency_applied,
            baseline.nozzle_efficiency_applied,
        ),
        ("multiphase setting", candidate.multiphase, baseline.multiphase),
        (
            "ambient pressure range start",
            float(candidate.ambient_pressure_from.to("atm").magnitude),
            float(baseline.ambient_pressure_from.to("atm").magnitude),
        ),
        (
            "ambient pressure range end",
            float(candidate.ambient_pressure_to.to("atm").magnitude),
            float(baseline.ambient_pressure_to.to("atm").magnitude),
        ),
        (
            "ambient pressure sample count",
            candidate.ambient_pressure_point_count,
            baseline.ambient_pressure_point_count,
        ),
    )
    for label, left, right in scalar_conditions:
        if isinstance(left, float) and isinstance(right, float):
            same = math.isclose(left, right, rel_tol=0.0, abs_tol=1e-8)
        else:
            same = left == right
        if not same:
            return f"{label} differs from the baseline"
    return None


def _vacuum_delivered_isp(case: SharedRPA4Case) -> float:
    try:
        values = case.report_values["estimated_delivered"]["specific_impulse_s"]
        value = float(values[2])
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise ValueError(
            f"Case {case.case_name!r} has no estimated-delivered vacuum Isp in its report"
        ) from error
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"Case {case.case_name!r} has an invalid estimated-delivered vacuum Isp")
    return value


def optimize_rpa4_cases(
    cases: tuple[SharedRPA4Case, ...] | list[SharedRPA4Case],
    *,
    baseline_inputs: RPA4Inputs,
    allow_validation_mismatches: bool = False,
) -> RPA4OptimizationResult:
    """Rank RPA4 exports by delivered vacuum Isp while varying O/F only.

    Fuel and oxidizer blends, chamber pressure, throat diameter, chamber and
    nozzle geometry, chamber count, and efficiency/flow flags must match the
    supplied baseline. Cases with CFG/PDF mismatches are excluded by default.
    This ranks already-computed RPA4 exports; it does not run RPA4 or invent
    new thermochemistry results.
    """
    ranked: list[RankedRPA4Case] = []
    exclusions: list[OptimizationExclusion] = []
    for case in cases:
        difference = _fixed_condition_difference(case.inputs, baseline_inputs)
        if difference:
            exclusions.append(OptimizationExclusion(case.case_name, difference))
            continue
        status = str(case.validation.get("status", "unknown"))
        if status not in {"match", "review_required", "mismatch"}:
            exclusions.append(
                OptimizationExclusion(case.case_name, f"unsupported validation status: {status}")
            )
            continue
        if status == "mismatch" and not allow_validation_mismatches:
            mismatches = [
                check["field"]
                for check in case.validation.get("checks", [])
                if check.get("status") == "mismatch"
            ]
            detail = ", ".join(mismatches) if mismatches else "CFG/PDF validation mismatch"
            exclusions.append(
                OptimizationExclusion(case.case_name, f"validation mismatch: {detail}")
            )
            continue
        ranked.append(
            RankedRPA4Case(
                case_name=case.case_name,
                mixture_ratio=float(case.inputs.mixture_ratio),
                objective_value=_vacuum_delivered_isp(case),
                validation_status=status,
                config_path=str(case.config_path),
                report_path=str(case.report_path),
            )
        )

    if len(ranked) < 2:
        reasons = "; ".join(f"{item.case_name}: {item.reason}" for item in exclusions)
        suffix = f" Excluded cases: {reasons}." if reasons else ""
        raise ValueError(
            "At least two eligible RPA4 cases are required to compare O/F candidates. "
            "Provide distinct case names with the same fixed design conditions and validated CFG/PDF pairs."
            + suffix
        )

    ranked.sort(
        key=lambda result: (
            -result.objective_value,
            result.mixture_ratio,
            result.case_name.casefold(),
        )
    )
    return RPA4OptimizationResult(
        objective=OBJECTIVE,
        objective_unit=OBJECTIVE_UNIT,
        fixed_conditions={
            "fuel": baseline_inputs.propellants["fuel"],
            "oxidizer": baseline_inputs.propellants["oxidizer"],
            "chamber_pressure": str(baseline_inputs.chamber_pressure.to("MPa")),
            "throat_diameter": str(baseline_inputs.throat_diameter.to("mm")),
            "nozzle_exit_area_ratio": baseline_inputs.nozzle_exit_area_ratio,
            "ambient_pressure_range": [
                str(baseline_inputs.ambient_pressure_from.to("atm")),
                str(baseline_inputs.ambient_pressure_to.to("atm")),
            ],
            "ambient_pressure_sample_count": baseline_inputs.ambient_pressure_point_count,
            "rpa4_version": baseline_inputs.version,
            "all other imported geometry and efficiency/flow settings": "held at baseline values",
        },
        ranking=tuple(ranked),
        exclusions=tuple(exclusions),
    )
