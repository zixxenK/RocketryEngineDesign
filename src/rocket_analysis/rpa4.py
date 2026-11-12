"""Load the checked-in RPA4 reference and extract PDF report text."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pdfplumber

from rocket_analysis.models import MachStation, Q_, RPA4Reference

_NUMBER = re.compile(r"(?<![A-Za-z])[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?")


def _quantity(value: dict[str, Any]) -> Any:
    return Q_(value["value"], value["unit"])


def _quantity_tree(values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _quantity(value)
        if isinstance(value, dict) and {"value", "unit"}.issubset(value)
        else value
        for key, value in values.items()
    }


def load_reference_json(path: str | Path) -> RPA4Reference:
    """Load normalized values transcribed from an RPA4 report."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    case = data["case"]
    flows = data["mass_flow"]
    performance = data["performance"]
    geometry = _quantity_tree(data["geometry"])
    station_conditions = data["station_conditions"]
    station_conditions = {
        key: tuple(_quantity(value) for value in values)
        if values and isinstance(values[0], dict) and {"value", "unit"}.issubset(values[0])
        else tuple(values)
        for key, values in station_conditions.items()
    }
    return RPA4Reference(
        case_id=case["case_id"],
        source_pdf=case["source_pdf"],
        fuel_name=case.get("fuel"),
        oxidizer_name=case.get("oxidizer"),
        mixture_ratio=case["mixture_ratio"],
        chamber_pressure=_quantity(case["chamber_pressure"]),
        throat_diameter=geometry["throat_diameter"],
        total_mass_flow=_quantity(flows["total"]),
        oxidizer_mass_flow=_quantity(flows["oxidizer"]),
        fuel_mass_flow=_quantity(flows["fuel"]),
        ideal_performance=_quantity_tree(performance["ideal"]),
        delivered_performance=_quantity_tree(performance["estimated_delivered"]),
        geometry=geometry,
        station_conditions=station_conditions,
        mach_stations=tuple(
            MachStation(**station) for station in data["mach_stations"]
        ),
        notes=tuple(data.get("notes", [])),
    )


def extract_report(path: str | Path) -> str:
    """Extract searchable text from a text-based RPA4 PDF report."""
    pdf_path = Path(path)
    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)
    with pdfplumber.open(pdf_path) as pdf:
        pages = [page.extract_text(layout=True) or "" for page in pdf.pages]
    text = "\n\n".join(pages).strip()
    if not text:
        raise ValueError(f"No selectable text found in {pdf_path}; OCR may be needed.")
    return text


def _table_section(text: str, heading: str, next_heading: str) -> str:
    start = text.casefold().find(heading.casefold())
    if start < 0:
        raise ValueError(f"Could not find '{heading}' in the report")
    end = text.casefold().find(next_heading.casefold(), start + len(heading))
    return text[start:] if end < 0 else text[start:end]


def _row_numbers(section: str, label: str, count: int) -> list[float]:
    for line in section.splitlines():
        match = re.search(
            rf"(?<![A-Za-z0-9]){re.escape(label)}(?![A-Za-z0-9])",
            line,
            flags=re.IGNORECASE,
        )
        if match:
            if label.casefold() == "thrust" and re.match(
                r"\s+coefficient\b", line[match.end() :], flags=re.IGNORECASE
            ):
                continue
            values = [float(value) for value in _NUMBER.findall(line[match.end() :])]
            if len(values) >= count:
                return values[:count]
    raise ValueError(f"Could not extract '{label}' from report table")


def _table_performance(section: str) -> dict[str, Any]:
    return {
        "altitude_km": _row_numbers(section, "Altitude", 2) + [None],
        "ambient_pressure_atm": _row_numbers(section, "Ambient pressure", 3),
        "characteristic_velocity_m_s": _row_numbers(
            section, "Characteristic velocity", 1
        )[0],
        "effective_exhaust_velocity_m_s": _row_numbers(
            section, "Effective exhaust velocity", 3
        ),
        "specific_impulse_s": _row_numbers(
            section, "Specific impulse (by weight)", 3
        ),
        "thrust_coefficient": _row_numbers(section, "Thrust coefficient", 3),
        "thrust_kN": _row_numbers(section, "Thrust", 3),
    }


def _propellant_component(section: str, name: str) -> dict[str, Any]:
    values = _row_numbers(section, name, 5)
    return {
        "name": name,
        "temperature_k": values[0],
        "pressure_mpa": values[1],
        "enthalpy_kj_mol": values[2],
        "enthalpy_kj_kg": values[3],
        "mass_fraction": values[4],
    }


def _performance_rows(section: str) -> list[list[float]]:
    """Return numeric rows from the altitude performance table.

    Do not impose an altitude or pressure ceiling: valid sweeps can include
    higher altitudes and very low pressures.
    """
    rows: list[list[float]] = []
    for line in section.splitlines():
        values = [float(value) for value in _NUMBER.findall(line)]
        if len(values) >= 6 and values[0] >= 0.0 and values[1] >= 0.0:
            rows.append(values[:6])
    return rows


def _condensed_mass_fractions(section: str) -> list[float] | None:
    """Extract condensed propellant mass fraction at the four stations."""
    for line in section.splitlines():
        match = re.search(r"\bCondense", line, flags=re.IGNORECASE)
        if not match:
            continue
        values = [float(value) for value in _NUMBER.findall(line[match.end() :])]
        # RPA4 prints mass/mole fractions for injector, inlet, throat, and exit.
        if len(values) >= 8:
            return [values[index] for index in (0, 2, 4, 6)]
    return None


def parse_report_values(path: str | Path) -> dict[str, Any]:
    """Extract benchmark conditions, station results, and performance tables."""
    text = extract_report(path)
    propellant_section = _table_section(text, "Table 1. Propellant Specification", "Table 2.")
    station_section = _table_section(text, "Table 2. Combustion Properties", "Table 3.")
    ideal = _table_section(text, "Table 4. Ideal Performance", "Table 5.")
    delivered = _table_section(
        text, "Table 5. Estimated Delivered Performance", "Table 6."
    )
    products = _table_section(text, "Table 3. Combustion Products", "Table 4.")
    condensed_mass_fractions = _condensed_mass_fractions(products)
    geometry = _table_section(text, "Table 8. Chamber Size", "Table 9.")
    altitude = _table_section(text, "Table 6. Altitude Performance", "Table 7.")
    flow_start = text.casefold().find("mass flow rate")
    if flow_start < 0:
        raise ValueError("Could not find mass flow rate in the report")
    flow_section = text[flow_start:]
    case_match = re.search(
        r"Case\s+name:\s*(.*?)\s+Description:", text, flags=re.IGNORECASE | re.DOTALL
    )
    if case_match is None:
        raise ValueError("Could not extract the report case name")
    station_names = ["Injector", "Nozzle inlet", "Nozzle throat", "Nozzle exit"]
    return {
        "case_name": re.sub(r"\s+", " ", case_match.group(1)).strip(),
        "propellants": {
            "fuel": _propellant_component(propellant_section, "RP-1"),
            "oxidizer": _propellant_component(propellant_section, "O2(L)"),
        },
        "mixture_ratio": _row_numbers(propellant_section, "O/F:", 1)[0],
        "station_names": station_names,
        "station_conditions": {
            "pressure_mpa": _row_numbers(station_section, "Pressure", 4),
            "temperature_k": _row_numbers(station_section, "Temperature", 4),
            "mach": _row_numbers(station_section, "Mach number", 4),
            "area_ratio": _row_numbers(station_section, "Area ratio", 4),
            "gamma": _row_numbers(station_section, "Gamma", 4),
            "molecular_weight": _row_numbers(station_section, "Molecular weight", 4),
        },
        "condensed_fraction_reported": condensed_mass_fractions is not None,
        "condensed_mass_fraction_by_station": condensed_mass_fractions,
        "ideal": _table_performance(ideal),
        "estimated_delivered": _table_performance(delivered),
        "total_mass_flow_kg_s": _row_numbers(flow_section, "Mass flow rate", 1)[0],
        "oxidizer_mass_flow_kg_s": _row_numbers(flow_section, "oxidizer", 1)[0],
        "fuel_mass_flow_kg_s": _row_numbers(flow_section, "fuel", 1)[0],
        "number_of_chambers": int(_row_numbers(flow_section, "Number of chambers", 1)[0]),
        "altitude_performance_rows": _performance_rows(altitude),
        "geometry": {
            "chamber_diameter_mm": _row_numbers(geometry, "Dc", 1)[0],
            "throat_diameter_mm": _row_numbers(geometry, "Dt", 1)[0],
            "exit_diameter_mm": _row_numbers(geometry, "De", 1)[0],
            "contraction_ratio": _row_numbers(geometry, "Ac/At", 1)[0],
            "chamber_length_mm": _row_numbers(geometry, "Lc", 1)[0],
            "cylindrical_chamber_length_mm": _row_numbers(geometry, "Lcyl", 1)[0],
            "chamber_angle_deg": _row_numbers(geometry, "b", 1)[0],
            "nozzle_inlet_radius_mm": _row_numbers(geometry, "R1", 1)[0],
            "nozzle_throat_rounding_radius_mm": _row_numbers(geometry, "Rn", 1)[0],
            "combustion_blend_radius_mm": _row_numbers(geometry, "R2", 1)[0],
            "nozzle_convergence_angle_deg": _row_numbers(geometry, "Tn", 1)[0],
            "nozzle_exit_angle_deg": _row_numbers(geometry, "Te", 1)[0],
            "exit_to_throat_area_ratio": _row_numbers(geometry, "Ae/At", 1)[0],
        },
    }


def mach_stations_from_report_values(values: dict[str, Any]) -> tuple[MachStation, ...]:
    """Build station samples directly from parsed RPA4 report values."""
    conditions = values["station_conditions"]
    return tuple(
        MachStation(name=name, mach=float(mach), confidence="rpa4-report-table")
        for name, mach in zip(values["station_names"], conditions["mach"], strict=True)
    )


def parse_report_mach_stations(path: str | Path) -> tuple[MachStation, ...]:
    """Extract exact Mach samples from an RPA4 PDF report."""
    return mach_stations_from_report_values(parse_report_values(path))


def report_contains_reference_values(path: str | Path) -> dict[str, bool]:
    """Check that key values from the supplied report are extractable.

    This is a benchmark-specific smoke check, not a general RPA4 PDF parser.
    """
    values = parse_report_values(path)
    ideal = values["ideal"]
    delivered = values["estimated_delivered"]
    geometry = values["geometry"]
    checks = {
        "fuel is RP-1": values["propellants"]["fuel"]["name"] == "RP-1",
        "oxidizer is liquid oxygen": values["propellants"]["oxidizer"]["name"] == "O2(L)",
        "mixture ratio": abs(values["mixture_ratio"] - 1.5) < 1e-4,
        "chamber pressure": abs(values["station_conditions"]["pressure_mpa"][0] - 2.0) < 1e-4,
        "ideal c_star": (ideal["characteristic_velocity_m_s"], 1642.5709),
        "delivered c_star": (delivered["characteristic_velocity_m_s"], 1589.7027),
        "sea-level delivered thrust": (delivered["thrust_kN"][0], 26.7328),
        "sea-level delivered specific impulse": (delivered["specific_impulse_s"][0], 119.4993),
        "optimum expansion altitude": (delivered["altitude_km"][1], 16.9418),
        "optimum expansion ambient pressure": (delivered["ambient_pressure_atm"][1], 0.0881),
        "total mass flow": (values["total_mass_flow_kg_s"], 22.8117),
        "throat diameter": (geometry["throat_diameter_mm"], 152.40),
        "exit area ratio": (geometry["exit_to_throat_area_ratio"], 19.74),
    }
    result: dict[str, bool] = {}
    for name, check in checks.items():
        if isinstance(check, bool):
            result[name] = check
        else:
            actual, expected = check
            result[name] = abs(actual - expected) < 1e-4
    for index, expected in enumerate((0.0, 0.0988, 1.0, 4.1322)):
        result[f"Mach at {values['station_names'][index]}"] = (
            abs(values["station_conditions"]["mach"][index] - expected) < 1e-4
        )
    return result
