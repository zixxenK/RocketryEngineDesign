import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from rocket_analysis.optimization import OBJECTIVE, optimize_rpa4_cases
from rocket_analysis.rpa4_validation import load_rpa4_inputs
from rocket_analysis.shared import SharedRPA4Case, load_shared_rpa4_cases, scan_shared_rpa4_cases


ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "data/reference/Test6in.cfg"
PDF = ROOT / "data/reference/test1-6in-of-1.5.pdf"


def _case(inputs, case_name: str, of_ratio: float, isp_s: float, status: str = "match"):
    candidate_inputs = replace(inputs, case_name=case_name, mixture_ratio=of_ratio)
    return SharedRPA4Case(
        case_name=case_name,
        config_path=Path(f"{case_name}.cfg"),
        report_path=Path(f"{case_name}.pdf"),
        inputs=candidate_inputs,
        report_values={"estimated_delivered": {"specific_impulse_s": [120.0, 260.0, isp_s]}},
        validation={"status": status, "checks": []},
    )


def test_shared_folder_pairs_transferred_files_by_rpa4_case_name(tmp_path):
    shutil.copyfile(CFG, tmp_path / "engine-input.cfg")
    shutil.copyfile(PDF, tmp_path / "exported-results.pdf")

    cases = load_shared_rpa4_cases(tmp_path)

    assert len(cases) == 1
    case = cases[0]
    assert case.case_name == "Test 1"
    assert case.config_path.name == "engine-input.cfg"
    assert case.report_path.name == "exported-results.pdf"
    assert case.inputs.mixture_ratio == 1.5
    assert case.report_values["station_conditions"]["mach"] == [0.0, 0.0988, 1.0, 4.1322]
    assert case.validation["status"] == "review_required"


def test_shared_folder_reads_environment_path_and_rejects_missing_pair(tmp_path, monkeypatch):
    shutil.copyfile(CFG, tmp_path / "engine-input.cfg")
    monkeypatch.setenv("RPA4_SHARED_DIR", str(tmp_path))

    with pytest.raises(ValueError, match="No RPA4 .pdf reports found"):
        load_shared_rpa4_cases()


def test_shared_folder_collapses_identical_duplicate_case_names(tmp_path):
    shutil.copyfile(CFG, tmp_path / "one.cfg")
    shutil.copyfile(CFG, tmp_path / "two.cfg")
    shutil.copyfile(PDF, tmp_path / "report.pdf")

    scan = scan_shared_rpa4_cases(tmp_path)
    assert len(scan.cases) == 1
    assert len(scan.duplicates) == 1
    assert not scan.problems


def test_shared_folder_requires_cfg_and_pdf_to_share_a_case_directory(tmp_path):
    config_dir = tmp_path / "config"
    report_dir = tmp_path / "report"
    config_dir.mkdir()
    report_dir.mkdir()
    shutil.copyfile(CFG, config_dir / "engine.cfg")
    shutil.copyfile(PDF, report_dir / "report.pdf")

    with pytest.raises(ValueError, match="must be in the same folder"):
        load_shared_rpa4_cases(tmp_path)


def test_optimizer_maximizes_delivered_vacuum_isp_under_fixed_conditions():
    baseline = load_rpa4_inputs(CFG)
    candidates = [
        _case(baseline, "O-F-1.4", 1.4, 280.0),
        _case(baseline, "O-F-1.5", 1.5, 283.0),
        _case(baseline, "O-F-1.6", 1.6, 281.0),
        _case(baseline, "bad-report", 1.7, 300.0, status="mismatch"),
        _case(
            replace(baseline, chamber_pressure=baseline.chamber_pressure * 1.1),
            "different-pressure",
            1.8,
            350.0,
        ),
        _case(
            replace(baseline, ambient_pressure_point_count=31),
            "different-sweep",
            1.9,
            360.0,
        ),
    ]

    result = optimize_rpa4_cases(candidates, baseline_inputs=baseline)

    assert result.objective == OBJECTIVE
    assert result.objective_unit == "s"
    assert result.winner.case_name == "O-F-1.5"
    assert result.winner.objective_value == 283.0
    assert [candidate.case_name for candidate in result.ranking] == [
        "O-F-1.5", "O-F-1.6", "O-F-1.4"
    ]
    assert {item.case_name for item in result.exclusions} == {
        "bad-report", "different-pressure", "different-sweep"
    }


def test_optimizer_requires_two_validated_candidates():
    baseline = load_rpa4_inputs(CFG)
    candidates = [
        _case(baseline, "valid", 1.5, 280.0),
        _case(baseline, "mismatch", 1.6, 300.0, status="mismatch"),
    ]

    with pytest.raises(ValueError, match="At least two eligible RPA4 cases"):
        optimize_rpa4_cases(candidates, baseline_inputs=baseline)
