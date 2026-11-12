import json
import shutil
from dataclasses import replace
from pathlib import Path

from rocket_analysis.rpa4_validation import load_rpa4_inputs
from rocket_analysis.shared import load_shared_rpa4_cases
from rocket_analysis.study import (
    ParameterSpec,
    generate_candidate_cfgs,
    import_shared_rpa4_results,
    load_study_spec,
    rank_screened_candidates,
    rank_study_results,
    sample_parameter_sets,
    screen_study_candidates,
    stage_rpa4_candidates,
    _optuna_study,
)


ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "data/reference/Test6in.cfg"
PDF = ROOT / "data/reference/test1-6in-of-1.5.pdf"
STUDY_JSON = ROOT / "data/studies/rp1-lox-design-study.example.json"


def test_study_spec_samples_stable_bounded_sobol_designs():
    spec = load_study_spec(STUDY_JSON)

    first, first_trials = sample_parameter_sets(spec)
    second, second_trials = sample_parameter_sets(spec)

    assert spec.sampling_method == "sobol"
    assert len(first) == spec.sample_count == 32
    assert first == second
    assert first_trials == second_trials == [None] * 32
    for candidate in first:
        assert 1.2 <= candidate["mixture_ratio"] <= 1.8
        assert 1.5 <= candidate["chamber_pressure"] <= 2.5
        assert 2.0 <= candidate["nozzle_inlet_area_ratio"] <= 15.0


def test_candidate_cfg_generation_is_stable_and_screening_is_persisted(tmp_path):
    source = load_study_spec(STUDY_JSON)
    spec = replace(
        source,
        output_dir=tmp_path,
        parameters=(ParameterSpec("mixture_ratio", "float", 1.4, 1.6, 2),),
        sampling_method="grid",
        sample_count=2,
        max_candidates=10,
        design_constraints=(),
    )

    candidates = generate_candidate_cfgs(spec)
    regenerated = generate_candidate_cfgs(spec)
    assert [item.candidate_id for item in candidates] == [item.candidate_id for item in regenerated]
    assert [load_rpa4_inputs(item.config_path).mixture_ratio for item in candidates] == [1.4, 1.6]

    screening = screen_study_candidates(spec, candidates)
    assert set(screening) == {item.candidate_id for item in candidates}
    assert all(item["cea"]["status"] == "completed" for item in screening.values())
    assert all(item["cantera"]["status"] == "not_configured" for item in screening.values())
    assert all((item.directory / "screening.json").is_file() for item in candidates)

    result = rank_screened_candidates(
        spec,
        {
            "mode": "pareto",
            "objectives": [
                {
                    "metric": "screening.cea.result.ideal_vacuum_specific_impulse_m_s",
                    "direction": "maximize",
                },
                {
                    "metric": "screening.cea.result.characteristic_velocity_m_s",
                    "direction": "maximize",
                },
            ],
        },
    )
    assert len(result.entries) == 2
    assert all(entry.metrics["screening.cea.result.exit_mach"] > 1 for entry in result.entries)


def test_staging_and_partial_shared_folder_loading(tmp_path):
    source = load_study_spec(STUDY_JSON)
    spec = replace(
        source,
        output_dir=tmp_path / "study",
        parameters=(ParameterSpec("mixture_ratio", "float", 1.4, 1.4, 1),),
        sampling_method="grid",
        sample_count=1,
        max_candidates=2,
        design_constraints=(),
    )
    candidates = generate_candidate_cfgs(spec)
    share = tmp_path / "share"
    staged = stage_rpa4_candidates(spec, share, candidates)
    assert len(staged) == 1
    assert (staged[0] / "candidate.cfg").is_file()

    # One completed case can be loaded while other staged CFGs await reports.
    complete = share / "baseline"
    complete.mkdir()
    shutil.copyfile(CFG, complete / "baseline.cfg")
    shutil.copyfile(PDF, complete / "baseline.pdf")
    cases = load_shared_rpa4_cases(share, allow_incomplete=True)
    assert [case.case_name for case in cases] == ["Test 1"]
    assert cases[0].validation["status"] == "review_required"


def test_shared_results_import_and_ranking_accept_area_ratio_review(tmp_path):
    source = load_study_spec(STUDY_JSON)
    spec = replace(source, output_dir=tmp_path / "study")
    candidate_dir = spec.output_dir / spec.study_id / "candidates" / "candidate-test"
    candidate_dir.mkdir(parents=True)
    shutil.copyfile(CFG, candidate_dir / "candidate.cfg")
    (candidate_dir / "manifest.json").write_text(
        json.dumps(
            {
                "study_id": spec.study_id,
                "candidate_id": "candidate-test",
                "case_name": "Test 1",
                "status": "awaiting_rpa4",
                "parameters": {"mixture_ratio": 1.5},
                "config_file": "candidate.cfg",
                "optuna_trial_number": None,
            }
        ),
        encoding="utf-8",
    )
    share = tmp_path / "share"
    share.mkdir()
    shutil.copyfile(CFG, share / "candidate.cfg")
    shutil.copyfile(PDF, share / "candidate.pdf")

    imported = import_shared_rpa4_results(spec, share)
    assert imported["last_imported_candidate_ids"] == ["candidate-test"]
    assert imported["results"][0]["validation_status"] == "review_required"
    assert imported["results"][0]["metrics"]["estimated_delivered.sea_level.thrust_kN"] == 26.7328
    assert imported["results"][0]["metrics"]["geometry.exit_to_throat_area_ratio"] == 19.74

    ranking = rank_study_results(spec)
    assert [entry.candidate_id for entry in ranking.entries] == ["candidate-test"]
    assert not ranking.exclusions

    second_share = tmp_path / "second-share"
    second_share.mkdir()
    shutil.copyfile(CFG, second_share / "candidate.cfg")
    (second_share / "candidate.pdf").write_bytes(PDF.read_bytes() + b"\n% second report export\n")
    second_import = import_shared_rpa4_results(spec, second_share)
    assert len(second_import["report_history"]) == 2
    assert len(second_import["results"]) == 1
    assert second_import["results"][0]["report_id"] != imported["results"][0]["report_id"]
    assert len(second_import["last_imported_report_ids"]) == 1

    repeated = import_shared_rpa4_results(spec, second_share)
    assert repeated["last_imported_report_ids"] == []
    assert len(repeated["report_history"]) == 2


def test_adaptive_trials_persist_and_complete_from_imported_report(tmp_path):
    source = load_study_spec(STUDY_JSON)
    spec = replace(
        source,
        output_dir=tmp_path / "study",
        parameters=(ParameterSpec("mixture_ratio", "float", 1.5, 1.5, 1),),
        sampling_method="adaptive",
        sample_count=1,
        max_candidates=2,
        design_constraints=(),
    )
    generated = generate_candidate_cfgs(spec)
    candidate = generated[0]
    manifest = json.loads(candidate.manifest_path.read_text(encoding="utf-8"))
    config = json.loads(candidate.config_path.read_text(encoding="utf-8"))
    config["engineDefinition"]["name"] = "Test 1"
    candidate.config_path.write_text(json.dumps(config), encoding="utf-8")
    manifest["case_name"] = "Test 1"
    candidate.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    share = tmp_path / "adaptive-share"
    share.mkdir()
    shutil.copyfile(candidate.config_path, share / "candidate.cfg")
    shutil.copyfile(PDF, share / "candidate.pdf")
    imported = import_shared_rpa4_results(spec, share)

    assert imported["last_imported_candidate_ids"] == [candidate.candidate_id]
    study = _optuna_study(spec)
    trial = next(item for item in study.get_trials(deepcopy=False) if item.number == candidate.optuna_trial_number)
    assert trial.state.name == "COMPLETE"
    expected_values = [imported["results"][0]["metrics"][item["metric"]] for item in spec.ranking["objectives"]]
    assert trial.values == expected_values

