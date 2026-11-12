import hashlib
import json

from rocket_analysis.reports import ReportLibrary, rank_report_rows, report_comparison, validate_metric_profile


def _row(report_id, imported_at, *, isp, chamber, status="match", parameters=None):
    return {
        "report_id": report_id,
        "study_id": "study-a",
        "study_path": "C:/study-a.json",
        "candidate_id": "candidate-1",
        "case_name": report_id,
        "imported_at": imported_at,
        "report_sha256": report_id,
        "validation_status": status,
        "report_path": None,
        "metrics": {"delivered.isp_s": isp, "chamber.pressure_mpa": chamber},
        "report_values": {"chamber": {"pressure": chamber}},
        "parameters": parameters or {"mixture_ratio": 2.0},
        "parameter_units": {"mixture_ratio": None},
        "validation": {},
        "provenance": {},
    }


def test_report_ranking_modes_apply_targets_weights_and_validation():
    profile = {
        "metrics": [
            {"metric": "delivered.isp_s", "goal": "maximize", "weight": 1, "priority": 1},
            {"metric": "chamber.pressure_mpa", "goal": "target", "target": 100, "weight": 10, "priority": 2},
        ]
    }
    rows = [
        _row("r1", "2026-01-03T00:00:00+00:00", isp=300, chamber=110),
        _row("r2", "2026-01-02T00:00:00+00:00", isp=250, chamber=100),
        _row("r3", "2026-01-01T00:00:00+00:00", isp=275, chamber=102),
        _row("bad", "2026-01-04T00:00:00+00:00", isp=400, chamber=100, status="mismatch"),
    ]

    weighted = rank_report_rows(rows, profile, "weighted")
    priority = rank_report_rows(rows, profile, "priority")
    target_only = rank_report_rows(rows, profile, "per_metric", "chamber.pressure_mpa")

    assert weighted["entries"][0]["report_id"] == "r2"
    assert priority["entries"][0]["report_id"] == "r1"
    assert target_only["entries"][0]["report_id"] == "r2"
    assert any(item["report_id"] == "bad" for item in weighted["exclusions"])


def test_report_library_keeps_candidate_history_and_scopes_all_studies(tmp_path):
    archive = ReportLibrary(tmp_path / "archive")
    source_pdf = tmp_path / "source.pdf"
    source_pdf.write_bytes(b"first pdf")
    first_hash = hashlib.sha256(source_pdf.read_bytes()).hexdigest()
    first = _row("report-old", "2025-01-01T00:00:00+00:00", isp=250, chamber=90)
    first["report_sha256"] = first_hash
    first["report_path"] = str(source_pdf)
    newer_pdf = tmp_path / "newer.pdf"
    newer_pdf.write_bytes(b"second pdf")
    newer_hash = hashlib.sha256(newer_pdf.read_bytes()).hexdigest()
    newer = _row("report-new", "2026-01-01T00:00:00+00:00", isp=260, chamber=100)
    newer["report_sha256"] = newer_hash
    newer["report_path"] = str(newer_pdf)
    first["provenance"] = {"imported_at": first["imported_at"]}
    newer["provenance"] = {"imported_at": newer["imported_at"]}
    result_path = tmp_path / "study-a" / "rpa4-results.json"
    result_path.parent.mkdir()
    result_path.write_text(json.dumps({"report_history": [first, newer], "results": [newer]}), encoding="utf-8")

    archive.sync_study(tmp_path / "study-a.json", "study-a", result_path)
    current = archive.list_reports(study_path=tmp_path / "study-a.json")
    assert [row["report_id"] for row in current] == ["report-new", "report-old"]
    assert all(row["report_path"] and archive.get_report(row["report_id"]) for row in current)
    assert len(archive.list_reports()) == 2

    other_pdf = tmp_path / "other.pdf"
    other_pdf.write_bytes(b"third pdf")
    other_hash = hashlib.sha256(other_pdf.read_bytes()).hexdigest()
    other = _row("report-other", "2026-02-01T00:00:00+00:00", isp=270, chamber=95)
    other["report_sha256"] = other_hash
    other["report_path"] = str(other_pdf)
    other["provenance"] = {"imported_at": other["imported_at"]}
    other_path = tmp_path / "study-b" / "rpa4-results.json"
    other_path.parent.mkdir()
    other_path.write_text(json.dumps({"results": [other]}), encoding="utf-8")
    archive.sync_study(tmp_path / "study-b.json", "study-b", other_path)
    assert len(archive.list_reports()) == 3
    assert len(archive.list_reports(study_path=tmp_path / "study-b.json")) == 1


def test_report_comparison_returns_metric_and_observed_input_deltas():
    latest = _row("latest", "2026-01-02T00:00:00+00:00", isp=280, chamber=100, parameters={"mixture_ratio": 2.2})
    reference = _row("reference", "2026-01-01T00:00:00+00:00", isp=270, chamber=95, parameters={"mixture_ratio": 2.0})
    comparison = report_comparison(latest, [reference], {"metrics": [
        {"metric": "delivered.isp_s", "goal": "maximize", "weight": 1, "priority": 1},
    ]})[0]
    assert comparison["metric_deltas"][0]["delta"] == 10
    assert comparison["parameter_deltas"][0]["field"] == "mixture_ratio"
    assert comparison["report_deltas"]


def test_metric_profile_requires_finite_target_and_up_to_five_unique_metrics():
    profile = validate_metric_profile({"metrics": [
        {"metric": "x", "goal": "target", "target": 2.5, "weight": 1, "priority": 1},
    ]})
    assert profile["metrics"][0]["target"] == 2.5
