from pathlib import Path

import matplotlib
import pytest

matplotlib.use("Agg")

import matplotlib.image as mpimg
import matplotlib.pyplot as plt

from rocket_analysis.models import MachStation
from rocket_analysis.plotting import plot_mach_reference
from rocket_analysis.rpa4 import mach_stations_from_report_values, parse_report_values


ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "data/reference/test1-6in-of-1.5.pdf"


def test_mach_plot_uses_report_values_and_saves_readable_png(tmp_path):
    report_values = parse_report_values(PDF)
    stations = mach_stations_from_report_values(report_values)
    output = tmp_path / "mach-reference.png"

    fig, ax = plot_mach_reference(
        stations, title="RPA4 Table 2 Mach by station", output=output
    )
    try:
        assert ax.get_title() == "RPA4 Table 2 Mach by station"
        assert ax.get_xlabel() == "Station"
        assert ax.get_ylabel() == "Mach number"
        assert ax.get_ylim()[0] == 0.0
        assert [tick.get_text() for tick in ax.get_xticklabels()] == [
            "Injector", "Nozzle inlet", "Nozzle throat", "Nozzle exit"
        ]
        assert list(ax.lines[0].get_ydata()) == [0.0, 0.0988, 1.0, 4.1322]
        assert list(ax.lines[0].get_xdata()) == [0, 1, 2, 3]
        assert ax.lines[0].get_marker() == "o"
        assert ax.lines[0].get_linestyle() == "None"
        assert [label.get_text() for label in ax.texts] == ["0", "0.0988", "1", "4.1322"]
        image = mpimg.imread(output)
        assert image.shape[0] > 100
        assert image.shape[1] > 100
        assert output.stat().st_size > 1000
    finally:
        plt.close(fig)


def test_report_mach_samples_keep_exact_names_and_values():
    stations = mach_stations_from_report_values(parse_report_values(PDF))

    assert [(point.name, point.mach, point.confidence) for point in stations] == [
        ("Injector", 0.0, "rpa4-report-table"),
        ("Nozzle inlet", 0.0988, "rpa4-report-table"),
        ("Nozzle throat", 1.0, "rpa4-report-table"),
        ("Nozzle exit", 4.1322, "rpa4-report-table"),
    ]


def test_mach_plot_rejects_empty_or_invalid_values():
    with pytest.raises(ValueError, match="At least one station"):
        plot_mach_reference(())
    with pytest.raises(ValueError, match="finite and non-negative"):
        plot_mach_reference((MachStation("Exit", float("nan")),))
