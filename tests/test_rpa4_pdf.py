from pathlib import Path

from rocket_analysis.rpa4 import (
    extract_report,
    parse_report_values,
    report_contains_reference_values,
)


ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "data/reference/test1-6in-of-1.5.pdf"


def test_pdf_has_extractable_rpa4_report_text():
    text = extract_report(PDF)

    assert "Ideal Performance" in text
    assert "Estimated Delivered Performance" in text
    assert "Chamber Size" in text


def test_supplied_pdf_contains_benchmark_values():
    checks = report_contains_reference_values(PDF)

    assert all(checks.values()), checks


def test_pdf_performance_tables_stay_separate():
    values = parse_report_values(PDF)

    assert values["ideal"]["characteristic_velocity_m_s"] == 1642.5709
    assert values["estimated_delivered"]["characteristic_velocity_m_s"] == 1589.7027
    assert values["estimated_delivered"]["thrust_kN"][0] == 26.7328
    assert values["estimated_delivered"]["altitude_km"] == [0.0, 16.9418, None]
    assert values["estimated_delivered"]["ambient_pressure_atm"] == [1.0, 0.0881, 0.0]
    assert values["geometry"]["throat_diameter_mm"] == 152.40
    assert values["total_mass_flow_kg_s"] == 22.8117


def test_pdf_exposes_configuration_and_exact_station_conditions():
    values = parse_report_values(PDF)

    assert values["case_name"] == "Test 1"
    assert values["propellants"]["fuel"] == {
        "name": "RP-1",
        "temperature_k": 298.1,
        "pressure_mpa": 0.1013,
        "enthalpy_kj_mol": -24.7177,
        "enthalpy_kj_kg": -1768.557,
        "mass_fraction": 0.4,
    }
    assert values["propellants"]["oxidizer"] == {
        "name": "O2(L)",
        "temperature_k": 90.2,
        "pressure_mpa": 0.1013,
        "enthalpy_kj_mol": -12.979,
        "enthalpy_kj_kg": -405.608,
        "mass_fraction": 0.6,
    }
    assert values["mixture_ratio"] == 1.5
    assert values["station_conditions"]["pressure_mpa"] == [2.0, 1.9759, 1.0985, 0.0089]
    assert values["station_conditions"]["mach"] == [0.0, 0.0988, 1.0, 4.1322]
    assert values["geometry"]["exit_to_throat_area_ratio"] == 19.74
    assert values["geometry"]["nozzle_throat_rounding_radius_mm"] == 29.11
    assert values["condensed_fraction_reported"] is True
    assert len(values["altitude_performance_rows"]) == 31
