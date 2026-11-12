import json
from pathlib import Path

import pytest

from rocket_analysis.rpa4_validation import (
    load_rpa4_config,
    load_rpa4_inputs,
    validate_rpa4_case,
)


ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "data/reference/Test6in.cfg"
PDF = ROOT / "data/reference/test1-6in-of-1.5.pdf"


def test_supplied_rpa4_configuration_is_json_and_has_expected_version():
    config = load_rpa4_config(CFG)

    assert config["application"] == "RPA"
    assert config["version"] == 4.06
    assert config["engineDefinition"]["bipropellantMixture"]["mixtureRatio"]["value"] == 1.5


def test_rpa4_config_loader_rejects_wrong_application_and_invalid_blends(tmp_path):
    wrong_application = tmp_path / "other.cfg"
    wrong_application.write_text(json.dumps({"application": "other"}))
    with pytest.raises(ValueError, match="application must be 'RPA'"):
        load_rpa4_config(wrong_application)

    invalid_blend = json.loads(CFG.read_text())
    invalid_blend["engineDefinition"]["bipropellantMixture"]["fuel"][0]["massFraction"] = 0.4
    invalid_path = tmp_path / "invalid-blend.cfg"
    invalid_path.write_text(json.dumps(invalid_blend))
    with pytest.raises(ValueError, match="Fuel component mass fractions must sum to 1"):
        load_rpa4_config(invalid_path)


def test_transferred_rpa4_values_load_as_unit_aware_inputs():
    inputs = load_rpa4_inputs(CFG)

    assert inputs.source_file == "Test6in.cfg"
    assert inputs.version == 4.06
    assert inputs.case_name == "Test 1"
    assert inputs.propellants == {"fuel": (("RP-1", 1.0),), "oxidizer": (("O2(L)", 1.0),)}
    assert inputs.mixture_ratio == 1.5
    assert inputs.chamber_pressure.to("MPa").magnitude == 2.0
    assert inputs.throat_diameter.to("inch").magnitude == 6.0
    assert inputs.nozzle_inlet_area_ratio == 14.43
    assert inputs.nozzle_exit_area_ratio == 19.74
    assert inputs.chamber_length_value == 1020.0
    assert inputs.chamber_length_unit is None


def test_configuration_matches_report_inputs_with_inlet_area_ratio_review_warning():
    validation = validate_rpa4_case(CFG, PDF)
    checks = {check["field"]: check for check in validation["checks"]}

    assert validation["status"] == "review_required"
    assert checks["fuel identity"]["status"] == "match"
    assert checks["oxidizer identity"]["status"] == "match"
    assert checks["mixture ratio O/F"]["status"] == "match"
    assert checks["chamber pressure (MPa)"]["status"] == "match"
    assert checks["throat diameter (mm)"]["status"] == "match"
    assert checks["nozzle exit area ratio"]["status"] == "match"
    assert checks["nozzle inlet area ratio"]["configured"] == 14.43
    assert checks["nozzle inlet area ratio"]["reported"] == 6.0
    assert checks["nozzle inlet area ratio"]["status"] == "review"
    assert "do not establish" in checks["nozzle inlet area ratio"]["note"]
    assert validation["counts"]["mismatch"] == 0
    assert validation["counts"]["review"] == 4
    assert checks["chamber length"]["status"] == "review"
    assert checks["ambient pressure sample count"]["status"] == "review"
    assert checks["equilibrium shifting mode"]["status"] == "review"
    assert checks[
        "nozzle exit pressure matches optimum-expansion ambient pressure (atm)"
    ]["status"] == "match"


def test_efficiency_flags_are_not_marked_matching_when_disabled(tmp_path):
    config = json.loads(CFG.read_text())
    config["nozzleConditions"]["reactionEficiency"]["estimateReactionEfficiency"] = False
    changed = tmp_path / "efficiency-disabled.cfg"
    changed.write_text(json.dumps(config))

    validation = validate_rpa4_case(changed, PDF)
    checks = {check["field"]: check for check in validation["checks"]}

    assert checks["reaction efficiency estimate enabled"]["status"] == "review"


def test_validation_includes_exact_report_mach_stations():
    validation = validate_rpa4_case(CFG, PDF)

    assert validation["reported_mach_stations"] == [
        {"name": "Injector", "mach": 0.0},
        {"name": "Nozzle inlet", "mach": 0.0988},
        {"name": "Nozzle throat", "mach": 1.0},
        {"name": "Nozzle exit", "mach": 4.1322},
    ]


def test_saved_validation_snapshot_matches_current_config_and_report():
    expected = validate_rpa4_case(CFG, PDF)
    snapshot = json.loads((ROOT / "data/reference/test1-validation.json").read_text())

    assert snapshot == expected
