import json
from pathlib import Path

import pytest

from rocket_analysis.cfd import (
    compare_station_mach,
    read_station_csv,
    run_openfoam_case,
)
from rocket_analysis.models import MachStation
from rocket_analysis.rpa4 import load_reference_json


ROOT = Path(__file__).resolve().parents[1]


def test_reference_loads_with_unit_aware_values():
    reference = load_reference_json(ROOT / "data/reference/test1-rpa4.json")

    assert reference.mixture_ratio == 1.5
    assert reference.chamber_pressure.to("MPa").magnitude == 2.0
    assert reference.station_conditions["pressure"][1].to("MPa").magnitude == 1.9759
    assert reference.station_conditions["temperature"][3].to("K").magnitude == 877.2925
    assert reference.throat_diameter.to("inch").magnitude == pytest.approx(6.0)
    assert reference.total_mass_flow.to("kg/s").magnitude == 22.8117
    assert reference.fuel_name == "RP-1"
    assert reference.oxidizer_name == "O2(L)"
    assert reference.delivered_performance["sea_level_thrust"].to("kN").magnitude == 26.7328
    assert reference.ideal_performance["sea_level_thrust"].to("kN").magnitude == 28.3811


def test_reference_json_includes_propellants_and_chamber_pressure():
    data = json.loads((ROOT / "data/reference/test1-rpa4.json").read_text())
    assert data["case"]["fuel"] == "RP-1"
    assert data["case"]["oxidizer"] == "O2(L)"
    assert data["case"]["chamber_pressure"] == {"value": 2.0, "unit": "MPa"}


def test_station_csv_reader(tmp_path):
    csv_path = tmp_path / "mach.csv"
    csv_path.write_text("station,mach\nInjector,0.01\nThroat,1.0\nExit,4.2\n")

    stations = read_station_csv(csv_path)

    assert [point.name for point in stations] == ["Injector", "Throat", "Exit"]
    assert [point.mach for point in stations] == [0.01, 1.0, 4.2]


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("name,value\nInjector,0.1\n", "station.*mach"),
        ("station,mach\n,0.1\n", "non-empty station name"),
        ("station,mach\nInjector,nan\n", "finite and non-negative"),
        ("station,mach\nInjector,-0.1\n", "finite and non-negative"),
        ("station,mach\nInjector,not-a-number\n", "Invalid Mach value"),
        ("station,mach\nInjector,0.0\ninjector,0.1\n", "duplicate station name"),
    ],
)
def test_station_csv_reader_rejects_bad_rows(tmp_path, contents, message):
    csv_path = tmp_path / "bad-mach.csv"
    csv_path.write_text(contents)

    with pytest.raises(ValueError, match=message):
        read_station_csv(csv_path)


def test_station_mach_comparison_matches_case_insensitive_names():
    reference = (MachStation("Nozzle throat ", 1.0, "rpa4-report-table"),)
    result = (MachStation("nozzle throat", 1.04, "solver-result"),)

    comparison = compare_station_mach(reference, result)[0]

    assert comparison.name == "Nozzle throat"
    assert comparison.delta_mach == pytest.approx(0.04)


def test_rpa4_reference_round_trips_through_cfd_csv_comparison(tmp_path):
    reference = load_reference_json(ROOT / "data/reference/test1-rpa4.json")
    csv_path = tmp_path / "rpa4-stations.csv"
    csv_path.write_text(
        "station,mach\n"
        + "".join(f"{point.name},{point.mach}\n" for point in reference.mach_stations)
    )

    result = read_station_csv(csv_path)
    comparisons = compare_station_mach(reference.mach_stations, result)

    assert len(comparisons) == 4
    assert [point.delta_mach for point in comparisons] == [0.0, 0.0, 0.0, 0.0]


def test_station_mach_comparison_rejects_missing_and_duplicate_stations():
    reference = (MachStation("Injector", 0.0), MachStation("Throat", 1.0))
    with pytest.raises(ValueError, match="missing reference stations: Throat"):
        compare_station_mach(reference, (MachStation("Injector", 0.01),))
    with pytest.raises(ValueError, match="duplicate station name"):
        compare_station_mach(
            (MachStation("Injector", 0.0),),
            (MachStation("Injector", 0.0), MachStation("injector", 0.1)),
        )


def test_station_mach_comparison_rejects_non_finite_results():
    with pytest.raises(ValueError, match="finite and non-negative"):
        compare_station_mach(
            (MachStation("Exit", 4.1),),
            (MachStation("Exit", float("inf")),),
        )


def test_openfoam_runner_uses_wsl_and_quotes_case_path(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "completed"

    monkeypatch.setattr("rocket_analysis.cfd.subprocess.run", fake_run)
    result = run_openfoam_case("/home/rocket/cases/test case")

    assert result == "completed"
    assert captured["args"] == [
        "wsl.exe",
        "-d",
        "Ubuntu-24.04",
        "--",
        "openfoam2512",
        "-c",
        "cd -- '/home/rocket/cases/test case' && ./Allrun",
    ]
    assert captured["kwargs"]["check"] is True


def test_openfoam_runner_rejects_non_linux_paths():
    with pytest.raises(ValueError, match="absolute Linux path"):
        run_openfoam_case("C:\\cases\\test1")
