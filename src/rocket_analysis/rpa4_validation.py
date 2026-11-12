"""Compare a native RPA 4 configuration with its exported PDF report."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from pint.errors import DimensionalityError, UndefinedUnitError

from rocket_analysis.models import Q_, RPA4Inputs
from rocket_analysis.rpa4 import parse_report_values


def load_rpa4_config(path: str | Path) -> dict[str, Any]:
    """Load and minimally validate an RPA 4 JSON ``.cfg`` file."""
    config_path = Path(path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if str(data.get("application", "")).casefold() != "rpa":
        raise ValueError("Configuration application must be 'RPA'")
    version = data.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, (int, float))
        or not math.isfinite(version)
        or version <= 0
    ):
        raise ValueError("Configuration is missing a valid numeric version")
    engine = data.get("engineDefinition")
    if not isinstance(engine, dict) or not str(engine.get("name", "")).strip():
        raise ValueError("Configuration is missing engineDefinition.name")
    mixture = engine.get("bipropellantMixture")
    if not isinstance(mixture, dict):
        raise ValueError("Configuration is missing engineDefinition.bipropellantMixture")
    for role in ("fuel", "oxidizer"):
        components = mixture.get(role)
        if not isinstance(components, list) or not components:
            raise ValueError(f"Configuration must contain at least one {role} component")
        for component in components:
            try:
                fraction = float(component["massFraction"])
                name = str(component["name"]).strip()
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"Invalid {role} component in configuration") from error
            if not name or not math.isfinite(fraction) or not 0.0 <= fraction <= 1.0:
                raise ValueError(f"Invalid {role} component name or mass fraction")
        fraction_sum = sum(float(component["massFraction"]) for component in components)
        if not math.isclose(fraction_sum, 1.0, rel_tol=0.0, abs_tol=1e-6):
            raise ValueError(f"{role.capitalize()} component mass fractions must sum to 1")
    try:
        mixture_ratio = float(mixture["mixtureRatio"]["value"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Configuration is missing a valid O/F mixture ratio") from error
    if not math.isfinite(mixture_ratio) or mixture_ratio <= 0.0:
        raise ValueError("O/F mixture ratio must be finite and positive")
    try:
        chamber_pressure = float(
            Q_(engine["chamberPressure"]["value"], engine["chamberPressure"]["units"])
            .to("MPa")
            .magnitude
        )
    except (KeyError, TypeError, ValueError, OverflowError, DimensionalityError, UndefinedUnitError) as error:
        raise ValueError("Configuration must provide chamber pressure with convertible units") from error
    if not math.isfinite(chamber_pressure) or chamber_pressure <= 0.0:
        raise ValueError("Chamber pressure must be finite and positive")
    if not isinstance(data.get("nozzleConditions"), dict):
        raise ValueError("Configuration is missing nozzleConditions")
    nozzle_conditions = data["nozzleConditions"]
    for key in ("nozzleInletConditions", "nozzleExitConditions", "ambientConditions"):
        if not isinstance(nozzle_conditions.get(key), dict):
            raise ValueError(f"Configuration is missing nozzleConditions.{key}")
    for key in ("nozzleInletConditions", "nozzleExitConditions"):
        try:
            area_ratio = float(nozzle_conditions[key]["areaRatio"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Configuration is missing {key}.areaRatio") from error
        if not math.isfinite(area_ratio) or area_ratio <= 0.0:
            raise ValueError(f"{key}.areaRatio must be finite and positive")
    ambient = nozzle_conditions["ambientConditions"].get("range")
    if not isinstance(ambient, dict):
        raise ValueError("Configuration is missing nozzleConditions.ambientConditions.range")
    try:
        pressure_from = float(Q_(ambient["from"]["value"], ambient["from"]["units"]).to("atm").magnitude)
        pressure_to = float(Q_(ambient["to"]["value"], ambient["to"]["units"]).to("atm").magnitude)
        point_count = int(ambient["numberOfPoints"])
        if float(ambient["numberOfPoints"]) != point_count:
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError, DimensionalityError, UndefinedUnitError) as error:
        raise ValueError("Ambient range must have convertible pressure endpoints and an integer point count") from error
    if (
        not math.isfinite(pressure_from)
        or not math.isfinite(pressure_to)
        or pressure_from <= 0.0
        or pressure_to <= 0.0
        or point_count < 2
    ):
        raise ValueError("Ambient pressure endpoints must be positive and the point count at least two")
    size = data.get("thrustChamberSize")
    if not isinstance(size, dict) or not isinstance(size.get("sizing"), dict):
        raise ValueError("Configuration is missing thrustChamberSize.sizing")
    if not isinstance(size.get("combustionChamberGeometry"), dict):
        raise ValueError("Configuration is missing thrustChamberSize.combustionChamberGeometry")
    if not isinstance(size.get("nozzleGeometry"), dict):
        raise ValueError("Configuration is missing thrustChamberSize.nozzleGeometry")
    chamber_geometry = size["combustionChamberGeometry"]
    nozzle_geometry = size["nozzleGeometry"]
    contour = nozzle_geometry.get("parabolicContour")
    if not isinstance(contour, dict):
        raise ValueError("Configuration is missing nozzleGeometry.parabolicContour")
    for field in ("contractionAngle", "R1toRt", "R2toR2max"):
        try:
            value = float(chamber_geometry[field])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Configuration is missing combustionChamberGeometry.{field}") from error
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"combustionChamberGeometry.{field} must be finite and positive")
    for field in ("RntoRt",):
        try:
            value = float(nozzle_geometry[field])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Configuration is missing nozzleGeometry.{field}") from error
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"nozzleGeometry.{field} must be finite and positive")
    for field in ("Tn", "Te"):
        try:
            value = float(contour[field])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Configuration is missing parabolicContour.{field}") from error
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"parabolicContour.{field} must be finite and positive")
    sizing = size["sizing"]
    try:
        throat_diameter_mm = float(
            Q_(sizing["throatDiameter"]["value"], sizing["throatDiameter"]["units"])
            .to("mm")
            .magnitude
        )
    except (KeyError, TypeError, ValueError, OverflowError, DimensionalityError, UndefinedUnitError) as error:
        raise ValueError("Configuration must provide a throat diameter with convertible units") from error
    if not math.isfinite(throat_diameter_mm) or throat_diameter_mm <= 0.0:
        raise ValueError("Throat diameter must be finite and positive")
    try:
        chamber_count = int(sizing["numberOfChambers"])
        if float(sizing["numberOfChambers"]) != chamber_count or chamber_count < 1:
            raise ValueError
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Number of chambers must be a positive integer") from error
    return data


def _config_chamber_length(geometry: dict[str, Any]) -> tuple[float | None, str | None]:
    length = geometry.get("chamberLength")
    if not isinstance(length, dict):
        return None, None
    value = length.get("value")
    if isinstance(value, dict):
        unit = value.get("units") or length.get("units")
        value = value.get("value")
    else:
        unit = length.get("units")
    try:
        numeric_value = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("Configured chamber length must be numeric") from error
    if not math.isfinite(numeric_value) or numeric_value <= 0.0:
        raise ValueError("Configured chamber length must be finite and positive")
    return numeric_value, unit


def _read_bool(values: dict[str, Any], key: str, *, default: bool = False) -> bool:
    value = values.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"Configuration field {key!r} must be a boolean")
    return value


def load_rpa4_inputs(path: str | Path) -> RPA4Inputs:
    """Import the transferable RPA 4 CFG into unit-aware toolkit inputs."""
    config = load_rpa4_config(path)
    engine = config["engineDefinition"]
    mixture = engine["bipropellantMixture"]
    size = config["thrustChamberSize"]
    sizing = size["sizing"]
    chamber_geometry = size["combustionChamberGeometry"]
    nozzle_geometry = size["nozzleGeometry"]
    contour = nozzle_geometry.get("parabolicContour", {})
    nozzle_conditions = config["nozzleConditions"]
    ambient = nozzle_conditions["ambientConditions"]["range"]
    chamber_length_value, chamber_length_unit = _config_chamber_length(chamber_geometry)

    propellants = {
        role: tuple(
            (component["name"], float(component["massFraction"]))
            for component in mixture[role]
        )
        for role in ("fuel", "oxidizer")
    }
    return RPA4Inputs(
        source_file=Path(path).name,
        version=float(config["version"]),
        case_name=str(engine["name"]).strip(),
        propellants=propellants,
        mixture_ratio=float(mixture["mixtureRatio"]["value"]),
        chamber_pressure=Q_(engine["chamberPressure"]["value"], engine["chamberPressure"]["units"]),
        number_of_chambers=int(sizing["numberOfChambers"]),
        throat_diameter=Q_(sizing["throatDiameter"]["value"], sizing["throatDiameter"]["units"]),
        nozzle_inlet_area_ratio=float(nozzle_conditions["nozzleInletConditions"]["areaRatio"]),
        nozzle_exit_area_ratio=float(nozzle_conditions["nozzleExitConditions"]["areaRatio"]),
        chamber_length_value=chamber_length_value,
        chamber_length_unit=chamber_length_unit,
        chamber_contraction_angle=Q_(chamber_geometry["contractionAngle"], "degree"),
        r1_to_rt=float(chamber_geometry["R1toRt"]),
        r2_to_r2max=float(chamber_geometry["R2toR2max"]),
        nozzle_throat_radius_to_rt=float(nozzle_geometry["RntoRt"]),
        nozzle_convergence_angle=Q_(contour["Tn"], "degree"),
        nozzle_exit_angle=Q_(contour["Te"], "degree"),
        ambient_pressure_from=Q_(ambient["from"]["value"], ambient["from"]["units"]),
        ambient_pressure_to=Q_(ambient["to"]["value"], ambient["to"]["units"]),
        ambient_pressure_point_count=int(ambient["numberOfPoints"]),
        equilibrium_flow_shifting=_read_bool(
            nozzle_conditions.get("equilibriumFlow", {}), "shifting"
        ),
        reaction_efficiency_estimated=_read_bool(
            nozzle_conditions.get("reactionEficiency", {}), "estimateReactionEfficiency"
        ),
        bell_nozzle_efficiency_estimated=_read_bool(
            nozzle_conditions.get("nozzleEficiency", {}), "estimateBellNozzleEfficicncy"
        ),
        nozzle_efficiency_applied=_read_bool(nozzle_geometry, "applyNozzleEfficiency"),
        multiphase=_read_bool(nozzle_conditions.get("nozzleFlowEffcets", {}), "multiphase"),
    )


def validate_rpa4_case(
    config_path: str | Path,
    report_path: str | Path,
) -> dict[str, Any]:
    """Compare supported inputs in an RPA 4 config with its report tables.

    This validates config/report consistency. It does not execute the RPA 4
    application or claim that the report was generated from this exact file.
    """
    inputs = load_rpa4_inputs(config_path)
    report = parse_report_values(report_path)

    checks: list[dict[str, Any]] = []

    def add(
        field: str,
        configured: Any,
        reported: Any,
        *,
        tolerance: float = 1e-4,
        note: str | None = None,
        status: str | None = None,
    ) -> None:
        if status is None:
            if isinstance(configured, (int, float)) and isinstance(reported, (int, float)):
                status = "match" if math.isclose(
                    float(configured), float(reported), rel_tol=0.0, abs_tol=tolerance
                ) else "mismatch"
            else:
                status = "match" if configured == reported else "mismatch"
        check = {
            "field": field,
            "configured": configured,
            "reported": reported,
            "status": status,
        }
        if note:
            check["note"] = note
        checks.append(check)

    report_case_name = re.sub(r"\s+", " ", report["case_name"]).strip().casefold()
    config_case_name = re.sub(r"\s+", " ", inputs.case_name).strip().casefold()
    add("case name", config_case_name, report_case_name)

    for role in ("fuel", "oxidizer"):
        components = inputs.propellants[role]
        reported_component = report["propellants"][role]
        configured_names = [name for name, _ in components]
        configured_identity: str | list[str] = (
            configured_names[0] if len(configured_names) == 1 else configured_names
        )
        add(f"{role} identity", configured_identity, reported_component["name"])
        add(
            f"{role} blend fractions sum",
            sum(fraction for _, fraction in components),
            1.0,
        )

    mixture_ratio = inputs.mixture_ratio
    add(
        "mixture ratio O/F",
        mixture_ratio,
        report["mixture_ratio"],
    )
    add(
        "reported mass-flow O/F",
        report["oxidizer_mass_flow_kg_s"] / report["fuel_mass_flow_kg_s"],
        mixture_ratio,
    )
    add(
        "fuel share of total propellant mass",
        1.0 / (1.0 + mixture_ratio),
        report["propellants"]["fuel"]["mass_fraction"],
    )
    add(
        "oxidizer share of total propellant mass",
        mixture_ratio / (1.0 + mixture_ratio),
        report["propellants"]["oxidizer"]["mass_fraction"],
    )
    add(
        "chamber pressure (MPa)",
        float(inputs.chamber_pressure.to("MPa").magnitude),
        report["station_conditions"]["pressure_mpa"][0],
        tolerance=1e-3,
    )
    add(
        "throat diameter (mm)",
        float(inputs.throat_diameter.to("mm").magnitude),
        report["geometry"]["throat_diameter_mm"],
        tolerance=1e-3,
    )
    add(
        "nozzle inlet area ratio",
        inputs.nozzle_inlet_area_ratio,
        report["station_conditions"]["area_ratio"][1],
        status="review",
        note=(
            "Numeric difference: CFG nozzleConditions.nozzleInletConditions.areaRatio is "
            "compared with Table 2 Nozzle inlet A/At. The supplied files do not establish "
            "whether RPA4 maps these fields to the same calculation station. Sweep the CFG "
            "input if desired, but do not use this PDF station value as a direct equality gate."
        ),
    )
    add(
        "nozzle exit area ratio",
        inputs.nozzle_exit_area_ratio,
        report["station_conditions"]["area_ratio"][3],
    )
    add(
        "chamber contraction ratio",
        report["geometry"]["contraction_ratio"],
        report["station_conditions"]["area_ratio"][0],
        note="Internal report check: chamber Ac/At versus injector station area ratio.",
    )

    chamber_length = inputs.chamber_length_value
    chamber_length_unit = inputs.chamber_length_unit
    if chamber_length is None:
        add(
            "chamber length",
            None,
            report["geometry"]["chamber_length_mm"],
            status="unavailable",
            note="No chamber length value was found in the configuration.",
        )
    elif chamber_length_unit:
        configured_length_mm = float(Q_(chamber_length, chamber_length_unit).to("mm").magnitude)
        add(
            "chamber length (mm)",
            configured_length_mm,
            report["geometry"]["chamber_length_mm"],
            tolerance=1e-2,
        )
    else:
        add(
            "chamber length",
            chamber_length,
            report["geometry"]["chamber_length_mm"],
            status="review",
            note="Numeric values agree, but the CFG does not specify a unit for this field.",
        )

    add(
        "chamber contraction angle (deg)",
        float(inputs.chamber_contraction_angle.to("degree").magnitude),
        report["geometry"]["chamber_angle_deg"],
    )
    add(
        "R1 / throat radius",
        inputs.r1_to_rt,
        report["geometry"]["nozzle_inlet_radius_mm"]
        / (report["geometry"]["throat_diameter_mm"] / 2.0),
        tolerance=1e-3,
    )
    add(
        "nozzle throat radius / throat radius",
        inputs.nozzle_throat_radius_to_rt,
        report["geometry"]["nozzle_throat_rounding_radius_mm"]
        / (report["geometry"]["throat_diameter_mm"] / 2.0),
        tolerance=1e-3,
    )
    add(
        "R2 / R2max ratio",
        inputs.r2_to_r2max,
        None,
        status="unavailable",
        note="The report gives R2 but not R2max, so this ratio cannot be checked.",
    )
    add(
        "nozzle convergence angle (deg)",
        float(inputs.nozzle_convergence_angle.to("degree").magnitude),
        report["geometry"]["nozzle_convergence_angle_deg"],
    )
    add(
        "nozzle exit angle (deg)",
        float(inputs.nozzle_exit_angle.to("degree").magnitude),
        report["geometry"]["nozzle_exit_angle_deg"],
    )
    report_has_estimated_delivered = (
        report["estimated_delivered"]["thrust_kN"] != report["ideal"]["thrust_kN"]
    )
    add(
        "reaction efficiency estimate enabled",
        inputs.reaction_efficiency_estimated,
        report_has_estimated_delivered,
        status="match"
        if inputs.reaction_efficiency_estimated and report_has_estimated_delivered
        else "review",
        note="The report contains separate ideal and estimated-delivered performance tables.",
    )
    add(
        "bell nozzle efficiency applied",
        inputs.bell_nozzle_efficiency_estimated and inputs.nozzle_efficiency_applied,
        report_has_estimated_delivered,
        status="match"
        if inputs.bell_nozzle_efficiency_estimated
        and inputs.nozzle_efficiency_applied
        and report_has_estimated_delivered
        else "review",
        note="The report contains separate ideal and estimated-delivered performance tables.",
    )
    add(
        "multiphase flow enabled",
        inputs.multiphase,
        report["condensed_fraction_reported"],
    )
    add(
        "chamber count",
        inputs.number_of_chambers,
        report["number_of_chambers"],
    )

    ambient_low = float(inputs.ambient_pressure_from.to("atm").magnitude)
    ambient_high = float(inputs.ambient_pressure_to.to("atm").magnitude)
    altitude_pressures = [row[1] for row in report["altitude_performance_rows"]]
    if altitude_pressures:
        add("ambient pressure range start (atm)", ambient_low, altitude_pressures[0])
        add("ambient pressure range end (atm)", ambient_high, altitude_pressures[-1])
    else:
        add(
            "ambient pressure range",
            [ambient_low, ambient_high],
            None,
            status="unavailable",
            note="No altitude pressure rows were extracted from the report.",
        )
    point_count = inputs.ambient_pressure_point_count
    row_count = len(report["altitude_performance_rows"])
    add(
        "ambient pressure sample count",
        point_count,
        row_count,
        status="match" if point_count == row_count else "review",
        note="The PDF has both endpoint rows; 30 configured intervals can produce 31 rows."
        if point_count + 1 == row_count
        else None,
    )

    shifting = inputs.equilibrium_flow_shifting
    molecular_weights = report["station_conditions"]["molecular_weight"]
    add(
        "equilibrium shifting mode",
        shifting,
        molecular_weights,
        status="review",
        note="Report molecular weight varies by station. Confirm the RPA 4.06 meaning of `shifting: false` before treating this as a mode mismatch.",
    )

    delivered = report["estimated_delivered"]
    nozzle_exit_pressure_atm = float(
        Q_(report["station_conditions"]["pressure_mpa"][3], "MPa").to("atm").magnitude
    )
    add(
        "nozzle exit pressure matches optimum-expansion ambient pressure (atm)",
        nozzle_exit_pressure_atm,
        delivered["ambient_pressure_atm"][1],
        tolerance=5e-4,
        note="Checks that the PDF's exit pressure matches its reported optimum-expansion ambient pressure.",
    )
    sea_level_ve = delivered["effective_exhaust_velocity_m_s"][0]
    sea_level_isp_ve = delivered["specific_impulse_s"][0] * 9.80665
    add(
        "delivered sea-level Isp / exhaust-velocity closure (m/s)",
        sea_level_isp_ve,
        sea_level_ve,
        tolerance=0.02,
        note="Internal report consistency check using standard gravity.",
    )
    thrust_from_flow = report["total_mass_flow_kg_s"] * sea_level_ve / 1000.0
    add(
        "delivered sea-level thrust / mass-flow closure (kN)",
        thrust_from_flow,
        delivered["thrust_kN"][0],
        tolerance=0.02,
        note="Internal report consistency check using total mass flow and effective exhaust velocity.",
    )

    counts = {status: sum(check["status"] == status for check in checks) for status in (
        "match", "mismatch", "review", "unavailable"
    )}
    overall = "mismatch" if counts["mismatch"] else "review_required" if counts["review"] else "match"
    return {
        "config_file": Path(config_path).name,
        "report_file": Path(report_path).name,
        "validation_scope": "CFG-to-report input consistency and report internal checks; no RPA 4 solver rerun",
        "status": overall,
        "counts": counts,
        "checks": checks,
        "reported_mach_stations": [
            {"name": name, "mach": mach}
            for name, mach in zip(
                report["station_names"], report["station_conditions"]["mach"], strict=True
            )
        ],
    }
