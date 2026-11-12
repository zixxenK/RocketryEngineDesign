import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rocket_analysis.service import StudyService


ROOT = Path(__file__).resolve().parents[1]
STUDY_SOURCE = ROOT / "data/studies/rp1-lox-design-study.example.json"
BASE_CFG = ROOT / "data/reference/Test6in.cfg"
REFERENCE_PDF = ROOT / "data/reference/test1-6in-of-1.5.pdf"


def _write_study(path: Path, output_dir: str = "runs") -> Path:
    payload = json.loads(STUDY_SOURCE.read_text(encoding="utf-8"))
    payload["base_cfg"] = str(BASE_CFG.resolve())
    payload["output_dir"] = output_dir
    payload["sampling"]["count"] = 2
    payload["sampling"]["max_candidates"] = 10
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def test_study_service_ui_read_models_cover_empty_and_populated_states(tmp_path):
    study_path = _write_study(tmp_path / "ui-study.json")
    service = StudyService(study_path)
    try:
        overview = service.study_overview()
        assert overview.proposed_count == 2
        assert overview.generated_count == 0
        assert overview.preview_error is None
        assert any(item.name == "estimated_delivered.sea_level.specific_impulse_s" for item in service.metric_catalog())

        preview = service.preview_return_source(REFERENCE_PDF)
        assert preview.pdf_files == (REFERENCE_PDF.name,)
        assert not preview.warnings

        candidates = service.generate()
        assert len(candidates) == 2
        detail = service.candidate_detail(candidates[0].candidate_id)
        assert detail.parameters == candidates[0].parameters
        assert detail.cfg["engineDefinition"]
        pack_preview = service.preview_pack({candidates[0].candidate_id})
        assert candidates[0].candidate_id in {row[0] for row in pack_preview.candidates}
        folder_pack = service.pack(tmp_path / "rpa4-folder-pack", {candidates[0].candidate_id})
        assert folder_pack.is_dir()
        assert (folder_pack / "RUN-SHEET.html").is_file()
        assert (folder_pack / "RUN-SHEET.md").is_file()
        assert (folder_pack / "pack-manifest.json").is_file()
        assert (folder_pack / candidates[0].case_name / "candidate.cfg").is_file()
    finally:
        service.close()


def _run_desktop(qtbot, monkeypatch, study_path=None):
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtCore, QtWidgets
    import rocket_analysis.desktop as desktop

    app = QtWidgets.QApplication.instance()
    real_core, real_gui, real_widgets = desktop._qt()

    class WidgetsProxy:
        def __getattr__(self, name):
            if name == "QApplication":
                return lambda _arguments: app
            return getattr(real_widgets, name)

    monkeypatch.setattr(desktop, "_qt", lambda: (real_core, real_gui, WidgetsProxy()))
    found = {}
    expected_path = Path(study_path).resolve() if study_path else None

    def capture_window():
        windows = [
            window
            for window in app.topLevelWidgets()
            if window.windowTitle() == "Rocket Analysis Study" and window.current_path == expected_path
        ]
        if windows:
            found["window"] = windows[-1]
        app.quit()

    QtCore.QTimer.singleShot(120, capture_window)
    arguments = [str(study_path)] if study_path else []
    assert desktop.main(arguments) == 0
    assert "window" in found
    return found["window"]


def test_desktop_startup_has_guided_empty_state(qtbot, monkeypatch):
    window = _run_desktop(qtbot, monkeypatch)
    try:
        assert window.pages["Study"].empty_title.text() == "Start a design study"
        assert len(window.nav_buttons) == 7
        assert window.pages["Inbox"].empty_title.text() == "No study inbox yet"
    finally:
        window.close()


def test_desktop_loaded_study_shows_design_summary_and_workflow(qtbot, monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets
    study_path = _write_study(tmp_path / "ui-study.json")
    window = _run_desktop(qtbot, monkeypatch, study_path)
    try:
        assert window.pages["Study"].data_model.rowCount() == 5
        assert window.overview_values["count"].text() == "2"
        assert window.screen_button.isEnabled() is False
        groups = [item.title() for item in window.pages["Study"].findChildren(QtWidgets.QGroupBox)]
        assert "Study at a glance" in groups
        assert "RPA4 report analysis" in groups
        assert "Four-step workflow" in groups
        assert window.report_scope.count() == 2
        assert window.report_mode.count() == 3
        assert window.report_ranked_table.accessibleName() == "Top 100 ranked RPA4 reports"
        assert window.report_recent_table.accessibleName() == "100 most recently imported RPA4 reports"
        assert window.feasibility_banner.text().startswith("Hard filters applied")
        assert "under calibration" in window.feasibility_banner.text()
    finally:
        window.close()
