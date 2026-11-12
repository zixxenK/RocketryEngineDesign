import pytest

from rocket_analysis.ranking import candidate_metric_catalog, rank_metric_rows, report_metric_catalog
from rocket_analysis.rpa4 import parse_report_values


def test_report_catalog_exposes_all_report_sources_with_unit_bearing_keys():
    from pathlib import Path

    pdf = Path(__file__).resolve().parents[1] / "data/reference/test1-6in-of-1.5.pdf"
    values = parse_report_values(pdf)
    metrics = report_metric_catalog(values)

    assert metrics["estimated_delivered.sea_level.thrust_kN"] == 26.7328
    assert metrics["estimated_delivered.vacuum.specific_impulse_s"] == 282.664
    assert metrics["propellants.fuel.enthalpy_kj_kg"] == -1768.557
    assert metrics["station_conditions.mach.nozzle_exit"] == 4.1322
    assert metrics["geometry.exit_to_throat_area_ratio"] == 19.74
    assert "altitude_sweep.row_000.column_5" in metrics

    combined = candidate_metric_catalog(
        values,
        {"cea": {"exit_mach": 4.13}},
        design_parameters={"nozzle_inlet_area_ratio": 10.0, "mode": "demo"},
    )
    assert combined["screening.cea.exit_mach"] == 4.13
    assert combined["design.nozzle_inlet_area_ratio"] == 10.0


def test_pareto_ranking_retains_tradeoffs_and_excludes_only_hard_failures():
    rows = [
        {"candidate_id": "high-thrust", "case_name": "A", "validation_status": "review_required",
         "metrics": {"thrust": 10.0, "isp": 250.0}},
        {"candidate_id": "high-isp", "case_name": "B", "validation_status": "match",
         "metrics": {"thrust": 9.0, "isp": 270.0}},
        {"candidate_id": "dominated", "case_name": "C", "validation_status": "match",
         "metrics": {"thrust": 8.0, "isp": 240.0}},
        {"candidate_id": "mismatch", "case_name": "D", "validation_status": "mismatch",
         "metrics": {"thrust": 100.0, "isp": 400.0}},
    ]
    result = rank_metric_rows(
        rows,
        {
            "mode": "pareto",
            "objectives": [
                {"metric": "thrust", "direction": "maximize"},
                {"metric": "isp", "direction": "maximize"},
            ],
            "constraints": [{"metric": "thrust", "operator": ">=", "value": 8.5}],
        },
    )

    assert {entry.candidate_id for entry in result.top_ranked} == {"high-thrust", "high-isp"}
    assert {entry.candidate_id for entry in result.entries} == {"high-thrust", "high-isp"}
    assert {entry.candidate_id for entry in result.exclusions} == {"dominated", "mismatch"}


def test_weighted_ranking_uses_current_candidate_min_max_and_retains_raw_values():
    rows = [
        {"candidate_id": "a", "case_name": "A", "validation_status": "match",
         "metrics": {"thrust": 10.0, "isp": 200.0}},
        {"candidate_id": "b", "case_name": "B", "validation_status": "review_required",
         "metrics": {"thrust": 9.0, "isp": 300.0}},
        {"candidate_id": "c", "case_name": "C", "validation_status": "match",
         "metrics": {"thrust": 8.0, "isp": 250.0}},
    ]
    result = rank_metric_rows(
        rows,
        {
            "mode": "weighted",
            "objectives": [
                {"metric": "thrust", "direction": "maximize"},
                {"metric": "isp", "direction": "maximize"},
            ],
            "weights": {"thrust": 0.25, "isp": 0.75},
        },
    )

    assert result.entries[0].candidate_id == "b"
    assert result.entries[0].weighted_score == pytest.approx(0.875)
    assert result.entries[0].metrics == {"thrust": 9.0, "isp": 300.0}
    assert "eligible candidates" in result.normalization

