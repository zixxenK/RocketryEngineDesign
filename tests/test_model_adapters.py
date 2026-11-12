import json
import os

import pytest

from rocket_analysis.cfd import openfoam_finalist_ids, prepare_openfoam_finalist_case
from rocket_analysis.openmdao_adapter import build_callable_problem, evaluate_callable
from rocket_analysis.thermochemistry import (
    run_cantera_equilibrium_screen,
    run_cea_ideal_screen,
)


def test_cea_ideal_screen_matches_reference_cstar_and_exit_mach():
    result = run_cea_ideal_screen(
        mixture_ratio=1.5,
        chamber_pressure_mpa=2.0,
        exit_area_ratio=19.74,
    )

    assert result.status == "ideal_equilibrium_screen"
    assert result.characteristic_velocity_m_s == pytest.approx(1642.5709, abs=0.02)
    assert result.exit_mach == pytest.approx(4.1322, abs=0.002)
    assert result.exit_area_ratio_calculated == pytest.approx(19.74, abs=0.002)
    assert result.ideal_vacuum_specific_impulse_m_s > result.ideal_exit_specific_impulse_m_s


def test_cantera_requires_explicit_mechanism_and_returns_equilibrium_state_only():
    result = run_cantera_equilibrium_screen(
        mechanism="gri30.yaml",
        fuel_species_mass_fractions={"CH4": 1.0},
        oxidizer_species_mass_fractions={"O2": 0.232, "N2": 0.768},
        mixture_ratio=3.0,
        chamber_pressure_pa=2.0e6,
        fuel_temperature_k=298.15,
        oxidizer_temperature_k=298.15,
    )

    assert result.status == "equilibrium_state_only_no_nozzle_performance"
    assert result.mechanism == "gri30.yaml"
    assert result.equilibrium_temperature_k > 1000
    assert result.gamma_cp_cv > 1
    assert "CO2" in result.equilibrium_mass_fractions

    with pytest.raises(ValueError, match="not found"):
        run_cantera_equilibrium_screen(
            mechanism="gri30.yaml",
            fuel_species_mass_fractions={"not-a-species": 1.0},
            oxidizer_species_mass_fractions={"O2": 1.0},
            mixture_ratio=3,
            chamber_pressure_pa=2e6,
            fuel_temperature_k=298,
            oxidizer_temperature_k=298,
        )


def test_openmdao_component_wraps_callable_and_reports_objective():
    model = lambda values: {"thrust": values["pc"] * values["area"]}
    assert evaluate_callable(model, {"pc": 2.0, "area": 3.0}) == {"thrust": 6.0}

    problem = build_callable_problem(
        model,
        input_defaults={"pc": 2.0, "area": 3.0},
        output_defaults={"thrust": 0.0},
    )
    problem.run_model()
    assert float(problem.get_val("thrust").item()) == pytest.approx(6.0)
    totals = problem.compute_totals(of=["thrust"], wrt=["pc", "area"])
    assert float(totals["thrust", "pc"].item()) == pytest.approx(3.0, rel=1e-4)
    assert float(totals["thrust", "area"].item()) == pytest.approx(2.0, rel=1e-4)


@pytest.mark.skipif(os.name == "nt", reason="Windows must stage finalist cases directly in WSL via a UNC path")
def test_openfoam_helpers_gate_finalists_and_substitute_template_tokens(tmp_path):
    class Entry:
        def __init__(self, candidate_id, rank):
            self.candidate_id = candidate_id
            self.rank = rank

    class Ranking:
        entries = [Entry("a", 2), Entry("b", 1), Entry("c", 1)]

    finalists = openfoam_finalist_ids(Ranking(), limit=2)
    assert finalists == ("b", "c")
    template = tmp_path / "template"
    template.mkdir()
    (template / "case.dict").write_text("pressure {{pc}}\n", encoding="utf-8")
    prepared = prepare_openfoam_finalist_case(
        template,
        tmp_path / "finalist-b",
        candidate_id="b",
        finalist_ids=finalists,
        replacements={"pc": 2.5},
    )
    assert "pressure 2.5" in (prepared / "case.dict").read_text(encoding="utf-8")
    manifest = json.loads((prepared / "rocket-analysis-finalist.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "prepared_for_user_review"

    with pytest.raises(ValueError, match="not in the selected CFD finalist set"):
        prepare_openfoam_finalist_case(
            template,
            tmp_path / "not-a-finalist",
            candidate_id="a",
            finalist_ids=finalists,
            replacements={"pc": 2.5},
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific OpenFOAM destination safety")
def test_openfoam_finalist_case_rejects_windows_local_storage(tmp_path):
    template = tmp_path / "template"
    template.mkdir()
    (template / "case.dict").write_text("pressure {{pc}}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be stored under a WSL UNC path"):
        prepare_openfoam_finalist_case(
            template,
            tmp_path / "finalist-b",
            candidate_id="b",
            finalist_ids=("b",),
            replacements={"pc": 2.5},
        )

