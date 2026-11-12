"""PySide6 desktop shell for RP-1/LOX design studies."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from rocket_analysis.reports import default_report_archive_root
from rocket_analysis.service import StudyService, run_doctor


LIGHT = {
    "bg": "#F6F7F9", "surface": "#FFFFFF", "alt": "#EEF0F3", "border": "#D5D9DF",
    "text": "#1B1F24", "muted": "#5A6472", "accent": "#1F5FBF", "ok": "#1E7A46",
    "warn": "#8A5A00", "error": "#B3261E",
}
DARK = {
    "bg": "#15181C", "surface": "#1D2126", "alt": "#252A31", "border": "#353B44",
    "text": "#E6E9ED", "muted": "#9AA4B1", "accent": "#6AA0F0", "ok": "#5CC389",
    "warn": "#E0B04F", "error": "#F2877F",
}


def _qt():
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
    except ImportError as error:
        raise RuntimeError("The desktop app requires PySide6. Install with: pip install -e .[desktop]") from error
    return QtCore, QtGui, QtWidgets


def _style(tokens: dict[str, str]) -> str:
    return f"""
    QWidget {{ background: {tokens['bg']}; color: {tokens['text']}; font: 14px 'Segoe UI'; }}
    QFrame#panel, QGroupBox {{ background: {tokens['surface']}; border: 1px solid {tokens['border']}; border-radius: 6px; }}
    QGroupBox {{ margin-top: 12px; padding: 12px; font-weight: 600; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; }}
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {{ background: {tokens['surface']}; border: 1px solid {tokens['border']}; border-radius: 4px; min-height: 28px; padding: 3px 6px; }}
    QPushButton {{ background: {tokens['surface']}; border: 1px solid {tokens['border']}; border-radius: 4px; min-height: 30px; padding: 2px 10px; font-weight: 600; }}
    QPushButton:hover {{ border-color: {tokens['accent']}; }}
    QPushButton:disabled {{ color: {tokens['muted']}; background: {tokens['alt']}; border-color: {tokens['border']}; }}
    QPushButton:focus, QLineEdit:focus, QComboBox:focus {{ border: 2px solid {tokens['accent']}; }}
    QPushButton#navButton:checked {{ background: {tokens['alt']}; border-left: 3px solid {tokens['accent']}; }}
    QPushButton[primary="true"] {{ background: {tokens['accent']}; color: white; border: 0; }}
    QTableView {{ background: {tokens['surface']}; alternate-background-color: {tokens['alt']}; gridline-color: {tokens['border']}; border: 1px solid {tokens['border']}; selection-background-color: {tokens['accent']}; selection-color: white; }}
    QTableView::item:focus {{ border: 1px solid {tokens['accent']}; }}
    QHeaderView::section {{ background: {tokens['alt']}; border: 0; border-bottom: 1px solid {tokens['border']}; padding: 6px; font-weight: 600; }}
    QStackedWidget {{ background: {tokens['bg']}; }}
    QLabel#title {{ font-size: 24px; font-weight: 600; }}
    QLabel#muted {{ color: {tokens['muted']}; }}
    QToolButton {{ background: {tokens['surface']}; border: 1px solid {tokens['border']}; border-radius: 4px; min-height: 28px; padding: 2px 8px; }}
    QToolButton:hover {{ border-color: {tokens['accent']}; }}
    QScrollArea {{ border: 0; background: transparent; }}
    QStatusBar {{ background: {tokens['surface']}; border-top: 1px solid {tokens['border']}; }}
    """


def main(argv: list[str] | None = None) -> int:
    QtCore, QtGui, QtWidgets = _qt()
    args = list(sys.argv[1:] if argv is None else argv)
    study_path = Path(args[0]).resolve() if args else None
    app = QtWidgets.QApplication([sys.argv[0], *args])
    app.setApplicationName("Rocket Analysis Study")
    settings = QtCore.QSettings("RocketAnalysis", "StudyDesktop")

    class RowsModel(QtCore.QAbstractTableModel):
        """Virtual table model with cached text and Python-side filtering and sorting."""

        def __init__(self, headers=(), rows=(), parent=None):
            super().__init__(parent)
            self.headers = list(headers)
            self.rows = [list(row) for row in rows]
            self.header_tooltips = []
            self.query = ""
            self.attention_only = False
            self.sort_column = -1
            self.sort_order = QtCore.Qt.SortOrder.AscendingOrder
            self._search_texts = []
            self._attention_rows = []
            self._visible_rows = []
            self._status_icons = {}
            self._cache_rows()

        def rowCount(self, parent=QtCore.QModelIndex()):
            return 0 if parent.isValid() else len(self._visible_rows)

        def columnCount(self, parent=QtCore.QModelIndex()):
            return 0 if parent.isValid() else len(self.headers)

        def data(self, index, role=QtCore.Qt.ItemDataRole.DisplayRole):
            if not index.isValid():
                return None
            source_row = self._visible_rows[index.row()]
            value = self.rows[source_row][index.column()]
            if role in (QtCore.Qt.ItemDataRole.DisplayRole, QtCore.Qt.ItemDataRole.ToolTipRole):
                return "—" if value is None else str(value)
            if role == QtCore.Qt.ItemDataRole.DecorationRole and self._is_status_column(index.column()):
                return self._status_icon(value)
            if role == QtCore.Qt.ItemDataRole.FontRole and self._is_status_column(index.column()) and value not in (None, ""):
                font = QtGui.QFont()
                font.setBold(True)
                return font
            if role == QtCore.Qt.ItemDataRole.UserRole:
                return value
            if role == QtCore.Qt.ItemDataRole.TextAlignmentRole and isinstance(value, (float, int)):
                return int(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            if role == QtCore.Qt.ItemDataRole.FontRole and isinstance(value, (float, int)):
                return QtGui.QFont("Consolas", 10)
            return None

        def _is_status_column(self, column):
            if column >= len(self.headers):
                return False
            header = str(self.headers[column]).casefold()
            return header in {"status", "state", "validation", "rpa4 check", "cea status", "cantera status"} or header.startswith("validation")

        def _status_icon(self, value):
            text = str(value or "").casefold()
            if any(word in text for word in ("failed", "error", "mismatch")):
                icon_name = "SP_MessageBoxCritical"
            elif any(word in text for word in ("review", "unpaired", "stale", "warning")):
                icon_name = "SP_MessageBoxWarning"
            elif "duplicate" in text:
                icon_name = "SP_FileLinkIcon"
            elif any(word in text for word in ("validated", "parsed", "ok")):
                icon_name = "SP_DialogApplyButton"
            elif "sent" in text:
                icon_name = "SP_ArrowForward"
            elif "received" in text:
                icon_name = "SP_DialogOpenButton"
            elif "pending" in text or text:
                icon_name = "SP_MessageBoxInformation"
            else:
                return None
            if icon_name not in self._status_icons:
                pixmap = getattr(QtWidgets.QStyle.StandardPixmap, icon_name, QtWidgets.QStyle.StandardPixmap.SP_MessageBoxInformation)
                self._status_icons[icon_name] = app.style().standardIcon(pixmap)
            return self._status_icons[icon_name]

        def headerData(self, section, orientation, role=QtCore.Qt.ItemDataRole.DisplayRole):
            if orientation == QtCore.Qt.Orientation.Horizontal and 0 <= section < len(self.headers):
                header = self.headers[section]
                if role == QtCore.Qt.ItemDataRole.DisplayRole:
                    return header
                if role == QtCore.Qt.ItemDataRole.ToolTipRole:
                    if section < len(self.header_tooltips) and self.header_tooltips[section]:
                        return self.header_tooltips[section]
                    normalized = str(header).casefold()
                    if normalized.startswith("validation"):
                        return "Match = CFG and report agree. Review required = inspect the source files. Mismatch = a hard conflict; excluded from ranking."
                    if "weighted score" in normalized:
                        return "A 0–1 score for the active goals. Higher is better; the score is relative to the eligible candidates in this study. Raw metrics remain in their own columns."
                    if normalized == "rank":
                        return "Ordered by the selected ranking method. Pareto rank 1 is the non-dominated set; weighted ranking orders candidates by weighted score."
                    if str(header).startswith("RPA4 delivered"):
                        return "Performance parsed from the RPA4 report. The unit is included in parentheses."
                    if str(header).startswith("Fast screening estimate"):
                        return "An estimate from the configured screening model, not an RPA4 delivered result. The unit is included in parentheses."
                    return header
            return None

        def setQuery(self, query: str):
            query = query.casefold().strip()
            if query == self.query:
                return
            self.query = query
            self._reset_visible_rows()

        def setAttentionOnly(self, enabled: bool):
            enabled = bool(enabled)
            if enabled == self.attention_only:
                return
            self.attention_only = enabled
            self._reset_visible_rows()

        @property
        def total_row_count(self) -> int:
            return len(self.rows)

        def set_rows(self, headers, rows, header_tooltips=None):
            self.beginResetModel()
            self.headers = list(headers)
            self.rows = [list(row) for row in rows]
            self.header_tooltips = list(header_tooltips or [])
            if self.sort_column >= len(self.headers):
                self.sort_column = -1
            self._cache_rows()
            self.endResetModel()

        def _cache_rows(self):
            self._search_texts = [" ".join("" if value is None else str(value) for value in row).casefold() for row in self.rows]
            self._attention_rows = []
            for row in self.rows:
                status = str(row[2] or "").casefold() if len(row) > 2 else ""
                validation = str(row[3] or "").casefold() if len(row) > 3 else ""
                needs_attention = (
                    any(tag in status for tag in ("review", "failed", "unpaired", "stale", "parse failed"))
                    or validation in {"mismatch", "review_required", "review-required"}
                )
                self._attention_rows.append(needs_attention)
            self._rebuild_visible_rows()

        def _rebuild_visible_rows(self):
            query = self.query
            attention_only = self.attention_only
            visible = [
                row_index
                for row_index, text in enumerate(self._search_texts)
                if (not query or query in text) and (not attention_only or self._attention_rows[row_index])
            ]
            if 0 <= self.sort_column < len(self.headers):
                column = self.sort_column

                def key(row_index):
                    value = self.rows[row_index][column] if column < len(self.rows[row_index]) else None
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        return 0, float(value)
                    if value is None:
                        return 2, ""
                    return 1, str(value).casefold()

                visible.sort(key=key, reverse=self.sort_order == QtCore.Qt.SortOrder.DescendingOrder)
            self._visible_rows = visible

        def _reset_visible_rows(self):
            self.beginResetModel()
            self._rebuild_visible_rows()
            self.endResetModel()

        def sort(self, column, order=QtCore.Qt.SortOrder.AscendingOrder):
            self.beginResetModel()
            self.sort_column = int(column)
            self.sort_order = order
            self._rebuild_visible_rows()
            self.endResetModel()

    class BundleDropZone(QtWidgets.QFrame):
        dropped = QtCore.Signal(object)

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setAcceptDrops(True)
            self.setObjectName("panel")
            layout = QtWidgets.QVBoxLayout(self)
            self.label = QtWidgets.QLabel("Drop an RPA4 return ZIP, folder, or loose PDF/CFG files here")
            self.label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.label.setAccessibleName("RPA4 return drop zone")
            layout.addWidget(self.label)
            self.setMinimumHeight(72)

        def dragEnterEvent(self, event):
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
            else:
                event.ignore()

        def dropEvent(self, event):
            urls = event.mimeData().urls()
            if urls:
                paths = [url.toLocalFile() for url in urls if url.isLocalFile()]
                self.dropped.emit(paths[0] if len(paths) == 1 else paths)
                event.acceptProposedAction()
            else:
                event.ignore()

    class TablePage(QtWidgets.QWidget):
        def __init__(self, title: str, subtitle: str = ""):
            super().__init__()
            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(24, 20, 24, 20)
            header = QtWidgets.QHBoxLayout()
            self.title = QtWidgets.QLabel(title)
            self.title.setObjectName("title")
            header.addWidget(self.title)
            header.addStretch(1)
            self.actions = QtWidgets.QHBoxLayout()
            header.addLayout(self.actions)
            layout.addLayout(header)
            if subtitle:
                self.subtitle = QtWidgets.QLabel(subtitle)
                self.subtitle.setObjectName("muted")
                self.subtitle.setWordWrap(True)
                self.subtitle.setMinimumWidth(0)
                self.subtitle.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
                layout.addWidget(self.subtitle)
            self.content_scroll = QtWidgets.QScrollArea()
            self.content_scroll.setWidgetResizable(True)
            self.content_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.content_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            self.content_widget = QtWidgets.QWidget()
            self.content_widget.setMinimumWidth(0)
            self.content_widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
            self.body_layout = QtWidgets.QVBoxLayout(self.content_widget)
            self.body_layout.setContentsMargins(0, 4, 0, 4)
            self.body_layout.setSpacing(12)
            self.content_scroll.setWidget(self.content_widget)
            layout.addWidget(self.content_scroll, 1)
            self.notice = QtWidgets.QLabel("")
            self.notice.setObjectName("notice")
            self.notice.setWordWrap(True)
            self.notice.setMinimumWidth(0)
            self.notice.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
            self.body_layout.addWidget(self.notice)
            self.filter = QtWidgets.QLineEdit()
            self.filter.setPlaceholderText("Filter rows by any value…")
            self.filter.setClearButtonEnabled(True)
            self.filter.setAccessibleName(f"Filter {title} rows")
            self.table = QtWidgets.QTableView()
            self.table.setMinimumWidth(0)
            self.table.setAlternatingRowColors(True)
            self.table.setSortingEnabled(True)
            self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
            self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
            self.table.verticalHeader().setDefaultSectionSize(28)
            self.table.setAccessibleName(f"{title} data table")
            self.table.setAccessibleDescription(f"Sortable data for the {title} screen")
            self.table.setWordWrap(False)
            self.table.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
            self.data_model = RowsModel(parent=self)
            self.filter.textChanged.connect(self.data_model.setQuery)
            self.filter.textChanged.connect(self._sync_filter_state)
            self.table.setModel(self.data_model)
            self.data_stack = QtWidgets.QStackedWidget()
            self.data_stack.addWidget(self.table)
            self.empty_card = QtWidgets.QFrame()
            self.empty_card.setObjectName("panel")
            empty_layout = QtWidgets.QVBoxLayout(self.empty_card)
            empty_layout.setContentsMargins(28, 24, 28, 24)
            empty_layout.addStretch(1)
            self.empty_title = QtWidgets.QLabel(f"{title} is ready")
            self.empty_title.setObjectName("sectionTitle")
            self.empty_title.setAlignment(QtCore.Qt.AlignmentFlag.AlignHCenter)
            empty_layout.addWidget(self.empty_title)
            self.empty_body = QtWidgets.QLabel("Open a study to see its data here.")
            self.empty_body.setObjectName("muted")
            self.empty_body.setWordWrap(True)
            self.empty_body.setMinimumWidth(0)
            self.empty_body.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
            self.empty_body.setAlignment(QtCore.Qt.AlignmentFlag.AlignHCenter)
            empty_layout.addWidget(self.empty_body)
            self.empty_action = QtWidgets.QPushButton("Open study…")
            self.empty_action.setAccessibleName("Open study")
            empty_layout.addWidget(self.empty_action, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
            empty_layout.addStretch(1)
            self.data_stack.addWidget(self.empty_card)
            self.data_stack.setCurrentWidget(self.empty_card)
            self.plot = None
            if title == "Results":
                try:
                    import pyqtgraph as pg

                    self.plot = pg.PlotWidget()
                    self.plot.setMinimumHeight(190)
                    self.plot.setAccessibleName("Pareto scatter plot")
                    self.plot.setAccessibleDescription("Scatter plot of the first two selected ranking metrics; the results table below provides the accessible equivalent")
                    self.plot.hide()
                except ImportError:
                    self.plot = None
            self.body_layout.addWidget(self.filter)
            self.body_layout.addWidget(self.data_stack, 1)
            if self.plot:
                self.body_layout.addWidget(self.plot)
            self.filter.hide()
            self.column_menu = QtWidgets.QMenu(self)
            self.column_button = QtWidgets.QToolButton()
            self.column_button.setText("Columns")
            self.column_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
            self.column_button.setMenu(self.column_menu)
            self.column_button.setAccessibleName(f"Choose visible {title} table columns")
            self.column_button.setToolTip("Choose which table columns are visible")
            self.column_button.setEnabled(False)
            self.actions.addWidget(self.column_button)
            self.column_actions = []

        def show_rows(self, headers: list[str], rows: list[list[Any]], header_tooltips=None) -> None:
            self.filter.blockSignals(True)
            self.filter.setText("")
            self.filter.blockSignals(False)
            self.data_model.setQuery("")
            self.data_model.set_rows(headers, rows, header_tooltips)
            self._build_column_menu(headers)
            if rows:
                self.filter.show()
                self._size_columns(headers, rows)
                self._sync_filter_state()
            else:
                self.data_stack.setCurrentWidget(self.empty_card)
                self.filter.hide()

        def _build_column_menu(self, headers: list[str]) -> None:
            self.column_menu.clear()
            self.column_actions = []
            settings_key = f"hidden_columns/{self.title.text()}"
            hidden = {str(name).replace("\n", " ") for name in settings.value(settings_key, [], list)}
            if self.title.text() == "Results" and "Candidate ID" in headers and not settings.contains(settings_key):
                hidden.add("Candidate ID")
            for column, header in enumerate(headers):
                display_name = str(header).replace("\n", " ")
                action = self.column_menu.addAction(display_name)
                action.setCheckable(True)
                action.setChecked(display_name not in hidden)
                action.setToolTip(f"Show or hide the {display_name} column")
                action.toggled.connect(lambda visible, index=column, name=header: self._set_column_visible(index, name, visible))
                self.column_actions.append(action)
                self.table.setColumnHidden(column, header in hidden)
            self.column_button.setEnabled(bool(headers))
            self.column_button.setToolTip("Choose which table columns are visible" if headers else "Table columns appear when data is available")

        def _set_column_visible(self, column: int, name: str, visible: bool) -> None:
            checked = [action for action in self.column_actions if action.isChecked()]
            if not checked:
                action = self.column_actions[column]
                action.blockSignals(True)
                action.setChecked(True)
                action.blockSignals(False)
                return
            self.table.setColumnHidden(column, not visible)
            hidden = [action.text() for action in self.column_actions if not action.isChecked()]
            settings.setValue(f"hidden_columns/{self.title.text()}", hidden)

        def _size_columns(self, headers: list[str], rows: list[list[Any]]) -> None:
            metrics = self.table.fontMetrics()
            header = self.table.horizontalHeader()
            header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Interactive)
            if self.title.text() == "Results":
                header.setDefaultAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
                header.setTextElideMode(QtCore.Qt.TextElideMode.ElideNone)
                metric_start = 5 if "Score (0–1)" in headers else 4
                metric_headers = headers[metric_start:]
                line_count = max((str(name).count("\n") + 1 for name in metric_headers), default=1)
                header.setFixedHeight(metrics.height() * line_count + 20)
                self.table.setHorizontalScrollBarPolicy(
                    QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOn if metric_headers
                    else QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded
                )
            metric_start = 5 if "Score (0–1)" in headers else 4
            # Measuring a bounded sample avoids a full 10,000-row content scan on every refresh.
            sample = rows[:80]
            for column, name in enumerate(headers):
                display_lines = str(name).splitlines() or [str(name)]
                width = max(metrics.horizontalAdvance(line) for line in display_lines) + 42
                for row in sample:
                    if column < len(row) and row[column] is not None:
                        width = max(width, metrics.horizontalAdvance(str(row[column])) + 24)
                if self.title.text() == "Results" and name == "RPA4 check":
                    width = max(width, 150)
                if self.title.text() == "Results" and column >= metric_start:
                    # Full names are wrapped into a few lines so several metric
                    # columns remain visible at once, with horizontal scrolling for
                    # the rest of the report.
                    width = min(300, max(230, width))
                limit = 360
                self.table.setColumnWidth(column, min(limit, max(76, width)))

        def _sync_filter_state(self, *_):
            if self.data_model.total_row_count and self.data_model.rowCount() == 0:
                if self.data_model.attention_only and not self.data_model.query:
                    self.empty_title.setText("No inbox items need attention")
                    self.empty_body.setText("All tracked cases are waiting, resolved, or already reviewed.")
                else:
                    self.empty_title.setText("No rows match this filter")
                    self.empty_body.setText("Clear the search or status filter to see the study data again.")
                self.empty_action.hide()
                self.data_stack.setCurrentWidget(self.empty_card)
            elif self.data_model.rowCount():
                self.empty_action.show()
                self.data_stack.setCurrentWidget(self.table)

        def set_empty(self, title: str, body: str, action_text: str | None = None, callback=None) -> None:
            self.empty_title.setText(title)
            self.empty_body.setText(body)
            self.empty_action.setVisible(bool(action_text and callback))
            if action_text and callback:
                try:
                    self.empty_action.clicked.disconnect()
                except RuntimeError:
                    pass
                self.empty_action.setText(action_text)
                self.empty_action.clicked.connect(callback)
            self.filter.blockSignals(True)
            self.filter.clear()
            self.filter.blockSignals(False)
            self.data_model.setQuery("")
            self.data_model.set_rows([], [])
            self._build_column_menu([])
            self.data_stack.setCurrentWidget(self.empty_card)
            self.filter.hide()
        def button(self, label: str, callback, *, primary: bool = False):
            button = QtWidgets.QPushButton(label)
            button.setAccessibleName(label)
            button.setAccessibleDescription(label)
            if primary:
                button.setProperty("primary", True)
            button.clicked.connect(callback)
            self.actions.addWidget(button)
            return button

    class MainWindow(QtWidgets.QMainWindow):
        def __init__(self, initial_path: Path | None):
            super().__init__()
            self.setWindowTitle("Rocket Analysis Study")
            screen = app.primaryScreen()
            available = screen.availableGeometry() if screen else QtCore.QRect(0, 0, 1360, 840)
            screen_width = max(320, available.width() - 16)
            screen_height = max(240, available.height() - 16)
            minimum_width = min(1100, screen_width)
            minimum_height = min(700, screen_height)
            self.setMinimumSize(minimum_width, minimum_height)
            self.resize(
                max(minimum_width, min(settings.value("width", 1360, int), screen_width)),
                max(minimum_height, min(settings.value("height", 840, int), screen_height)),
            )
            saved_x = max(0, settings.value("x", 0, int))
            saved_y = max(0, settings.value("y", 0, int))
            max_x = max(0, available.width() - self.width())
            max_y = max(0, available.height() - self.height())
            self.move(min(saved_x, max_x), min(saved_y, max_y))
            self.service: StudyService | None = None
            self.archive_root = Path(settings.value("report_archive_root", str(default_report_archive_root()))).expanduser().resolve()
            self.current_path: Path | None = None
            self._job_states: dict[str, str] = {}
            self._visible_jobs = []
            self.openfoam_available = False
            self.finalist_ids: list[str] = []
            self.prepared_case = False
            self.prepared_candidate_id: str | None = None
            self._theme = settings.value("theme", "light")
            self.table_density = settings.value("row_height", 28, int)
            self._make_layout()
            for page in self.pages.values():
                page.table.verticalHeader().setDefaultSectionSize(self.table_density)
            self.job_table.verticalHeader().setDefaultSectionSize(self.table_density)
            self.finalist_compare_table.verticalHeader().setDefaultSectionSize(self.table_density)
            if not initial_path:
                self.pages["Study"].set_empty("Start a design study", "Open an existing study JSON or create a one-point study from an RPA4 CFG. Then edit ranges and review feasibility before generating cases.", "Open study", self.open_dialog)
                self.pages["Screen"].set_empty("No candidates to screen", "Open a study and generate candidate CFG files first.", "Go to Study", lambda: self.stack.setCurrentWidget(self.pages["Study"]))
                self.pages["Export"].set_empty("No cases to export", "Generate candidates before building an RPA4 handoff pack.", "Go to Study", lambda: self.stack.setCurrentWidget(self.pages["Study"]))
                self.pages["Inbox"].set_empty("No study inbox yet", "Open or create a study to track the cases sent to RPA4.", "Open study", self.open_dialog)
                self.pages["Results"].set_empty("No results to rank", "Import RPA4 reports in Inbox or screen candidates to create ideal-model comparison data.", "Go to Inbox", lambda: self.stack.setCurrentWidget(self.pages["Inbox"]))
                self.pages["Candidate"].set_empty("Select a candidate", "Candidate detail opens from a row in Inbox or Results.")
                self.pages["Finalists"].set_empty("No ranked finalists", "Rank candidates in Results, then refresh the finalist list.", "Go to Results", lambda: self.stack.setCurrentWidget(self.pages["Results"]))
                self.pages["Doctor"].set_empty("Check the analysis setup", "Run the checks to see which analysis packages, shared folders, WSL, and OpenFOAM are available.", "Run checks", self.doctor)
            self._make_shortcuts()
            self._refresh_timer = QtCore.QTimer(self)
            self._refresh_timer.timeout.connect(self._refresh_jobs)
            self._refresh_timer.start(500)
            if initial_path:
                self.load_study(initial_path)
            else:
                self.statusBar().showMessage("Open a study JSON or create a study from an RPA4 CFG")

        def _make_layout(self):
            root = QtWidgets.QWidget()
            outer = QtWidgets.QVBoxLayout(root)
            outer.setContentsMargins(0, 0, 0, 0)
            body = QtWidgets.QHBoxLayout()
            body.setSpacing(0)
            rail = QtWidgets.QFrame()
            rail.setFixedWidth(200)
            self.rail = rail
            self.nav_buttons = []
            rail.setStyleSheet("border-right: 1px solid palette(mid);")
            rail_layout = QtWidgets.QVBoxLayout(rail)
            brand = QtWidgets.QLabel("ROCKET\nSTUDY")
            self.brand_label = brand
            brand.setStyleSheet("font-size: 16px; font-weight: 700; padding: 16px 10px;")
            rail_layout.addWidget(brand)
            self.stack = QtWidgets.QStackedWidget()
            self.nav_group = QtWidgets.QButtonGroup(self)
            self.nav_group.setExclusive(True)
            self.pages: dict[str, TablePage] = {}
            page_names = ["Study", "Screen", "Export", "Inbox", "Results", "Candidate", "Finalists", "Doctor"]
            page_subtitles = {
                "Study": "Set ranges for engine inputs, check how many cases they create, and generate RPA4 configuration (CFG) files. The returned reports are compared in Results.",
                "Screen": "Run quick chemistry estimates with CEA (chemical equilibrium analysis) and any configured Cantera model. Use these estimates to narrow the cases; use returned RPA4 reports for engine performance results.",
                "Export": "Create a ZIP or folder with the RPA4 configuration files and a run sheet. Copy it to the computer that runs RPA4, then bring the returned reports to Inbox.",
                "Inbox": "Bring back the RPA4 PDF reports and their matching configuration files from a ZIP, folder, or shared location. Check the file matches before ranking the case.",
                "Results": "Each row is a returned RPA4 case. Choose report metrics to rank, then select two to five rows to compare them side by side. Double-click a row for its report and file checks.",
                "Candidate": "Select a case in Inbox or Results to see its input settings, values read from the RPA4 report, file-match checks, and source files.",
                "Finalists": "Prepare OpenFOAM computational fluid dynamics (CFD) simulations for top-ranked cases. Review the simulation setup before running; CFD results do not replace a hot-fire test.",
                "Doctor": "Check required software, shared-folder access, and file paths. Each result explains what needs attention; review a suggested command before running it.",
            }
            for index, name in enumerate(page_names):
                page = TablePage(name, page_subtitles[name])
                self.pages[name] = page
                self.stack.addWidget(page)
                if name != "Candidate":
                    button = QtWidgets.QPushButton(name)
                    button.setObjectName("navButton")
                    button.setFlat(True)
                    button.setCheckable(True)
                    button.setStyleSheet("text-align: left; padding-left: 12px;")
                    button.setAccessibleName(name)
                    button.setAccessibleDescription(f"Open the {name} screen")
                    button.clicked.connect(lambda _checked=False, target=page: self.stack.setCurrentWidget(target))
                    self.nav_group.addButton(button)
                    self.nav_buttons.append((button, name))
                    rail_layout.addWidget(button)
            self.stack.currentChanged.connect(self._sync_nav_selection)
            self._sync_nav_selection(self.stack.currentIndex())
            rail_layout.addStretch(1)
            self.study_label = QtWidgets.QLabel("No study open")
            self.study_label.setObjectName("muted")
            self.study_label.setWordWrap(True)
            rail_layout.addWidget(self.study_label)
            self.open_button = QtWidgets.QPushButton("Open study...")
            self.open_button.setAccessibleName("Open study file")
            self.open_button.clicked.connect(self.open_dialog)
            rail_layout.addWidget(self.open_button)
            for page in self.pages.values():
                page.empty_action.clicked.connect(self.open_dialog)
            self.new_button = QtWidgets.QPushButton("New from CFG...")
            self.new_button.setAccessibleName("Create a study from an RPA4 CFG")
            self.new_button.clicked.connect(self.new_study_dialog)
            rail_layout.addWidget(self.new_button)
            self.theme_button = QtWidgets.QPushButton("Toggle theme")
            self.theme_button.clicked.connect(self.toggle_theme)
            rail_layout.addWidget(self.theme_button)
            self.settings_button = QtWidgets.QPushButton("Settings...")
            self.settings_button.clicked.connect(self.open_settings)
            self.settings_button.setAccessibleName("Open application settings")
            rail_layout.addWidget(self.settings_button)
            body.addWidget(rail)
            body.addWidget(self.stack, 1)
            outer.addLayout(body, 1)
            self.job_strip = QtWidgets.QPushButton("Jobs: 0 active - click to expand")
            self.job_strip.setFlat(True)
            self.job_strip.clicked.connect(self.toggle_jobs)
            job_controls = QtWidgets.QHBoxLayout()
            job_controls.addWidget(self.job_strip, 1)
            self.cancel_job_button = QtWidgets.QPushButton("Cancel selected job")
            self.cancel_job_button.setAccessibleName("Cancel selected job")
            self.cancel_job_button.clicked.connect(self.cancel_selected_job)
            job_controls.addWidget(self.cancel_job_button)
            self.job_details_button = QtWidgets.QPushButton("Job details")
            self.job_details_button.setAccessibleName("Open selected job details")
            self.job_details_button.clicked.connect(self.open_selected_job_details)
            job_controls.addWidget(self.job_details_button)
            outer.addLayout(job_controls)
            self.job_table = QtWidgets.QTableView()
            self.job_table.setMaximumHeight(210)
            self.job_table.hide()
            outer.addWidget(self.job_table)
            self.setCentralWidget(root)
            self.statusBar().showMessage("Ready")
            self._configure_pages()
            self.pages["Inbox"].table.doubleClicked.connect(self._open_selected_candidate)
            self.pages["Screen"].table.doubleClicked.connect(lambda index: self._open_selected_candidate(index, "Screen"))
            self.pages["Results"].table.doubleClicked.connect(lambda index: self._open_selected_candidate(index, "Results"))
            self.pages["Candidate"].table.clicked.connect(self._show_candidate_detail)
            self.pages["Finalists"].table.clicked.connect(self._update_finalist_controls)
            self.candidate_detail_tabs = QtWidgets.QTabWidget()
            self.candidate_detail_views = {}
            candidate_tab_labels = {
                "Report": "RPA4 report values",
                "CFG vs PDF": "Validation",
                "Screening": "Screen estimates",
                "Provenance": "Files & history",
            }
            candidate_tab_help = {
                "Report": "Values parsed from the imported RPA4 PDF, including units and station data.",
                "CFG vs PDF": "Checks report inputs against the CFG used for this candidate. Review required means check the source files.",
                "Screening": "Fast model estimates. These do not replace delivered values from RPA4.",
                "Provenance": "Source file paths, hashes, import time, and processing history.",
            }
            for tab_name in ("Inputs", "Report", "CFG vs PDF", "Screening", "Provenance"):
                view = QtWidgets.QPlainTextEdit()
                view.setReadOnly(True)
                view.setFont(QtGui.QFont("Consolas", 10))
                view.setAccessibleName(f"Candidate {tab_name} details")
                self.candidate_detail_views[tab_name] = view
                tab_index = self.candidate_detail_tabs.addTab(view, candidate_tab_labels.get(tab_name, tab_name))
                self.candidate_detail_tabs.setTabToolTip(tab_index, candidate_tab_help.get(tab_name, "Candidate input values used to generate the RPA4 case."))
            self.candidate_detail_tabs.setMaximumHeight(250)
            self.pages["Candidate"].body_layout.addWidget(self.candidate_detail_tabs)

        def _make_shortcuts(self):
            shortcut = QtGui.QShortcut(QtGui.QKeySequence("F5"), self)
            shortcut.activated.connect(self.rescan)
            shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+E"), self)
            shortcut.activated.connect(self.pack)
            shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+I"), self)
            shortcut.activated.connect(self.import_dialog)
            shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+R"), self)
            shortcut.activated.connect(lambda: self.stack.setCurrentWidget(self.pages["Results"]))
            shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+,"), self)
            shortcut.activated.connect(self.open_settings)
            generate = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+G"), self)
            generate.activated.connect(self.generate)

        def _sync_nav_selection(self, page_index: int) -> None:
            page = self.stack.widget(page_index)
            for button, name in self.nav_buttons:
                button.setChecked(self.pages[name] is page)
            if page is self.pages.get("Results"):
                QtCore.QTimer.singleShot(0, lambda target=page: target.content_scroll.verticalScrollBar().setValue(0))

        def _set_overview_columns(self, columns: int) -> None:
            if not hasattr(self, "overview_tiles") or self._overview_columns == columns:
                return
            for index, tile in enumerate(self.overview_tiles):
                self.overview_layout.addWidget(tile, index // columns, index % columns)
            for column in range(len(self.overview_tiles)):
                self.overview_layout.setColumnStretch(column, 1 if column < columns else 0)
            self._overview_columns = columns

        def _configure_pages(self):
            self.study_preview_button = self.pages["Study"].button("Preview cases", self.preview)
            self.study_preview_button.setToolTip("Show the candidate combinations that the current sampling settings will create.")
            self.study_edit_ranges_button = self.pages["Study"].button("Edit input ranges…", self.edit_ranges)
            self.study_edit_ranges_button.setToolTip("Change parameter bounds, levels, categorical values, or fixed boolean states.")
            self.generate_button = self.pages["Study"].button("Generate candidates", self.generate, primary=True)
            self.generate_button.setEnabled(False)
            sampling_group = QtWidgets.QGroupBox("Sampling")
            sampling_layout = QtWidgets.QGridLayout(sampling_group)
            sampling_layout.addWidget(QtWidgets.QLabel("Method"), 0, 0)
            self.sampling_method = QtWidgets.QComboBox()
            self.sampling_method.addItems(["grid", "sobol", "adaptive", "explicit"])
            self.sampling_method.setAccessibleName("Sampling method")
            self.sampling_method.setToolTip("Grid covers every level combination. Sobol and adaptive sample a requested count. Explicit uses rows you enter.")
            sampling_layout.addWidget(self.sampling_method, 0, 1)
            count_label = QtWidgets.QLabel("Count (non-grid)")
            count_label.setToolTip("Grid size comes from the product of active parameter levels")
            sampling_layout.addWidget(count_label, 0, 2)
            self.sample_count = QtWidgets.QSpinBox()
            self.sample_count.setRange(1, 1000000)
            self.sample_count.setAccessibleName("Sampling count")
            self.sample_count.setToolTip("Number of candidates for Sobol or adaptive sampling. Grid count comes from the parameter levels.")
            sampling_layout.addWidget(self.sample_count, 0, 3)
            sampling_layout.addWidget(QtWidgets.QLabel("Seed"), 0, 4)
            self.sample_seed = QtWidgets.QSpinBox()
            self.sample_seed.setRange(-2147483647, 2147483647)
            self.sample_seed.setAccessibleName("Sampling seed")
            self.sample_seed.setToolTip("Seed used to reproduce Sobol and adaptive candidate samples.")
            sampling_layout.addWidget(self.sample_seed, 0, 5)
            sampling_layout.addWidget(QtWidgets.QLabel("Candidate limit"), 1, 0)
            self.sample_limit = QtWidgets.QSpinBox()
            self.sample_limit.setRange(1, 1000000)
            self.sample_limit.setAccessibleName("Maximum candidates")
            self.sample_limit.setToolTip("Safety limit for the number of generated candidates.")
            sampling_layout.addWidget(self.sample_limit, 1, 1)
            apply_sampling = QtWidgets.QPushButton("Apply")
            apply_sampling.clicked.connect(self.apply_sampling_options)
            sampling_layout.addWidget(apply_sampling, 1, 2, 1, 2)
            edit_explicit = QtWidgets.QPushButton("Edit explicit values…")
            edit_explicit.clicked.connect(self.edit_explicit_candidates)
            sampling_layout.addWidget(edit_explicit, 1, 4, 1, 2)
            study_page = self.pages["Study"]
            overview_group = QtWidgets.QGroupBox("Study at a glance")
            overview_layout = QtWidgets.QGridLayout(overview_group)
            self.overview_layout = overview_layout
            self.overview_tiles = []
            self._overview_columns = 0
            self.overview_values = {}
            for column, (key, caption) in enumerate((("count", "Proposed cases"), ("limit", "Candidate limit"), ("generated", "Generated"), ("imported", "RPA4 returns"))):
                tile = QtWidgets.QFrame()
                tile.setObjectName("panel")
                tile_layout = QtWidgets.QVBoxLayout(tile)
                tile_layout.setContentsMargins(8, 6, 8, 6)
                tile_layout.setSpacing(2)
                value = QtWidgets.QLabel("—")
                value.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                value.setStyleSheet("font-size: 20px; font-weight: 700;")
                title = QtWidgets.QLabel(caption)
                title.setObjectName("muted")
                title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                tile_layout.addWidget(value)
                tile_layout.addWidget(title)
                overview_layout.addWidget(tile, 0, column)
                self.overview_tiles.append(tile)
                self.overview_values[key] = value
            self._set_overview_columns(2 if self.width() < 900 else 4)
            study_page.body_layout.insertWidget(0, overview_group)
            report_group = QtWidgets.QGroupBox("RPA4 report analysis")
            report_group.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
            report_layout = QtWidgets.QVBoxLayout(report_group)
            self.report_summary = QtWidgets.QLabel("Import RPA4 results to build a searchable report history.")
            self.report_summary.setWordWrap(True)
            self.report_summary.setObjectName("muted")
            report_layout.addWidget(self.report_summary)
            report_controls = QtWidgets.QVBoxLayout()
            scope_row = QtWidgets.QHBoxLayout()
            scope_row.addWidget(QtWidgets.QLabel("Scope"))
            self.report_scope = QtWidgets.QComboBox()
            self.report_scope.addItem("This Study", "study")
            self.report_scope.addItem("All studies", "all")
            self.report_scope.setAccessibleName("Report comparison scope")
            self.report_scope.setMinimumContentsLength(12)
            self.report_scope.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            self.report_scope.setMaximumWidth(185)
            scope_row.addWidget(self.report_scope)
            scope_row.addSpacing(12)
            scope_row.addWidget(QtWidgets.QLabel("Rank by"))
            self.report_mode = QtWidgets.QComboBox()
            self.report_mode.addItem("Weighted goals", "weighted")
            self.report_mode.addItem("Priority order", "priority")
            self.report_mode.addItem("Separate metric", "per_metric")
            self.report_mode.setAccessibleName("Report ranking mode")
            self.report_mode.setMinimumContentsLength(15)
            self.report_mode.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            self.report_mode.setMaximumWidth(220)
            scope_row.addWidget(self.report_mode)
            scope_row.addSpacing(12)
            self.report_metric_label = QtWidgets.QLabel("Metric")
            self.report_metric = QtWidgets.QComboBox()
            self.report_metric.setAccessibleName("Metric for separate report ranking")
            self.report_metric.setMinimumWidth(190)
            self.report_metric.setMaximumWidth(300)
            self.report_metric.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            self.report_metric.setMinimumContentsLength(20)
            self.report_metric.setToolTip("Choose which single metric to rank when using Separate metric mode.")
            self.report_mode.setToolTip("Weighted goals balance all selected metrics; Priority order applies your saved order; Separate metric ranks by one goal.")
            self.report_scope.setToolTip("Limit rankings to this design study or include the full local report archive.")
            scope_row.addWidget(self.report_metric_label)
            scope_row.addWidget(self.report_metric)
            scope_row.addStretch(1)
            report_controls.addLayout(scope_row)
            report_action_row = QtWidgets.QHBoxLayout()
            self.report_profile_button = QtWidgets.QPushButton("Metrics…")
            self.report_profile_button.setToolTip("Choose up to five report metrics, targets, weights, and priorities")
            self.report_profile_button.clicked.connect(self.edit_report_profile)
            report_action_row.addWidget(self.report_profile_button)
            self.report_import_button = QtWidgets.QPushButton("Import returns")
            self.report_import_button.setToolTip("Open Inbox to import RPA4 reports from a folder, ZIP, or shared location")
            self.report_import_button.clicked.connect(lambda: self.stack.setCurrentWidget(self.pages["Inbox"]))
            report_action_row.addWidget(self.report_import_button)
            report_action_row.addStretch(1)
            report_controls.addLayout(report_action_row)
            report_layout.addLayout(report_controls)
            self.report_tabs = QtWidgets.QTabWidget()
            self.report_ranked_table = self._report_table("Top 100 ranked RPA4 reports")
            self.report_recent_table = self._report_table("100 most recently imported RPA4 reports")
            self.report_compare_table = self._report_table("Latest RPA4 report compared with ranked references")
            self.report_tabs.addTab(self.report_ranked_table, "Best 100")
            self.report_tabs.addTab(self.report_recent_table, "Recent 100")
            self.report_tabs.addTab(self.report_compare_table, "Latest vs best 5")
            self.report_tabs.setMinimumHeight(110)
            self.report_tabs.setMaximumHeight(225)
            self.report_tabs.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
            report_layout.addWidget(self.report_tabs)
            report_actions = QtWidgets.QHBoxLayout()
            self.report_open_button = QtWidgets.QPushButton("Open PDF")
            self.report_open_button.setToolTip("Open the archived PDF for the selected report")
            self.report_open_button.clicked.connect(self.open_selected_library_pdf)
            self.report_open_button.setEnabled(False)
            report_actions.addWidget(self.report_open_button)
            self.report_candidate_button = QtWidgets.QPushButton("Candidate details")
            self.report_candidate_button.setToolTip("Open the associated candidate and its validation/provenance details")
            self.report_candidate_button.clicked.connect(self.open_selected_report_candidate)
            self.report_candidate_button.setEnabled(False)
            report_actions.addWidget(self.report_candidate_button)
            report_actions.addStretch(1)
            report_layout.addLayout(report_actions)
            self.report_delta_detail = QtWidgets.QPlainTextEdit()
            self.report_delta_detail.setReadOnly(True)
            self.report_delta_detail.setAccessibleName("Observed differences between the latest report and selected ranked report")
            self.report_delta_detail.setMaximumHeight(74)
            self.report_delta_detail.setPlaceholderText("Select a ranked report to inspect observed metric, input, and report-field changes. Differences are possible drivers, not proven causes.")
            report_layout.addWidget(self.report_delta_detail)
            self.report_compare_table.itemSelectionChanged.connect(self._show_selected_report_comparison)
            self.report_ranked_table.itemSelectionChanged.connect(self._update_report_open_buttons)
            self.report_recent_table.itemSelectionChanged.connect(self._update_report_open_buttons)
            self.report_tabs.currentChanged.connect(self._update_report_open_buttons)
            for table in (self.report_ranked_table, self.report_recent_table, self.report_compare_table):
                table.cellDoubleClicked.connect(lambda _row, _column, target=table: self.open_library_pdf(target))
            self.report_scope.currentIndexChanged.connect(self._refresh_report_library)
            self.report_mode.currentIndexChanged.connect(self._report_mode_changed)
            self.report_metric.currentIndexChanged.connect(self._refresh_report_library)
            self.report_tabs.currentChanged.connect(self._resize_report_tabs)
            study_page.body_layout.insertWidget(1, report_group)
            study_page.body_layout.insertWidget(2, sampling_group)
            feasibility_group = QtWidgets.QGroupBox("Feasibility and review flags")
            feasibility_layout = QtWidgets.QVBoxLayout(feasibility_group)
            self.feasibility_banner = QtWidgets.QLabel("No hard feasibility rules are configured.")
            self.feasibility_banner.setWordWrap(True)
            self.feasibility_banner.setAccessibleName("Design feasibility summary")
            feasibility_layout.addWidget(self.feasibility_banner)
            edit_rules = QtWidgets.QPushButton("Edit feasibility rules…")
            edit_rules.clicked.connect(self.edit_design_constraints)
            feasibility_layout.addWidget(edit_rules, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
            study_page.body_layout.insertWidget(3, feasibility_group)
            workflow = QtWidgets.QGroupBox("Four-step workflow")
            workflow_grid = QtWidgets.QGridLayout(workflow)
            steps = (
                ("1  Define", "Set input ranges and feasibility rules."),
                ("2  Preview", "Check the case count before generating."),
                ("3  Send to RPA4", "Export CFGs, run them, then import PDFs."),
                ("4  Compare", "Rank goals and review the strongest runs."),
            )
            for column, (heading, detail) in enumerate(steps):
                card = QtWidgets.QFrame()
                card.setObjectName("panel")
                card_layout = QtWidgets.QVBoxLayout(card)
                card_layout.setContentsMargins(8, 6, 8, 6)
                label = QtWidgets.QLabel(heading)
                label.setStyleSheet("font-weight: 700;")
                body = QtWidgets.QLabel(detail)
                body.setObjectName("muted")
                body.setWordWrap(True)
                card_layout.addWidget(label)
                card_layout.addWidget(body)
                workflow_grid.addWidget(card, 0, column)
            study_page.body_layout.addWidget(workflow)
            study_page.table.setMaximumHeight(160)
            self.screen_button = self.pages["Screen"].button("Run screening", self.screen, primary=True)
            screen_group = QtWidgets.QGroupBox("Design space preview")
            screen_layout = QtWidgets.QGridLayout(screen_group)
            self.screen_x = QtWidgets.QComboBox()
            self.screen_x.setAccessibleName("Design plot horizontal parameter")
            self.screen_y = QtWidgets.QComboBox()
            self.screen_y.setAccessibleName("Design plot vertical parameter")
            screen_layout.addWidget(QtWidgets.QLabel("Horizontal"), 0, 0)
            screen_layout.addWidget(self.screen_x, 0, 1)
            screen_layout.addWidget(QtWidgets.QLabel("Vertical"), 0, 2)
            screen_layout.addWidget(self.screen_y, 0, 3)
            self.screen_plot = None
            try:
                import pyqtgraph as pg
                self.screen_plot = pg.PlotWidget()
                self.screen_plot.setMinimumHeight(190)
                self.screen_plot.setAccessibleName("Design space scatter plot")
                self.screen_plot.setAccessibleDescription("Candidate inputs in the selected parameter dimensions; the candidate table provides the accessible equivalent")
                screen_layout.addWidget(self.screen_plot, 1, 0, 1, 4)
            except ImportError:
                screen_layout.addWidget(QtWidgets.QLabel("Install the desktop extra to enable design space plots."), 1, 0, 1, 4)
            self.screen_group = screen_group
            screen_group.hide()
            self.pages["Screen"].body_layout.insertWidget(1, screen_group)
            self.screen_x.currentIndexChanged.connect(self._draw_screen_plot)
            self.screen_y.currentIndexChanged.connect(self._draw_screen_plot)
            self.export_preview_button = self.pages["Export"].button("Preview package", self.pack_preview)
            self.export_preview_button.setToolTip("See which candidate CFG files will be included and why any are excluded.")
            zip_button = self.pages["Export"].button("Create RPA4 ZIP", self.pack, primary=True)
            zip_button.setToolTip("Create a portable ZIP with candidate CFG files, manifests, and the RPA4 run sheet.")
            folder_button = self.pages["Export"].button("Create RPA4 folder", lambda: self.pack(folder=True))
            folder_button.setToolTip("Create the same RPA4 handoff files as a regular folder instead of a ZIP.")
            self.open_pack_folder_button = self.pages["Export"].button("Open output folder", self.open_pack_folder)
            self.open_pack_folder_button.setEnabled(False)
            import_button = self.pages["Inbox"].button("Import returned files…", self.import_dialog, primary=True)
            import_button.setToolTip("Choose the ZIP, folder, or PDF/CFG files returned by the RPA4 computer.")
            scan_button = self.pages["Inbox"].button("Scan shared folder", self.rescan)
            scan_button.setToolTip("Check the configured local or shared folder for new RPA4 PDFs and CFG files.")
            stale_button = self.pages["Inbox"].button("Mark case stale / ready", self.toggle_stale)
            stale_button.setToolTip("Toggle the selected staged case between ready and stale. Use this when its RPA4 files are out of date.")
            self.attention_filter = QtWidgets.QCheckBox("Needs attention only")
            self.attention_filter.setAccessibleName("Show inbox items needing attention")
            self._attention_filter_user_changed = False
            self.attention_filter.toggled.connect(self._set_attention_filter)
            self.attention_filter.clicked.connect(lambda: setattr(self, "_attention_filter_user_changed", True))
            inbox_page = self.pages["Inbox"]
            inbox_page.body_layout.insertWidget(1, self.attention_filter)
            self.return_drop_zone = BundleDropZone()
            self.return_drop_zone.dropped.connect(self._preview_import_source)
            inbox_page.body_layout.insertWidget(2, self.return_drop_zone)
            self.pareto_button = self.pages["Results"].button("Pareto trade-offs", lambda: self.rank("pareto"))
            self.pareto_button.setAccessibleName("Rank cases using Pareto trade-offs")
            self.pareto_button.setToolTip("Keeps trade-offs visible. A case ranks first when no other eligible case is as good or better on every selected goal and better on at least one. Several cases can rank first; weights are ignored.")
            self.weighted_button = self.pages["Results"].button("Weighted score", lambda: self.rank("weighted"))
            self.weighted_button.setAccessibleName("Rank cases by weighted score")
            self.weighted_button.setToolTip("Combines the selected goals using their relative weights. A higher 0–1 score is better, and scores are relative to the eligible cases in this study.")
            self.weighted_button.setEnabled(False)
            self.screened_rank_button = self.pages["Results"].button("Quick CEA estimates", lambda: self.rank("pareto", screened=True))
            self.screened_rank_button.setAccessibleName("Rank fast chemistry screening estimates")
            self.screened_rank_button.setToolTip("Ranks fast CEA estimates such as ideal vacuum specific impulse and characteristic velocity. These are screening estimates, not RPA4 delivered results.")
            self.results_compare_button = self.pages["Results"].button("Compare (2–5)", self.compare_selected_results)
            self.results_compare_button.setEnabled(False)
            self.results_compare_button.setToolTip("Select two to five candidate rows in the table to see their report values side by side.")
            self.pages["Results"].table.selectionModel().selectionChanged.connect(self._update_results_compare_button)
            self.pages["Results"].data_model.modelReset.connect(self._update_results_compare_button)
            for button in (self.pareto_button, self.weighted_button, self.screened_rank_button):
                self.pages["Results"].actions.removeWidget(button)
            self.pages["Candidate"].button("Open report PDF", self.open_selected_report)
            self.pages["Finalists"].button("Refresh ranked finalists", self.finalists)
            self.prepare_finalist_button = self.pages["Finalists"].button("Prepare selected case", self.prepare_finalist)
            self.run_finalist_button = self.pages["Finalists"].button("Run selected in WSL", self.run_finalist)
            self.run_finalist_button.setEnabled(False)
            self.pages["Finalists"].button("Compare Mach CSV…", self.compare_finalist_mach)
            self.pages["Doctor"].button("Run checks", self.doctor, primary=True)
            doctor_folder = QtWidgets.QGroupBox("RPA4 return folder on this computer")
            doctor_folder_layout = QtWidgets.QHBoxLayout(doctor_folder)
            self.doctor_shared_path = QtWidgets.QLineEdit(settings.value("shared_rpa4_folder", __import__("os").environ.get("RPA4_SHARED_DIR", "")))
            self.doctor_shared_path.setPlaceholderText("Optional local or shared folder for returned RPA4 PDFs")
            self.doctor_shared_path.setAccessibleName("RPA4 shared return folder")
            doctor_folder_layout.addWidget(self.doctor_shared_path, 1)
            browse_shared = QtWidgets.QPushButton("Browse…")
            browse_shared.clicked.connect(self.browse_shared_folder)
            doctor_folder_layout.addWidget(browse_shared)
            self.pages["Doctor"].body_layout.insertWidget(0, doctor_folder)
            self.pages["Finalists"].notice.setText("CFD results are screening evidence for finalists, not hot-fire validation.")
            self.pages["Results"].notice.setText("Choose what matters, rank the cases, and compare their RPA4 report values. The table keeps each metric's unit.")
            self.ranking_constraints: list[dict[str, Any]] = []
            controls = QtWidgets.QGroupBox("Choose goals and rank cases")
            controls_outer = QtWidgets.QVBoxLayout(controls)
            self.ranking_setup_toggle = QtWidgets.QToolButton()
            self.ranking_setup_toggle.setCheckable(True)
            self.ranking_setup_toggle.setChecked(True)
            self.ranking_setup_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self.ranking_setup_toggle.setToolTip("Choose which performance metrics to rank and set any required pass/fail limits.")
            self.ranking_setup_summary = QtWidgets.QLabel()
            self.ranking_setup_summary.setObjectName("muted")
            self.ranking_setup_summary.setWordWrap(True)
            self.ranking_setup_summary.setMinimumWidth(0)
            self.ranking_setup_summary.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
            self.ranking_setup_summary.setMaximumHeight(112)
            self.ranking_setup_content = QtWidgets.QWidget()
            controls_layout = QtWidgets.QGridLayout(self.ranking_setup_content)
            controls_layout.setContentsMargins(0, 4, 0, 0)
            controls_outer.addWidget(self.ranking_setup_toggle)
            controls_outer.addWidget(self.ranking_setup_summary)
            controls_outer.addWidget(self.ranking_setup_content)
            goals_guide = QtWidgets.QLabel("Choose up to five report metrics. Set whether you want each value higher or lower. A weight matters only for the weighted score; Pareto keeps trade-offs and ignores weights. Optional filters exclude cases that miss a required limit.")
            goals_guide.setObjectName("muted")
            goals_guide.setWordWrap(True)
            goals_guide.setMinimumWidth(0)
            goals_guide.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
            controls_layout.addWidget(goals_guide, 0, 0, 1, 4)
            for column, heading in enumerate(("Metric (source and unit)", "Goal direction", "Weight", "Share of weighted score")):
                controls_layout.addWidget(QtWidgets.QLabel(heading), 1, column)
            self.objective_rows = []
            self.weight_share_labels = []
            extra_goal_widgets = []
            defaults = self.service.spec.ranking.get("objectives", []) if self.service else []
            expand_extra_goals = len(defaults) > 3
            for index in range(5):
                metric = QtWidgets.QComboBox()
                metric.setEditable(True)
                metric.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
                metric.setMinimumWidth(150)
                metric.setMaximumWidth(420)
                metric.setMinimumContentsLength(20)
                metric.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                metric.setAccessibleName(f"Objective {index + 1} metric")
                metric.setToolTip("Select an output. Its source and unit appear in the name, for example RPA4 delivered · Sea level specific impulse (s).")
                direction = QtWidgets.QComboBox()
                direction.addItems(["maximize", "minimize"])
                direction.setMinimumWidth(80)
                direction.setMaximumWidth(110)
                direction.setAccessibleName(f"Objective {index + 1} direction")
                direction.setToolTip("Choose whether larger or smaller values are preferred for this metric.")
                weight = QtWidgets.QDoubleSpinBox()
                weight.setRange(0, 1000)
                weight.setMinimumWidth(65)
                weight.setMaximumWidth(95)
                weight.setDecimals(3)
                weight.setValue(1 if index < len(defaults) else 0)
                weight.setAccessibleName(f"Objective {index + 1} weight")
                weight.setToolTip("Relative importance in Weighted score. Set to 0 to leave this metric out of that score.")
                weight.valueChanged.connect(self._update_weighted_button)
                metric.currentTextChanged.connect(self._update_weighted_button)
                direction.currentTextChanged.connect(self._update_ranking_setup_summary)
                weight.valueChanged.connect(self._update_ranking_setup_summary)
                metric.currentTextChanged.connect(self._update_ranking_setup_summary)
                if index < len(defaults):
                    metric.setCurrentText(str(defaults[index].get("metric", "")))
                    direction.setCurrentText(str(defaults[index].get("direction", "maximize")))
                objective_row = index + 2
                controls_layout.addWidget(metric, objective_row, 0)
                controls_layout.addWidget(direction, objective_row, 1)
                controls_layout.addWidget(weight, objective_row, 2)
                share = QtWidgets.QLabel("0%")
                share.setObjectName("muted")
                share.setMinimumWidth(55)
                share.setMaximumWidth(75)
                share.setAccessibleName(f"Objective {index + 1} normalized weight share")
                share.setToolTip("This metric's percentage of the total positive weight used by Weighted score.")
                controls_layout.addWidget(share, objective_row, 3)
                self.objective_rows.append((metric, direction, weight))
                self.weight_share_labels.append(share)
                if index >= 3:
                    extra_goal_widgets.append((metric, direction, weight, share))
                    for widget in extra_goal_widgets[-1]:
                        widget.setVisible(expand_extra_goals)
            self.more_goals_toggle = QtWidgets.QToolButton()
            self.more_goals_toggle.setCheckable(True)
            self.more_goals_toggle.setChecked(expand_extra_goals)
            self.more_goals_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self.more_goals_toggle.setToolTip("Show two additional metric goals. Most studies need only the first three.")
            controls_layout.addWidget(self.more_goals_toggle, 7, 0, 1, 4)

            def set_extra_goals_visible(visible):
                for row_widgets in extra_goal_widgets:
                    for widget in row_widgets:
                        widget.setVisible(visible)
                label = "Hide the two additional metric goals" if visible else "Show two additional metric goals"
                self.more_goals_toggle.setText(label)
                self.more_goals_toggle.setAccessibleName(label)

            self.more_goals_toggle.toggled.connect(set_extra_goals_visible)
            set_extra_goals_visible(expand_extra_goals)
            controls_layout.setColumnMinimumWidth(0, 180)
            controls_layout.setColumnMinimumWidth(1, 80)
            controls_layout.setColumnMinimumWidth(2, 65)
            controls_layout.setColumnMinimumWidth(3, 58)
            controls_layout.setColumnStretch(0, 5)
            controls_layout.setColumnStretch(1, 2)
            controls_layout.setColumnStretch(2, 1)
            controls_layout.setColumnStretch(3, 2)
            self.filter_toggle = QtWidgets.QToolButton()
            self.filter_toggle.setCheckable(True)
            self.filter_toggle.setChecked(False)
            self.filter_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self.filter_toggle.setAccessibleName("Show optional pass/fail ranking filters")
            controls_layout.addWidget(self.filter_toggle, 8, 0, 1, 4)
            filter_panel = QtWidgets.QWidget()
            filter_layout = QtWidgets.QGridLayout(filter_panel)
            filter_layout.setContentsMargins(0, 4, 0, 0)
            filter_guide = QtWidgets.QLabel("Optional pass/fail filters remove candidates outside the stated limit. Excluded candidates and the reason appear below the ranking.")
            filter_guide.setObjectName("muted")
            filter_guide.setWordWrap(True)
            filter_guide.setMinimumWidth(0)
            filter_guide.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
            filter_layout.addWidget(filter_guide, 0, 0, 1, 4)
            for column, heading in enumerate(("Metric", "Rule", "Limit in metric units", "Active filters")):
                filter_layout.addWidget(QtWidgets.QLabel(heading), 1, column)
            self.constraint_metric = QtWidgets.QComboBox()
            self.constraint_metric.setEditable(True)
            self.constraint_metric.setMinimumWidth(150)
            self.constraint_metric.setMaximumWidth(420)
            self.constraint_metric.setMinimumContentsLength(20)
            self.constraint_metric.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            self.constraint_metric.setAccessibleName("Constraint metric")
            self.constraint_metric.setToolTip("Choose the value that candidates must keep within a required limit.")
            self.constraint_operator = QtWidgets.QComboBox()
            self.constraint_operator.addItems(["<", "<=", "==", ">=", ">"])
            self.constraint_operator.setAccessibleName("Filter rule")
            self.constraint_operator.setToolTip("Choose how the candidate value is compared with the limit.")
            self.constraint_value = QtWidgets.QDoubleSpinBox()
            self.constraint_value.setRange(-1e12, 1e12)
            self.constraint_value.setDecimals(6)
            self.constraint_value.setAccessibleName("Filter limit in selected metric units")
            self.constraint_value.setToolTip("Enter a threshold using the unit shown in the selected metric name.")
            add_constraint = QtWidgets.QPushButton("Add filter")
            add_constraint.clicked.connect(self.add_ranking_constraint)
            clear_constraints = QtWidgets.QPushButton("Clear all filters")
            clear_constraints.clicked.connect(self.clear_ranking_constraints)
            self.constraint_summary = QtWidgets.QListWidget()
            self.constraint_summary.setAccessibleName("Active pass or fail filters")
            self.constraint_summary.setMaximumHeight(96)
            filter_layout.addWidget(self.constraint_metric, 2, 0)
            filter_layout.addWidget(self.constraint_operator, 2, 1)
            filter_layout.addWidget(self.constraint_value, 2, 2)
            filter_layout.addWidget(self.constraint_summary, 2, 3, 2, 1)
            filter_layout.addWidget(add_constraint, 3, 0)
            filter_layout.addWidget(clear_constraints, 3, 1)
            remove_constraint = QtWidgets.QPushButton("Remove selected filter")
            remove_constraint.clicked.connect(self.remove_ranking_constraint)
            filter_layout.addWidget(remove_constraint, 3, 2)
            filter_panel.setVisible(False)
            controls_layout.addWidget(filter_panel, 9, 0, 1, 4)
            self.filter_toggle.toggled.connect(lambda visible: filter_panel.setVisible(visible))
            self.filter_toggle.toggled.connect(lambda visible: self._update_constraint_summary())
            self.ranking_setup_toggle.toggled.connect(self._set_ranking_setup_expanded)
            rank_guide = QtWidgets.QLabel(
                "Pareto shows trade-offs; several cases may rank first. Weighted combines goals into a 0–1 score, where higher is better. "
                "Quick CEA values are estimates, not RPA4 results. Rank again after changing goals or filters."
            )
            rank_guide.setObjectName("muted")
            rank_guide.setWordWrap(True)
            rank_guide.setMinimumWidth(0)
            rank_guide.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
            controls_outer.addWidget(rank_guide)
            rank_buttons = QtWidgets.QGridLayout()
            rank_buttons.setColumnStretch(0, 1)
            rank_buttons.setColumnStretch(1, 1)
            rank_buttons.addWidget(self.pareto_button, 0, 0)
            rank_buttons.addWidget(self.weighted_button, 0, 1)
            rank_buttons.addWidget(self.screened_rank_button, 1, 0, 1, 2)
            controls_outer.addLayout(rank_buttons)
            self._set_ranking_setup_expanded(True)
            self._update_constraint_summary()
            self.pages["Results"].body_layout.insertWidget(0, controls)
            metric_guide = QtWidgets.QLabel(
                "Delivered = RPA4 delivered value; Ideal = theoretical. SL = sea level; Isp = specific impulse; c* = characteristic velocity. "
                "Units appear in the headings; hover one for details. If metrics extend past the right edge, use the bar under the table to reach them."
            )
            metric_guide.setObjectName("muted")
            metric_guide.setWordWrap(True)
            metric_guide.setMinimumWidth(0)
            metric_guide.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
            self.pages["Results"].body_layout.insertWidget(2, metric_guide)
            axes_group = QtWidgets.QGroupBox("Optional plot · candidate metrics")
            axes_layout = QtWidgets.QVBoxLayout(axes_group)
            axis_controls = QtWidgets.QHBoxLayout()
            plot_guide = QtWidgets.QLabel("Each point is one included case. Green circles show rank 1; gray triangles show lower ranks. The table gives exact report values and file checks.")
            plot_guide.setObjectName("muted")
            plot_guide.setWordWrap(True)
            plot_guide.setMinimumWidth(0)
            plot_guide.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
            self.results_plot_guide = plot_guide
            axis_controls.addWidget(QtWidgets.QLabel("Horizontal (X)"))
            self.result_x = QtWidgets.QComboBox()
            self.result_x.setAccessibleName("Results plot horizontal metric")
            self.result_x.setToolTip("Choose the metric shown on the horizontal axis.")
            axis_controls.addWidget(self.result_x, 1)
            axis_controls.addWidget(QtWidgets.QLabel("Vertical (Y)"))
            self.result_y = QtWidgets.QComboBox()
            self.result_y.setAccessibleName("Results plot vertical metric")
            self.result_y.setToolTip("Choose the metric shown on the vertical axis.")
            axis_controls.addWidget(self.result_y, 1)
            axes_layout.addLayout(axis_controls)
            axes_layout.addWidget(plot_guide)
            self.results_axes_group = axes_group
            axes_group.hide()
            if not self.pages["Results"].plot:
                axes_group.hide()
            self.pages["Results"].body_layout.insertWidget(5, axes_group)
            self.result_x.currentIndexChanged.connect(self._draw_results_plot)
            self.result_y.currentIndexChanged.connect(self._draw_results_plot)
            exclusions_group = QtWidgets.QGroupBox("Candidates excluded from this ranking")
            exclusions_layout = QtWidgets.QVBoxLayout(exclusions_group)
            self.results_exclusion_table = QtWidgets.QTableWidget(0, 2)
            self.results_exclusion_table.setAccessibleName("Excluded candidates and reasons")
            self.results_exclusion_table.setHorizontalHeaderLabels(["Candidate", "Reason"])
            self.results_exclusion_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
            self.results_exclusion_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            self.results_exclusion_table.verticalHeader().setVisible(False)
            self.results_exclusion_table.horizontalHeader().setStretchLastSection(True)
            self.results_exclusion_table.setMaximumHeight(180)
            exclusions_layout.addWidget(self.results_exclusion_table)
            self.results_exclusions_group = exclusions_group
            exclusions_group.hide()
            self.pages["Results"].body_layout.insertWidget(6, exclusions_group)
            finalist_setup = QtWidgets.QGroupBox("CFD setup (OpenFOAM)")
            finalist_form = QtWidgets.QGridLayout(finalist_setup)
            finalist_form.addWidget(QtWidgets.QLabel("Ranked candidates to prepare"), 0, 0)
            self.finalist_limit = QtWidgets.QSpinBox()
            self.finalist_limit.setRange(1, 20)
            self.finalist_limit.setValue(3)
            self.finalist_limit.setAccessibleName("Number of ranked CFD finalists")
            self.finalist_limit.setToolTip("Prepare CFD cases for this many of the highest-ranked candidates.")
            finalist_form.addWidget(self.finalist_limit, 0, 1)
            finalist_form.addWidget(QtWidgets.QLabel("Template"), 1, 0)
            self.finalist_template = QtWidgets.QLineEdit()
            self.finalist_template.setAccessibleName("OpenFOAM template directory")
            self.finalist_template.setPlaceholderText("Choose a reviewed OpenFOAM case template")
            self.finalist_template.setToolTip("Reviewed OpenFOAM template containing the mesh, solver, and boundary-condition setup.")
            finalist_form.addWidget(self.finalist_template, 1, 1)
            browse_template = QtWidgets.QPushButton("Browse…")
            browse_template.clicked.connect(self.browse_finalist_template)
            finalist_form.addWidget(browse_template, 1, 2)
            finalist_form.addWidget(QtWidgets.QLabel("WSL case directory"), 2, 0)
            self.finalist_destination = QtWidgets.QLineEdit()
            self.finalist_destination.setAccessibleName("Finalist destination in WSL storage")
            self.finalist_destination.setPlaceholderText(r"\\wsl$\Ubuntu-24.04\home\user\rocket-cases\candidate")
            self.finalist_destination.setToolTip("Folder inside WSL where the prepared OpenFOAM case will be written.")
            finalist_form.addWidget(self.finalist_destination, 2, 1, 1, 2)
            self.finalist_readiness = QtWidgets.QLabel("Run Doctor to check WSL and OpenFOAM availability.")
            self.finalist_readiness.setObjectName("muted")
            self.finalist_readiness.setWordWrap(True)
            finalist_form.addWidget(self.finalist_readiness, 3, 0, 1, 3)
            finalist_page = self.pages["Finalists"]
            finalist_page.body_layout.insertWidget(0, finalist_setup)
            compare_group = QtWidgets.QGroupBox("Station Mach comparison")
            compare_layout = QtWidgets.QVBoxLayout(compare_group)
            self.finalist_compare_table = QtWidgets.QTableView()
            self.finalist_compare_table.setAccessibleName("RPA4 and CFD Mach comparison")
            self.finalist_compare_table.setAccessibleDescription("Station Mach values printed in the RPA4 report compared with imported CFD station values")
            self.finalist_compare_table.setMaximumHeight(155)
            compare_layout.addWidget(self.finalist_compare_table)
            self.finalist_compare_group = compare_group
            compare_group.hide()
            finalist_page.body_layout.addWidget(compare_group)
            finalist_page.table.selectionModel().currentChanged.connect(self._finalist_row_changed)
            self.finalist_limit.valueChanged.connect(self._update_finalist_controls)
            self.finalist_template.textChanged.connect(self._finalist_paths_changed)
            self.finalist_destination.textChanged.connect(self._finalist_paths_changed)
            self._update_finalist_controls()

        @staticmethod
        def _report_table(accessible_name: str):
            table = QtWidgets.QTableWidget(0, 0)
            table.setAccessibleName(accessible_name)
            table.setAlternatingRowColors(True)
            table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
            table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
            table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            table.verticalHeader().setVisible(False)
            table.horizontalHeader().setStretchLastSection(False)
            table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Interactive)
            table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            table.setSizeAdjustPolicy(QtWidgets.QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
            table.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
            table.setSortingEnabled(False)
            return table

        def _report_mode_changed(self, *_):
            if not hasattr(self, "report_metric"):
                return
            separate = self.report_mode.currentData() == "per_metric"
            self.report_metric.setEnabled(separate)
            self.report_metric.setVisible(separate)
            self.report_metric_label.setVisible(separate)
            self._refresh_report_library()

        def _resize_report_tabs(self, *_):
            if not hasattr(self, "report_tabs"):
                return
            table = self._active_report_table()
            visible_rows = min(5, max(1, table.rowCount()))
            header_height = max(28, table.horizontalHeader().sizeHint().height())
            row_height = max(28, table.rowHeight(0) if table.rowCount() else table.verticalHeader().defaultSectionSize())
            tab_height = self.report_tabs.tabBar().sizeHint().height()
            target_height = tab_height + header_height + visible_rows * row_height + 18
            self.report_tabs.setFixedHeight(min(225, max(110, target_height)))

        def _refresh_report_library(self, *_):
            service = self.service
            if not service or not hasattr(self, "report_scope"):
                return
            try:
                profile = service.report_profile()
                names = [item["metric"] for item in profile["metrics"]]
                selected = self.report_metric.currentData()
                self.report_metric.blockSignals(True)
                self.report_metric.clear()
                for name in names:
                    descriptor = getattr(self, "metric_descriptors", {}).get(name)
                    suffix = f" ({descriptor.unit})" if descriptor and descriptor.unit else ""
                    self.report_metric.addItem(f"{name}{suffix}", name)
                index = self.report_metric.findData(selected) if selected else -1
                self.report_metric.setCurrentIndex(index if index >= 0 else (0 if names else -1))
                self.report_metric.blockSignals(False)
                separate_metric = self.report_mode.currentData() == "per_metric"
                self.report_metric.setEnabled(separate_metric)
                self.report_metric.setVisible(separate_metric)
                self.report_metric_label.setVisible(separate_metric)
                scope = str(self.report_scope.currentData() or "study")
                mode = str(self.report_mode.currentData() or "weighted")
                metric = self.report_metric.currentData()
                ranked = service.rank_reports(scope=scope, mode=mode, selected_metric=str(metric) if metric else None)
                recent = service.list_reports(scope=scope, limit=100)
                self._populate_report_table(self.report_ranked_table, ranked.get("entries", [])[:100], ranked=True)
                self._populate_report_table(self.report_recent_table, recent, ranked=False)
                self._update_report_comparisons(service.compare_latest_report(
                    scope=scope, mode=mode, selected_metric=str(metric) if metric else None
                ), profile)
                self._resize_report_tabs()
                all_rows = service.list_reports(scope=scope)
                eligible_count = len(ranked.get("entries", []))
                scope_label = "this Study" if scope == "study" else "the local archive"
                if all_rows:
                    latest = all_rows[0]
                    validation = str(latest.get("validation_status") or "unknown").replace("_", " ").title()
                    next_step = (
                        "Only one rankable report; import another complete run to compare."
                        if eligible_count == 1
                        else "No report matches all selected ranking metrics; review the profile or report values."
                        if eligible_count == 0
                        else ""
                    )
                    self.report_summary.setText(
                        f"{len(all_rows)} RPA4 report(s) in {scope_label} · {eligible_count} rankable · "
                        f"Latest: {latest.get('case_name') or latest.get('report_id')} — {validation}. "
                        f"{next_step}"
                    )
                else:
                    self.report_summary.setText(
                        f"No RPA4 reports in {scope_label} yet. Import returned PDFs from Inbox to start ranking."
                    )
                self._update_report_open_buttons()
            except Exception as error:
                self.report_summary.setText(f"Report library could not be refreshed: {type(error).__name__}: {error}")
                self.report_delta_detail.setPlainText("")

        def _populate_report_table(self, table, reports, *, ranked: bool):
            columns = ["Rank", "RPA4 case", "Study", "Imported", "Validation"]
            if ranked:
                columns.append("Goal score")
            columns.append("Report ID")
            table.clear()
            table.clearSpans()
            table.setColumnCount(len(columns))
            table.setHorizontalHeaderLabels(columns)
            table.setColumnHidden(len(columns) - 1, True)
            table.setRowCount(len(reports) if reports else 1)
            if not reports:
                table.setSpan(0, 0, 1, len(columns))
                message = "No reports match this ranking." if ranked else "No RPA4 reports have been imported yet."
                table.setItem(0, 0, QtWidgets.QTableWidgetItem(message))
                table.setRowHeight(0, 36)
            for row_index, report in enumerate(reports):
                values = [
                    report.get("rank", ""),
                    report.get("case_name", ""),
                    report.get("study_id", ""),
                    str(report.get("imported_at", "")).replace("T", " ")[:19],
                    self._validation_text(report.get("validation_status", "unknown")),
                ]
                if ranked:
                    score = report.get("score")
                    values.append(f"{float(score):.4f}" if isinstance(score, (int, float)) else "")
                values.append(report.get("report_id", ""))
                for column, value in enumerate(values):
                    item = QtWidgets.QTableWidgetItem("—" if value is None else str(value))
                    if column == 0:
                        item.setData(QtCore.Qt.ItemDataRole.UserRole, report.get("report_id"))
                        item.setToolTip(f"Report ID: {report.get('report_id', '')}")
                    if column == 1:
                        item.setToolTip(str(report.get("case_name", "")))
                    table.setItem(row_index, column, item)
                table.setRowHeight(row_index, 28)
            if ranked:
                score_column = table.horizontalHeaderItem(5)
                if score_column:
                    score_column.setToolTip("Normalized fit to the selected report goals; higher is better.")
            widths = (48, 150, 115, 135, 125, 105, 190) if ranked else (48, 150, 115, 135, 125, 190)
            for column, width in enumerate(widths):
                if column < table.columnCount():
                    table.setColumnWidth(column, width)
                    table.horizontalHeader().setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.Interactive)
            for column in (1, 2, 4):
                if column < table.columnCount():
                    table.horizontalHeader().setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.Stretch)

        def _update_report_comparisons(self, comparison, profile):
            latest = comparison.get("latest")
            references = comparison.get("comparisons", [])
            metrics = profile.get("metrics", [])
            self.report_comparison_by_id = {item["reference"]["report_id"]: item for item in references}
            headers = ["Rank", "RPA4 case", "Validation"]
            headers.extend(f"Δ {item['metric']}" for item in metrics)
            headers.append("Observed changes")
            table = self.report_compare_table
            table.clear()
            table.clearSpans()
            table.setColumnCount(len(headers))
            table.setHorizontalHeaderLabels(headers)
            table.setRowCount(len(references) if references else 1)
            if not references:
                table.setSpan(0, 0, 1, len(headers))
                message = (
                    "Import more RPA4 runs to compare the latest result with the best reports."
                    if latest else "Import returned RPA4 PDFs from Inbox to start comparing runs."
                )
                table.setItem(0, 0, QtWidgets.QTableWidgetItem(message))
                table.setRowHeight(0, 36)
            for row_index, pair in enumerate(references):
                reference = pair["reference"]
                values = [reference.get("rank", row_index + 1), reference.get("case_name", ""), self._validation_text(reference.get("validation_status", "unknown"))]
                for delta in pair.get("metric_deltas", []):
                    latest_value, reference_value, change = delta.get("latest"), delta.get("reference"), delta.get("delta")
                    if latest_value is None or reference_value is None:
                        values.append("missing")
                    else:
                        unit = f" {delta.get('unit')}" if delta.get("unit") else ""
                        sign = "+" if change is not None and change > 0 else ""
                        values.append(f"{float(latest_value):.5g} vs {float(reference_value):.5g} ({sign}{float(change):.4g}{unit})")
                changes = len(pair.get("parameter_deltas", [])) + len(pair.get("report_deltas", []))
                values.append(f"{len(pair.get('parameter_deltas', []))} input · {len(pair.get('report_deltas', []))} report field(s)")
                for column, value in enumerate(values):
                    cell = QtWidgets.QTableWidgetItem(str(value))
                    if column == 0:
                        cell.setData(QtCore.Qt.ItemDataRole.UserRole, reference.get("report_id"))
                    if column == len(values) - 1:
                        cell.setToolTip(f"{changes} observed input and report-field difference(s); these are possible drivers, not proven causes.")
                    table.setItem(row_index, column, cell)
                table.setRowHeight(row_index, 28)
            for column, width in enumerate((55, 145, 130, *([220] * len(metrics)), 180)):
                if column < table.columnCount():
                    table.setColumnWidth(column, width)
            table.horizontalHeader().setStretchLastSection(True)
            if latest:
                validation = str(latest.get("validation_status") or "unknown").replace("_", " ")
                self.report_delta_detail.setPlainText(
                    f"Latest: {latest.get('case_name')} · {validation} validation. "
                    + ("Select a reference row to inspect observed differences." if references else "Import another run to see a direct report comparison.")
                )
            else:
                self.report_delta_detail.setPlainText("Import an RPA4 report to compare its metrics and observed inputs with the top five reports.")

        def _selected_report_id_from(self, table):
            row = table.currentRow()
            if row < 0 or table.columnCount() == 0 or table.item(row, 0) is None:
                return None
            value = table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
            return str(value) if value else None

        def _active_report_table(self):
            return (self.report_ranked_table, self.report_recent_table, self.report_compare_table)[self.report_tabs.currentIndex()]

        def _update_report_open_buttons(self, *_):
            if not hasattr(self, "report_tabs"):
                return
            report_id = self._selected_report_id_from(self._active_report_table())
            record = self.service.get_report(report_id) if self.service and report_id else None
            available = bool(record and record.get("report_path") and Path(record["report_path"]).is_file())
            self.report_open_button.setEnabled(available)
            self.report_candidate_button.setEnabled(bool(record))

        def open_selected_library_pdf(self):
            self.open_library_pdf(self._active_report_table())

        def open_library_pdf(self, table):
            if not self.service:
                return
            report_id = self._selected_report_id_from(table)
            record = self.service.get_report(report_id) if report_id else None
            report_path = Path(record["report_path"]) if record and record.get("report_path") else None
            if report_path and report_path.is_file():
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(report_path)))
            else:
                self.statusBar().showMessage("The archived PDF is unavailable for that report entry.", 6000)

        def _show_selected_report_comparison(self, *_):
            report_id = self._selected_report_id_from(self.report_compare_table)
            pair = getattr(self, "report_comparison_by_id", {}).get(report_id)
            if not pair:
                self._update_report_open_buttons()
                return
            lines = ["Observed differences from the latest report (possible drivers, not proven causes):"]
            metrics = pair.get("metric_deltas", [])
            if metrics:
                lines.append("Metrics: " + "; ".join(
                    f"{item['metric']} {item.get('latest')} vs {item.get('reference')} (Δ {item.get('delta')}{' ' + item['unit'] if item.get('unit') else ''})"
                    for item in metrics
                ))
            parameters = pair.get("parameter_deltas", [])
            lines.append("Inputs: " + ("; ".join(
                f"{item['field']} {item.get('latest')} vs {item.get('reference')}" +
                (f" {item['unit']}" if item.get("unit") else "" if item.get("comparable") else " (units differ; not directly comparable)")
                for item in parameters
            ) if parameters else "no input differences found"))
            fields = pair.get("report_deltas", [])
            lines.append("Report fields: " + ("; ".join(
                f"{item['field']} {item.get('latest')} vs {item.get('reference')}" + (f" (Δ {item['delta']})" if item.get("delta") is not None else "")
                for item in fields
            ) if fields else "no report-field differences found"))
            lines.append("Observed deltas describe correlation within these reports; they do not prove physical causation.")
            self.report_delta_detail.setPlainText("\n".join(lines))
            self._update_report_open_buttons()

        def open_selected_report_candidate(self):
            if not self.service:
                return
            record = self.service.get_report(self._selected_report_id_from(self._active_report_table()) or "")
            if not record:
                return
            study_path = Path(record["study_path"])
            if study_path.resolve() != self.service.study_path.resolve():
                self.load_study(study_path)
            self.stack.setCurrentWidget(self.pages["Candidate"])
            for row in range(self.pages["Candidate"].data_model.rowCount()):
                candidate_id = str(self.pages["Candidate"].data_model.index(row, 0).data() or "")
                if candidate_id == record["candidate_id"]:
                    self.pages["Candidate"].table.selectRow(row)
                    self._show_candidate_detail_for_id(candidate_id)
                    break

        def edit_report_profile(self):
            service = self._require_service()
            if not service:
                return
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("RPA4 metric analysis profile")
            dialog.resize(1080, 360)
            layout = QtWidgets.QVBoxLayout(dialog)
            layout.addWidget(QtWidgets.QLabel("Choose up to five metrics. Goals are evaluated using preference percentiles so units do not get mixed in the combined ranking."))
            table = QtWidgets.QTableWidget(5, 5)
            table.setHorizontalHeaderLabels(["Metric", "Goal", "Target value", "Weight", "Priority"])
            table.verticalHeader().setVisible(False)
            descriptors = service.metric_catalog()
            profile = service.report_profile()
            cells = []
            for row in range(5):
                metric = QtWidgets.QComboBox()
                metric.setEditable(True)
                metric.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
                metric.addItem("", "")
                for descriptor in descriptors:
                    metric.addItem(self._metric_label(descriptor.name, descriptor), descriptor.name)
                goal = QtWidgets.QComboBox()
                goal.addItem("Maximize", "maximize")
                goal.addItem("Minimize", "minimize")
                goal.addItem("Match target", "target")
                target = QtWidgets.QDoubleSpinBox()
                target.setRange(-1e15, 1e15)
                target.setDecimals(6)
                target.setAccessibleName(f"Metric {row + 1} target value")
                weight = QtWidgets.QDoubleSpinBox()
                weight.setRange(0, 1000)
                weight.setDecimals(3)
                weight.setValue(1 if row == 0 else 0)
                weight.setAccessibleName(f"Metric {row + 1} weight")
                priority = QtWidgets.QSpinBox()
                priority.setRange(1, 5)
                priority.setValue(row + 1)
                priority.setAccessibleName(f"Metric {row + 1} priority")
                saved = profile["metrics"][row] if row < len(profile["metrics"]) else None
                if saved:
                    found = metric.findData(saved["metric"])
                    if found >= 0:
                        metric.setCurrentIndex(found)
                    else:
                        metric.setCurrentText(saved["metric"])
                    goal.setCurrentIndex(max(0, goal.findData(saved["goal"])))
                    target.setValue(float(saved.get("target", 0)))
                    weight.setValue(float(saved.get("weight", 1)))
                    priority.setValue(int(saved.get("priority", row + 1)))
                metric.setAccessibleName(f"Analysis metric {row + 1}")
                table.setCellWidget(row, 0, metric)
                table.setCellWidget(row, 1, goal)
                table.setCellWidget(row, 2, target)
                table.setCellWidget(row, 3, weight)
                table.setCellWidget(row, 4, priority)
                goal.currentIndexChanged.connect(lambda _index, g=goal, t=target: t.setEnabled(g.currentData() == "target"))
                target.setEnabled(goal.currentData() == "target")
                cells.append((metric, goal, target, weight, priority))
            table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
            layout.addWidget(table)
            buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
            layout.addWidget(buttons)
            buttons.rejected.connect(dialog.reject)

            def save():
                metrics = []
                for metric, goal, target, weight, priority in cells:
                    name = str(metric.currentData() or metric.currentText()).strip()
                    if name:
                        metrics.append({
                            "metric": name,
                            "goal": str(goal.currentData()),
                            "target": target.value(),
                            "weight": weight.value(),
                            "priority": priority.value(),
                        })
                try:
                    service.save_report_profile({"metrics": metrics})
                except Exception as error:
                    QtWidgets.QMessageBox.warning(dialog, "Invalid metric profile", str(error))
                    return
                dialog.accept()
                self._refresh_report_library()

            buttons.accepted.connect(save)

        def _refresh_metric_choices(self):
            if not self.service:
                return
            descriptors = self.service.metric_catalog()
            self.metric_descriptors = {item.name: item for item in descriptors}
            ranking = self.service.ranking_snapshot()
            saved_objectives = ranking.get("objectives", self.service.spec.ranking.get("objectives", []))
            has_extra_objectives = len(saved_objectives) > 3 or any(
                self._metric_for_widget(metric) for metric, _direction, _weight in self.objective_rows[3:]
            )
            self.more_goals_toggle.setChecked(has_extra_objectives)
            defaults = [str(item.get("metric", "")) for item in saved_objectives]
            saved_weights = ranking.get("weights", self.service.spec.ranking.get("weights", {}))
            for index, (metric, direction, weight) in enumerate(self.objective_rows):
                current = self._metric_for_widget(metric)
                target = current or (defaults[index] if index < len(defaults) else "")
                self._fill_metric_picker(metric, descriptors, target, allow_empty=True)
                if index < len(saved_objectives):
                    direction.setCurrentText(str(saved_objectives[index].get("direction", "maximize")))
                    weight.setValue(float(saved_weights.get(defaults[index], 1.0)))
            self.ranking_constraints = list(ranking.get("constraints", self.service.spec.ranking.get("constraints", [])))
            self._update_constraint_summary()
            current_constraint = self._metric_for_widget(self.constraint_metric)
            self._fill_metric_picker(self.constraint_metric, descriptors, current_constraint, allow_empty=True)
            self._prepare_results_axes()
            self._update_weighted_button()
            self._update_ranking_setup_summary()

        def _fill_metric_picker(self, combo, descriptors, target="", *, allow_empty=False):
            combo.blockSignals(True)
            combo.setEditable(True)
            combo.clear()
            grouped = {}
            for descriptor in descriptors:
                grouped.setdefault(str(descriptor.group or "Other"), []).append(descriptor)
            completion_labels = []
            first_metric_index = -1
            for group_name in sorted(grouped, key=str.casefold):
                combo.addItem(f"— {group_name} —")
                group_item = combo.model().item(combo.count() - 1)
                if group_item is not None:
                    group_item.setEnabled(False)
                for descriptor in sorted(grouped[group_name], key=lambda item: (item.tier, item.name)):
                    suffix = f" ({descriptor.unit})" if descriptor.unit else ""
                    label = self._metric_label(descriptor.name, descriptor)
                    combo.addItem(label, descriptor.name)
                    combo.setItemData(combo.count() - 1, f"Source: {label.split(' · ', 1)[0]}. Unit: {descriptor.unit or 'unitless'}.", QtCore.Qt.ItemDataRole.ToolTipRole)
                    completion_labels.append(label)
                    if first_metric_index < 0:
                        first_metric_index = combo.count() - 1
            found = combo.findData(target) if target else -1
            if found >= 0:
                combo.setCurrentIndex(found)
            elif target:
                combo.addItem(target, target)
                combo.setCurrentIndex(combo.count() - 1)
                completion_labels.append(target)
            elif allow_empty:
                combo.setCurrentIndex(-1)
            elif first_metric_index >= 0:
                combo.setCurrentIndex(first_metric_index)
            completer = QtWidgets.QCompleter(completion_labels, combo)
            completer.setCaseSensitivity(QtCore.Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(QtCore.Qt.MatchFlag.MatchContains)
            combo.setCompleter(completer)
            completer.activated.connect(lambda label, target_combo=combo: self._select_metric_label(target_combo, label))
            combo.blockSignals(False)

        @staticmethod
        def _select_metric_label(combo, label):
            index = combo.findText(str(label), QtCore.Qt.MatchFlag.MatchExactly)
            if index >= 0:
                combo.setCurrentIndex(index)

        def _metric_label(self, name, descriptor=None):
            descriptor = descriptor or getattr(self, "metric_descriptors", {}).get(name)
            if not descriptor:
                return name
            parts = str(name).split(".")
            root = parts[0].casefold() if parts else ""
            source_labels = {
                "estimated_delivered": "RPA4 delivered",
                "ideal": "RPA4 ideal",
                "derived": "Calculated from report",
                "screening": "Fast screening estimate",
                "design": "Design input",
            }
            source = source_labels.get(root, "RPA4 report")
            detail_parts = parts[1:] if root in source_labels or root in {"geometry", "propellants", "station_conditions", "altitude_sweep"} else parts
            tokens = [part for part in (detail_parts[-1].split("_") if detail_parts else [])]
            unit_suffixes = {
                "s": ("s",), "m/s": ("m", "s"), "kN": ("kn",), "kg/s": ("kg", "s"),
                "MPa": ("mpa",), "atm": ("atm",), "km": ("km",), "K": ("k",),
                "Mach": ("mach",), "fraction": ("fraction",), "mm": ("mm",), "in": ("in",), "ratio": ("ratio",),
            }
            suffix = tuple(token.casefold() for token in unit_suffixes.get(descriptor.unit, ()))
            if suffix and tuple(token.casefold() for token in tokens[-len(suffix):]) == suffix:
                tokens = tokens[:-len(suffix)]
                if detail_parts:
                    detail_parts[-1] = "_".join(tokens)
            detail = " ".join(part.replace("_", " ") for part in detail_parts).strip()
            detail = detail.title().replace("Cea", "CEA")
            unit = f" ({descriptor.unit})" if descriptor.unit else ""
            return f"{source} · {detail}{unit}" if detail else f"{source}{unit}"

        def _metric_column_label(self, name):
            """Use a short table label; keep the complete metric name in its tooltip."""
            descriptor = getattr(self, "metric_descriptors", {}).get(name)
            if not descriptor:
                return str(name)
            parts = str(name).split(".")
            root = parts[0].casefold()
            source = {
                "estimated_delivered": "Delivered",
                "ideal": "Ideal",
                "derived": "Calc.",
                "screening": "Screening",
                "design": "Input",
            }.get(root, "RPA4")
            conditions = {
                "sea_level": "SL",
                "optimum_expansion": "Optimum expansion",
                "vacuum": "Vacuum",
                "injector": "Injector",
                "nozzle_inlet": "Nozzle inlet",
                "nozzle_throat": "Throat",
                "nozzle_exit": "Nozzle exit",
                "chamber": "Chamber",
            }
            condition = next((conditions[item] for item in parts[1:-1] if item in conditions), "")
            leaf_names = {
                "specific_impulse_s": "Isp",
                "effective_exhaust_velocity_m_s": "Exhaust velocity",
                "characteristic_velocity_m_s": "c*",
                "characteristic_velocity_efficiency": "C* efficiency",
                "thrust_coefficient": "Thrust coeff.",
                "thrust_kn": "Thrust",
                "mass_flow_kg_s": "Mass flow",
                "ambient_pressure_atm": "Ambient pressure",
                "pressure_mpa": "Pressure",
                "pressure_atm": "Pressure",
                "temperature_k": "Temperature",
                "mach": "Mach",
                "altitude_km": "Altitude",
                "exit_condensed_mass_fraction": "Condensed mass fraction at exit",
                "sea_level_exit_pressure_ratio": "Sea-level exit pressure ratio",
                "separation_risk_flag": "Nozzle separation risk flag",
            }
            leaf = leaf_names.get(parts[-1].casefold(), parts[-1].replace("_", " ").title())
            detail = " ".join(part for part in (condition, leaf) if part)
            unit = f" ({descriptor.unit})" if descriptor.unit else ""
            return f"{source} · {detail}{unit}" if detail else f"{source}{unit}"

        @staticmethod
        def _wrap_results_metric_header(label, line_limit=25):
            """Keep the complete metric label while making its table header compact."""
            source, separator, detail = str(label).partition(" · ")
            if not separator:
                return str(label)
            lines = [source]
            current = ""
            for word in detail.split():
                if current and len(current) + len(word) + 1 > line_limit:
                    lines.append(current)
                    current = word
                else:
                    current = f"{current} {word}".strip()
            if current:
                lines.append(current)
            return "\n".join(lines)

        def _metric_tooltip(self, name):
            descriptor = getattr(self, "metric_descriptors", {}).get(name)
            unit = descriptor.unit if descriptor else ""
            unit_text = {
                "s": "seconds",
                "m/s": "meters per second",
                "kN": "kilonewtons",
                "kg/s": "kilograms per second",
                "MPa": "megapascals",
                "atm": "atmospheres",
                "km": "kilometers",
                "K": "kelvin",
                "Mach": "Mach (speed relative to the local speed of sound)",
                "fraction": "fraction from 0 to 1",
                "unitless": "unitless",
            }.get(unit, unit)
            leaf = str(name).split(".")[-1].casefold()
            meaning = {
                "specific_impulse_s": "Specific impulse (Isp) indicates how efficiently propellant produces thrust. Compare it at the same operating condition.",
                "characteristic_velocity_efficiency": "Calculated as RPA4 delivered characteristic velocity (c*) divided by ideal c*. A value of 1.0 is 100% of the ideal value.",
                "thrust_coefficient": "A unitless measure of how the nozzle converts chamber pressure into thrust.",
                "characteristic_velocity_m_s": "Characteristic velocity (c*) describes combustion performance before nozzle expansion.",
                "mach": "The flow speed divided by the local speed of sound.",
                "effective_exhaust_velocity_m_s": "Effective exhaust velocity is equivalent exhaust speed derived from thrust and propellant flow.",
                "thrust_kn": "Engine thrust at the operating condition named in this column.",
                "mass_flow_kg_s": "Propellant mass flowing through the engine per second.",
            }.get(leaf, "The value is read from the RPA4 report or calculated from report values, as shown in the column label.")
            full_name = self._metric_label(name)
            unit_line = f"Unit: {unit_text}." if unit_text else "Check the source report for its unit."
            return f"{full_name}\n\n{meaning}\n{unit_line} The table value retains the report precision."

        @staticmethod
        def _results_validation_text(status):
            value = str(status or "unknown").casefold()
            if value == "review_required":
                return "Needs review"
            return MainWindow._validation_text(value)

        def _ranking_table_headers(self, mode, metric_names):
            headers = ["Rank"]
            tooltips = ["Rank within the selected ranking method. Pareto rank 1 identifies trade-off leaders; weighted rankings are ordered by score."]
            if mode == "weighted":
                headers.append("Score (0–1)")
                tooltips.append("Combined score for the selected goals. Higher is better. Scores are relative to the eligible cases in this study and can change when cases or filters change.")
            headers += ["Candidate ID", "RPA4 case", "RPA4 check"]
            tooltips += [
                "Internal identifier for this design case. Hidden by default; show it in Columns if needed.",
                "Name of the case in the RPA4 report.",
                "File-match check between the design configuration and RPA4 report. Match means they agree on checked values. Needs review means manually compare both files. Mismatch cases are excluded.",
            ]
            page = self.pages["Results"]
            page.metric_column_ids = {}
            for name in metric_names:
                headers.append(self._wrap_results_metric_header(self._metric_label(name)))
                tooltips.append(self._metric_tooltip(name))
                page.metric_column_ids[len(headers) - 1] = str(name)
            return headers, tooltips

        @staticmethod
        def _metric_for_widget(widget):
            data = widget.currentData()
            return str(data) if data else widget.currentText().strip()
        def load_study(self, path: Path):
            resolved_path = path.resolve()
            study_changed = self.current_path != resolved_path
            try:
                new_service = StudyService(path, archive_root=self.archive_root)
            except Exception as error:
                QtWidgets.QMessageBox.critical(self, "Could not open study", str(error))
                return
            known_paths = settings.value("known_study_paths", [])
            if isinstance(known_paths, str):
                known_paths = [known_paths]
            normalized_known = {str(Path(item).expanduser().resolve()) for item in known_paths if item}
            normalized_known.add(str(resolved_path))
            for other_path in sorted(normalized_known):
                if other_path == str(resolved_path) or not Path(other_path).is_file():
                    continue
                try:
                    registered = StudyService(other_path, archive_root=self.archive_root)
                    registered.close()
                except Exception:
                    continue
            settings.setValue("known_study_paths", sorted(normalized_known))
            if self.service:
                active = [job for job in self.service.jobs.jobs() if job.state.value in {"queued", "running"}]
                if active:
                    answer = QtWidgets.QMessageBox.question(self, "Study jobs running", "Cancel active jobs and open the new study?", QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
                    if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                        new_service.close(wait=False)
                        return
                    for job in active:
                        self.service.jobs.cancel(job.job_id)
                self.service.close(wait=False)
            self.service = new_service
            if study_changed:
                self._set_ranking_setup_expanded(True)
                for metric, _direction, weight in self.objective_rows:
                    metric.setCurrentIndex(-1)
                    metric.setEditText("")
                    weight.setValue(0)
                self.constraint_metric.setCurrentIndex(-1)
                self.constraint_metric.setEditText("")
                for combo in (self.result_x, self.result_y):
                    combo.setCurrentIndex(-1)
                    combo.setEditText("")
                self.ranking_constraints = []
            self._attention_filter_user_changed = False
            self.attention_filter.setChecked(False)
            self.last_ranking = None
            self._display_ranking_exclusions([])
            self.finalist_ids = []
            self.prepared_case = False
            self.prepared_candidate_id = None
            self.finalist_compare_group.hide()
            self.open_pack_folder_button.setEnabled(False)
            if self.pages["Results"].plot:
                self.pages["Results"].plot.clear()
                self.pages["Results"].plot.hide()
            if self.screen_plot:
                self.screen_plot.clear()
                self.screen_group.hide()
            for view in self.candidate_detail_views.values():
                view.setPlainText("Select a candidate to load this section.")
            self.current_path = resolved_path
            self.study_label.setText(self.current_path.name)
            self.statusBar().showMessage(f"Loaded {self.current_path}")
            self._refresh_metric_choices()
            self._refresh_study()
            self._refresh_statuses()

        def open_dialog(self):
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open study", str(Path.cwd()), "Study JSON (*.json)")
            if path:
                self.load_study(Path(path))

        def new_study_dialog(self):
            cfg, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Choose base RPA4 CFG", str(Path.home()), "RPA4 CFG (*.cfg)")
            if not cfg:
                return
            output, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save study spec", str(Path(cfg).with_suffix(".study.json")), "Study JSON (*.json)")
            if not output:
                return
            try:
                path = StudyService.create_from_cfg(cfg, output)
            except Exception as error:
                QtWidgets.QMessageBox.critical(self, "Could not create study", str(error))
                return
            self.load_study(path)

        def _require_service(self) -> StudyService | None:
            if not self.service:
                QtWidgets.QMessageBox.information(self, "Open a study", "Open a study JSON or create one from an RPA4 CFG first.")
                return None
            return self.service

        def _submit(self, title: str, operation, refresh=None):
            service = self._require_service()
            if not service:
                return

            def execute(context):
                context.report(0.1, title)
                result = operation(service, context)
                context.checkpoint()
                context.report(0.95, "Updating views")
                return result

            job = service.submit(title, execute)
            self._job_states[job.job_id] = "queued"
            if refresh:
                QtCore.QTimer.singleShot(800, refresh)

        def _refresh_jobs(self):
            if not self.service:
                return
            jobs = self.service.jobs.jobs()
            active = [job for job in jobs if job.state.value in {"queued", "running"}]
            self._visible_jobs = list(jobs[-20:])
            self.job_strip.setText(f"Jobs: {len(active)} active  ·  click to {'hide' if self.job_table.isVisible() else 'expand'}")
            model = QtGui.QStandardItemModel(self)
            model.setHorizontalHeaderLabels(["Job", "State", "Progress", "Message"])
            for job in self._visible_jobs:
                model.appendRow([QtGui.QStandardItem(job.title), QtGui.QStandardItem(job.state.value), QtGui.QStandardItem("—" if job.progress is None else f"{job.progress:.0%}"), QtGui.QStandardItem(job.message)])
                previous = self._job_states.get(job.job_id)
                if previous != job.state.value and job.state.value in {"done", "failed", "cancelled"}:
                    self.statusBar().showMessage(f"{job.title}: {job.state.value}" + (f" ({job.error})" if job.error else ""), 10000)
                    if job.state.value == "done":
                        self._show_job_result(job.title, job.result)
                        self._refresh_study()
                        self._refresh_statuses()
                    elif job.state.value == "failed":
                        target = "Study" if "Generate" in job.title else "Screen" if "Screen" in job.title else "Export" if "pack" in job.title.casefold() else "Inbox" if "RPA4" in job.title or "Import" in job.title else "Finalists" if "OpenFOAM" in job.title else "Doctor" if "Doctor" in job.title else "Results"
                        self.pages[target].notice.setText(f"{job.title} failed: {job.error or job.message}")
                self._job_states[job.job_id] = job.state.value
            self.job_table.setModel(model)

        def cancel_selected_job(self):
            if not self.service:
                return
            index = self.job_table.currentIndex()
            if index.isValid() and index.row() < len(self._visible_jobs):
                self.service.jobs.cancel(self._visible_jobs[index.row()].job_id)

        def open_selected_job_details(self):
            index = self.job_table.currentIndex()
            if not index.isValid() or index.row() >= len(self._visible_jobs):
                self.statusBar().showMessage("Select a job row to see its current message or error.", 5000)
                return
            job = self._visible_jobs[index.row()]
            detail = f"Job: {job.title}\nState: {job.state.value}\nProgress: {job.progress if job.progress is not None else 'indeterminate'}\nMessage: {job.message}\nError: {job.error or 'None'}"
            QtWidgets.QMessageBox.information(self, "Job details", detail)

        def _show_job_result(self, title: str, result: Any):
            if result is None:
                return
            if title == "Run Doctor checks":
                self._show_doctor(result)
            elif title == "Screen candidates" and isinstance(result, dict):
                rows = []
                for candidate_id, outcome in result.items():
                    cea = outcome.get("cea", {})
                    cea_result = cea.get("result", {})
                    cantera = outcome.get("cantera", {})
                    rows.append([candidate_id, cea.get("status"), cea_result.get("ideal_vacuum_specific_impulse_m_s"), cea_result.get("characteristic_velocity_m_s"), cea_result.get("exit_mach"), cantera.get("status")])
                page = self.pages["Screen"]
                page.show_rows(["Candidate", "CEA status", "CEA ideal vacuum Isp (m/s)", "CEA c* (m/s)", "CEA exit Mach", "Cantera status"], rows)
                page.notice.setText(f"{len(rows)} candidate(s) screened. CEA values are ideal outputs; Cantera is a chamber-state model only.")
                if rows:
                    self._prepare_screen_plot(rows)
                    self._draw_screen_plot()
                else:
                    page.set_empty("No candidates to screen", "Generate candidate CFG files in Study before running analysis models.", "Go to Study", lambda: self.stack.setCurrentWidget(self.pages["Study"]))
            elif title.startswith("Rank") and hasattr(result, "entries"):
                self.last_ranking = result
                entries = result.entries
                metric_names = [str(item["metric"]) for item in result.objectives]
                headers, header_tooltips = self._ranking_table_headers(result.mode, metric_names)
                rows = []
                for entry in entries:
                    row = [entry.rank]
                    if result.mode == "weighted":
                        score = entry.weighted_score
                        row.append(f"{score:.3f}" if isinstance(score, (int, float)) else "—")
                    row += [entry.candidate_id, entry.case_name, self._results_validation_text(entry.validation_status)]
                    row += [entry.metrics.get(name) for name in metric_names]
                    rows.append(row)
                page = self.pages["Results"]
                if rows:
                    page.show_rows(headers, rows, header_tooltips)
                else:
                    page.set_empty("No eligible candidates", "Every candidate was excluded by validation, missing values, or a required filter. Review the exclusion reasons below.", "Review Inbox", lambda: self.stack.setCurrentWidget(self.pages["Inbox"]))
                exclusions = list(result.exclusions)
                self._display_ranking_exclusions(exclusions)
                note = f"{len(entries)} included · {len(exclusions)} excluded. "
                if result.mode == "weighted":
                    note += "Weighted score: higher is better (0–1, relative to this study)."
                else:
                    note += "Pareto rank 1 shows the best trade-off cases; several can share rank 1."
                if title == "Rank screened":
                    note = f"{len(entries)} quick estimates · {len(exclusions)} excluded. These are CEA estimates, not RPA4 report results."
                if result.mode == "weighted" and entries:
                    constant = [name for name in metric_names if len({entry.metrics.get(name) for entry in entries}) <= 1]
                    if constant:
                        note += " Same value for every case, so it cannot distinguish designs: " + ", ".join(self._metric_column_label(name) for name in constant) + "."
                review_count = sum(str(entry.validation_status).casefold() == "review_required" for entry in entries)
                if review_count:
                    subject = "case needs" if review_count == 1 else "cases need"
                    note += f" {review_count} {subject} a manual check of the report against its configuration file."
                if len(entries) == 1:
                    note += " Import another RPA4 report to compare designs."
                elif len(entries) >= 2:
                    note += " Select 2–5 rows, then use Compare."
                if entries:
                    note += " Double-click a row for details."
                page.notice.setText(note)
                self._prepare_results_axes(metric_names)
                self._draw_results_plot()
                if entries and title != "Rank screened":
                    self._set_ranking_setup_expanded(False)
                self._update_weighted_button()
            elif title == "Build RPA4 pack" and result is not None:
                self.last_pack_path = Path(result)
                self.open_pack_folder_button.setEnabled(True)
                self.pages["Export"].notice.setText(f"RPA4 pack created at {result}. Copy this ZIP to the computer that runs RPA4.")
            elif title in {"Rescan RPA4 inbox", "Import RPA4 results"}:
                summary = self.service.imported_summary() if self.service else {}
                if hasattr(result, "imported_candidate_ids"):
                    imported = len(result.imported_report_ids or result.imported_candidate_ids)
                    duplicates = len(result.duplicates)
                    unpaired = len(result.unpaired)
                    problems = len(result.problems)
                elif isinstance(result, dict):
                    imported = len(result.get("last_imported_report_ids", result.get("last_imported_candidate_ids", [])))
                    duplicates = len(result.get("duplicates", []))
                    unpaired = len(result.get("unpaired", []))
                    problems = len(result.get("scan_problems", []))
                else:
                    imported = duplicates = unpaired = problems = 0
                stamp = summary.get("last_scanned_at") or "unknown time"
                self.pages["Inbox"].notice.setText(f"Scan {stamp}: {imported} report(s) imported, {duplicates} duplicate(s), {unpaired} unpaired, {problems} problem(s). No PDFs yet is a normal state while the RPA4 computer is running cases.")
                self._refresh_statuses()
            elif title == "Prepare OpenFOAM finalist" and result is not None:
                self.prepared_case = True
                self.prepared_candidate_id = self._selected_finalist_id()
                self.pages["Finalists"].notice.setText(f"Case prepared at {result}. Review its mesh, models, and boundary conditions before running.")
                self._update_finalist_controls()
            elif title == "Run OpenFOAM finalist" and result is not None:
                self.pages["Finalists"].notice.setText("OpenFOAM run completed. CFD results are screening evidence for finalists, not hot-fire validation.")

        def _display_ranking_exclusions(self, exclusions):
            table = self.results_exclusion_table
            table.clearContents()
            table.setRowCount(len(exclusions))
            if not exclusions:
                self.results_exclusions_group.hide()
                return
            self.results_exclusions_group.setTitle(f"{len(exclusions)} candidate(s) excluded · review reasons")
            for row, item in enumerate(exclusions):
                if isinstance(item, dict):
                    candidate_id = str(item.get("candidate_id", "Unknown candidate"))
                    reason = str(item.get("reason", "No exclusion reason was recorded"))
                else:
                    candidate_id = str(getattr(item, "candidate_id", "Unknown candidate"))
                    reason = str(getattr(item, "reason", "No exclusion reason was recorded"))
                for metric_name in sorted(getattr(self, "metric_descriptors", {}), key=len, reverse=True):
                    reason = reason.replace(metric_name, self._metric_label(metric_name))
                reason = reason.replace("missing objective metrics:", "Missing selected metric values:")
                reason = reason.replace("missing constrained metric:", "Missing value for required filter:")
                reason = reason.replace("constraint failed:", "Required filter failed:")
                reason = reason.replace("CFG/PDF validation has a hard mismatch", "CFG and RPA4 report values do not match")
                table.setItem(row, 0, QtWidgets.QTableWidgetItem(candidate_id))
                table.setItem(row, 1, QtWidgets.QTableWidgetItem(reason))
                table.item(row, 1).setToolTip(reason)
            table.setColumnWidth(0, 210)
            self.results_exclusions_group.show()

        def _update_results_compare_button(self, *_):
            button = getattr(self, "results_compare_button", None)
            if not button:
                return
            selected = self.pages["Results"].table.selectionModel().selectedRows()
            headers = self.pages["Results"].data_model.headers
            candidate_header = "Candidate ID" if "Candidate ID" in headers else "Candidate" if "Candidate" in headers else None
            count = len(selected)
            enabled = count in range(2, 6) and candidate_header is not None
            if candidate_header is None:
                self.last_ranking = None
                if self.pages["Results"].plot:
                    self.pages["Results"].plot.clear()
                    self.pages["Results"].plot.hide()
                if getattr(self, "results_axes_group", None):
                    self.results_axes_group.hide()
                if hasattr(self, "results_exclusions_group"):
                    self._display_ranking_exclusions([])
            button.setEnabled(enabled)
            if enabled:
                button.setText(f"Compare {count} selected")
                button.setToolTip("Open a side-by-side comparison of the selected RPA4 cases and their report values.")
            elif count == 1:
                button.setText("Compare (select one more)")
                button.setToolTip("Select one more candidate row. Compare supports two to five cases.")
            elif count > 5:
                button.setText("Compare (select 5 or fewer)")
                button.setToolTip("Clear some rows. Compare supports two to five cases at a time.")
            else:
                button.setText("Compare (2–5)")
                button.setToolTip("Select two to five candidate rows in the table to see their report values side by side.")
            button.setAccessibleName(button.text())

        def compare_selected_results(self):
            page = self.pages["Results"]
            model = page.data_model
            headers = list(model.headers)
            selected_rows = sorted(index.row() for index in page.table.selectionModel().selectedRows())
            candidate_header = "Candidate ID" if "Candidate ID" in headers else "Candidate" if "Candidate" in headers else None
            if len(selected_rows) not in range(2, 6) or candidate_header is None:
                page.notice.setText("Select between two and five ranked candidate rows before comparing.")
                return
            candidate_column = headers.index(candidate_header)
            case_header = "RPA4 case" if "RPA4 case" in headers else "Case" if "Case" in headers else None
            case_column = headers.index(case_header) if case_header else None
            comparison_columns = [
                (column, header) for column, header in enumerate(headers)
                if header not in {"Candidate ID", "Candidate", "RPA4 case", "Case"}
            ]
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("Compare selected candidates")
            window_handle = self.windowHandle()
            screen = window_handle.screen() if window_handle else app.primaryScreen()
            available = screen.availableGeometry() if screen else QtCore.QRect(0, 0, 1200, 800)
            dialog_width = min(1200, max(480, available.width() - 48))
            dialog_height = min(max(360, available.height() - 72), max(360, 300 + len(comparison_columns) * 48))
            dialog.resize(dialog_width, dialog_height)
            dialog.setMinimumSize(min(dialog_width, 700), min(dialog_height, 420))
            layout = QtWidgets.QVBoxLayout(dialog)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(10)
            note = QtWidgets.QLabel(
                "Each column is one RPA4 case. Metric rows show the report values in their stated units; the score is relative to this study. "
                "Bold values are best for a selected goal. Needs review means check the report against its configuration file."
            )
            note.setWordWrap(True)
            note.setObjectName("muted")
            layout.addWidget(note)
            table = QtWidgets.QTableWidget(len(comparison_columns), len(selected_rows) + 1)
            table.setAccessibleName("Side by side candidate metric comparison")
            candidate_labels = []
            for row in selected_rows:
                case_name = model.index(row, case_column).data() if case_column is not None else "RPA4 case"
                rank_column = headers.index("Rank") if "Rank" in headers else None
                candidate_labels.append(f"{case_name}\nRank {model.index(row, rank_column).data()}" if rank_column is not None else case_name)
            table.setHorizontalHeaderLabels(["Metric or check"] + candidate_labels)
            for candidate_index, source_row in enumerate(selected_rows, start=1):
                table.horizontalHeaderItem(candidate_index).setToolTip(
                    f"RPA4 case: {model.index(source_row, case_column).data() if case_column is not None else 'unknown'}\n"
                    f"Candidate ID: {model.index(source_row, candidate_column).data()}"
                )
            table.verticalHeader().setVisible(False)
            table.verticalHeader().setDefaultSectionSize(44)
            table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectItems)
            table.setWordWrap(True)
            table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            objective_directions = {}
            ranking = getattr(self, "last_ranking", None)
            objectives = ranking.objectives if hasattr(ranking, "objectives") else ranking.get("objectives", []) if isinstance(ranking, dict) else []
            for objective in objectives:
                metric = objective.metric if hasattr(objective, "metric") else objective.get("metric")
                direction = objective.direction if hasattr(objective, "direction") else objective.get("direction", "maximize")
                if metric:
                    objective_directions[str(metric)] = str(direction)
            objective_rows = {}
            for source_column, metric in getattr(page, "metric_column_ids", {}).items():
                if metric in objective_directions:
                    comparison_row = next((index for index, (column, _label) in enumerate(comparison_columns) if column == source_column), None)
                    if comparison_row is not None:
                        objective_rows[comparison_row] = objective_directions[metric]
            for row, (source_column, label) in enumerate(comparison_columns):
                label_item = QtWidgets.QTableWidgetItem(label)
                label_item.setToolTip(model.headerData(source_column, QtCore.Qt.Orientation.Horizontal, QtCore.Qt.ItemDataRole.ToolTipRole) or label)
                table.setItem(row, 0, label_item)
                numeric_values = []
                for candidate_index, source_row in enumerate(selected_rows, start=1):
                    value = model.index(source_row, source_column).data()
                    item = QtWidgets.QTableWidgetItem("—" if value is None else str(value))
                    if row in objective_rows:
                        try:
                            numeric_values.append((candidate_index, float(str(value))))
                        except (TypeError, ValueError):
                            pass
                    table.setItem(row, candidate_index, item)
                if numeric_values and row in objective_rows:
                    best_value = (max if objective_rows[row] == "maximize" else min)(value for _column, value in numeric_values)
                    for candidate_index, value in numeric_values:
                        if value == best_value:
                            item = table.item(row, candidate_index)
                            font = item.font()
                            font.setBold(True)
                            item.setFont(font)
                            item.setToolTip("Best value among the selected cases for this chosen goal.")
            table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Interactive)
            table.horizontalHeader().setMinimumSectionSize(120)
            table.setColumnWidth(0, min(420, max(260, int(dialog_width * 0.32))))
            for column in range(1, table.columnCount()):
                table.horizontalHeader().setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.Stretch)
            table.verticalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
            layout.addWidget(table, 1)
            buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(dialog.reject)
            buttons.accepted.connect(dialog.accept)
            layout.addWidget(buttons)
            dialog.exec()

        def _prepare_screen_plot(self, rows):
            if not self.service:
                return
            data = []
            parameter_names = []
            for row in rows:
                try:
                    detail = self.service.candidate_detail(str(row[0]))
                except Exception:
                    continue
                parameters = detail.parameters
                data.append((detail.candidate_id, parameters))
                for name, value in parameters.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and name not in parameter_names:
                        parameter_names.append(name)
            self.screen_plot_rows = data
            if len(parameter_names) < 2:
                self.screen_group.hide()
                self.pages["Screen"].notice.setText("The design space plot needs two numeric parameters. The screening table still shows the available results.")
                return
            for combo in (self.screen_x, self.screen_y):
                current = combo.currentData()
                combo.blockSignals(True)
                combo.clear()
                for name in parameter_names:
                    combo.addItem(name, name)
                if current and combo.findData(current) >= 0:
                    combo.setCurrentIndex(combo.findData(current))
                combo.blockSignals(False)
            if len(parameter_names) > 1 and self.screen_y.currentIndex() == self.screen_x.currentIndex():
                self.screen_y.setCurrentIndex(1)

        def _draw_screen_plot(self, *_):
            if not self.screen_plot or not getattr(self, "screen_plot_rows", None):
                if hasattr(self, "screen_group"):
                    self.screen_group.hide()
                return
            self.screen_group.show()
            x_name, y_name = self.screen_x.currentData(), self.screen_y.currentData()
            if not x_name or not y_name:
                return
            points = []
            for candidate_id, values in self.screen_plot_rows:
                x, y = values.get(x_name), values.get(y_name)
                if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                    points.append({"pos": (float(x), float(y)), "data": candidate_id, "symbol": "o", "brush": "#1F5FBF", "size": 9})
            self.screen_plot.clear()
            self.screen_plot.setLabel("bottom", x_name)
            self.screen_plot.setLabel("left", y_name)
            if points:
                self.screen_plot.addItem(__import__("pyqtgraph").ScatterPlotItem(spots=points))

        def _prepare_results_axes(self, preferred=()):
            if not self.service:
                return
            descriptors = self.service.metric_catalog()
            for combo in (self.result_x, self.result_y):
                current = self._metric_for_widget(combo)
                axis_index = 0 if combo is self.result_x else 1
                target = (
                    preferred[axis_index]
                    if len(preferred) > axis_index
                    else current or (descriptors[min(axis_index, len(descriptors) - 1)].name if descriptors else "")
                )
                self._fill_metric_picker(combo, descriptors, target, allow_empty=False)
            self._draw_results_plot()

        def _update_ranking_setup_summary(self, *_):
            ranking = getattr(self, "last_ranking", None)
            mode = ranking.mode if hasattr(ranking, "mode") else ranking.get("mode") if isinstance(ranking, dict) else None
            objectives = {"Maximize": [], "Minimize": []}
            total_weight = sum(weight.value() for metric, _direction, weight in getattr(self, "objective_rows", []) if self._metric_for_widget(metric))
            for metric, direction, weight in getattr(self, "objective_rows", []):
                name = self._metric_for_widget(metric)
                if not name or (mode == "weighted" and weight.value() <= 0):
                    continue
                label = self._metric_column_label(name).split(" · ", 1)[-1]
                if mode == "weighted":
                    share = weight.value() / total_weight if total_weight else 0.0
                    label += f" ({share:.0%})"
                objectives[direction.currentText().title()].append(label)
            groups = [f"{direction}: {', '.join(names)}" for direction, names in objectives.items() if names]
            if groups:
                summary = "Goals: " + "; ".join(groups) + "."
            else:
                summary = "No ranking goals selected. Choose metrics and whether each should be maximized or minimized."
            filter_count = len(getattr(self, "ranking_constraints", []))
            summary += f" {filter_count} required limit(s) active." if filter_count else " No required limits active."
            self.ranking_setup_summary.setText(summary)

        def _set_ranking_setup_expanded(self, expanded: bool):
            self.ranking_setup_content.setVisible(expanded)
            self.ranking_setup_summary.setVisible(not expanded)
            label = "Hide goal and filter settings" if expanded else "Show goal and filter settings"
            self.ranking_setup_toggle.setText(label)
            self.ranking_setup_toggle.setAccessibleName(label)
            self._update_ranking_setup_summary()

        def _draw_results_plot(self, *_):
            ranking = getattr(self, "last_ranking", None)
            plot = self.pages["Results"].plot
            if not plot or not ranking:
                return
            entries = ranking.entries if hasattr(ranking, "entries") else ranking.get("entries", [])
            plot.setVisible(bool(entries))
            self.results_axes_group.setVisible(bool(entries))
            x_name, y_name = self.result_x.currentData(), self.result_y.currentData()
            if not x_name or not y_name:
                return
            points = []
            for entry in entries:
                metrics = entry.metrics if hasattr(entry, "metrics") else entry.get("metrics", {})
                rank = entry.rank if hasattr(entry, "rank") else entry.get("rank", 999999)
                candidate_id = entry.candidate_id if hasattr(entry, "candidate_id") else entry.get("candidate_id", "")
                x, y = metrics.get(x_name), metrics.get(y_name)
                if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                    points.append({"pos": (float(x), float(y)), "data": candidate_id, "symbol": "o" if rank == 1 else "t", "brush": "#1E7A46" if rank == 1 else "#87909B", "size": 11 if rank == 1 else 8})
            plot.clear()
            plot.setLabel("bottom", self._metric_column_label(x_name))
            plot.setLabel("left", self._metric_column_label(y_name))
            plot.setToolTip(f"X axis: {self._metric_tooltip(x_name)}\n\nY axis: {self._metric_tooltip(y_name)}")
            mode = ranking.mode if hasattr(ranking, "mode") else ranking.get("mode", "pareto")
            self.results_plot_guide.setText(
                "Each point is one included case. Green circles show rank 1; gray triangles show lower ranks. "
                + ("For weighted ranking, a higher score means a better combined result. " if mode == "weighted" else "For Pareto ranking, rank 1 means no candidate is at least as good on every selected goal and better on one. ")
                + "The table gives the RPA4 values, units, and validation status."
            )
            plot.setAccessibleName("Ranked candidate comparison plot")
            plot.setAccessibleDescription(self.results_plot_guide.text())
            if points:
                plot.addItem(__import__("pyqtgraph").ScatterPlotItem(spots=points))
            else:
                self.results_plot_guide.setText("No candidate has values for both selected axes. Choose metrics with values in the table; the table remains the source for exact values and validation status.")
                plot.setVisible(False)

        def _update_weighted_button(self):
            if not hasattr(self, "weighted_button"):
                return
            selected = [(self._metric_for_widget(metric), weight.value()) for metric, _direction, weight in self.objective_rows if self._metric_for_widget(metric)]
            has_objective = bool(selected)
            has_positive_weight = any(weight > 0 for _metric, weight in selected)
            self.pareto_button.setEnabled(has_objective)
            self.pareto_button.setToolTip("Choose at least one metric goal first." if not has_objective else "Pareto uses every selected goal and ignores weights.")
            self.weighted_button.setEnabled(has_objective and has_positive_weight)
            self.weighted_button.setToolTip("Choose at least one metric and give it a positive weight." if not self.weighted_button.isEnabled() else "Calculate a 0–1 weighted score from the selected goals.")
            total = sum(weight for _metric, weight in selected)
            for index, (metric, _direction, weight) in enumerate(self.objective_rows):
                share = weight.value() / total if total and self._metric_for_widget(metric) else 0.0
                self.weight_share_labels[index].setText(f"{share:.1%}")
                self.weight_share_labels[index].setToolTip("Percentage of the total positive weight used by Weighted score. A zero-weight metric is skipped there.")

        def toggle_jobs(self):
            self.job_table.setVisible(not self.job_table.isVisible())
            self.job_strip.setText("Jobs: expanded  ·  click to collapse" if self.job_table.isVisible() else "Jobs: collapsed  ·  click to expand")

        def _refresh_study(self):
            if not self.service:
                return
            spec = self.service.spec
            overview = self.service.study_overview()
            self.sampling_method.setCurrentText(spec.sampling_method)
            self.sample_count.setValue(spec.sample_count)
            self.sample_seed.setValue(spec.seed)
            self.sample_limit.setValue(spec.max_candidates)
            rows = [[p.name, p.unit or "—", p.kind, p.minimum, p.maximum, p.levels or len(p.values), ", ".join(map(str, p.values)) or ("log" if p.log else "linear")] for p in spec.parameters]
            page = self.pages["Study"]
            page.show_rows(["Parameter", "Unit", "Type", "Minimum", "Maximum", "Levels", "Sampling"] , rows)
            self.overview_values["count"].setText(str(overview.proposed_count) if overview.proposed_count is not None else "Blocked")
            self.overview_values["limit"].setText(str(overview.candidate_limit))
            self.overview_values["generated"].setText(str(overview.generated_count))
            self.overview_values["imported"].setText(str(overview.imported_count))
            can_generate = overview.proposed_count is not None
            self.generate_button.setEnabled(can_generate)
            self.generate_button.setToolTip(overview.preview_error or "")
            if overview.preview_error:
                notice = f"Generation is blocked: {overview.preview_error}"
            else:
                notice = f"{overview.proposed_count} candidate(s) proposed by {spec.sampling_method} sampling. RP-1 and O2(L) come from the base CFG."
            if overview.constraint_count:
                notice += f" {overview.constraint_count} feasibility rule(s) will exclude invalid combinations before CFG files are written."
            if any(parameter.name == "nozzle_inlet_area_ratio" for parameter in spec.parameters):
                notice += " Nozzle inlet area-ratio mapping remains under calibration review."
            page.notice.setText(notice)
            if spec.design_constraints:
                rules = []
                for rule in spec.design_constraints:
                    right = rule.get("right", rule.get("value"))
                    rules.append(str(rule.get("name") or f"{rule.get('left')} {rule.get('operator')} {right}"))
                self.feasibility_banner.setText("Hard filters applied before CFG generation: " + "; ".join(rules))
            else:
                self.feasibility_banner.setText("No hard feasibility rules are configured. Every sampled combination remains eligible for CFG generation; add rules for geometric or operating limits.")
            if any(parameter.name == "nozzle_inlet_area_ratio" for parameter in spec.parameters):
                self.feasibility_banner.setText(self.feasibility_banner.text() + " Review flag: the RPA4 nozzle inlet area-ratio mapping is still under calibration.")
            self.screen_button.setEnabled(overview.generated_count > 0)
            self.export_preview_button.setEnabled(overview.generated_count > 0)
            self._refresh_report_library()
            if overview.generated_count == 0:
                self.pages["Screen"].set_empty("No candidates to screen", "Generate candidate CFG files in Study before running analysis models.", "Go to Study", lambda: self.stack.setCurrentWidget(self.pages["Study"]))
                self.pages["Export"].set_empty("No cases to export", "Generate candidate CFG files before preparing an RPA4 handoff pack.", "Go to Study", lambda: self.stack.setCurrentWidget(self.pages["Study"]))
                self.pages["Results"].set_empty("No results to rank", "Import RPA4 reports or screen candidates to create comparison data.", "Go to Inbox", lambda: self.stack.setCurrentWidget(self.pages["Inbox"]))
                self.pages["Finalists"].set_empty("No ranked finalists", "Rank candidates in Results, then refresh the finalist list.", "Go to Results", lambda: self.stack.setCurrentWidget(self.pages["Results"]))
            else:
                self.pack_preview()
                if overview.screened_count:
                    self._populate_screen_from_service()
                else:
                    self.pages["Screen"].set_empty("Ready to screen", "Run CEA and any explicitly configured Cantera model to compare candidate designs.", "Run screening", self.screen)
                ranked = self._populate_saved_ranking()
                if not ranked and overview.imported_count:
                    self.pages["Results"].set_empty("RPA4 reports are ready", "Choose the metrics and goals above, then calculate a weighted score or view Pareto trade-offs.", "Set ranking goals", lambda: self.stack.setCurrentWidget(self.pages["Results"]))
                elif not ranked and not overview.imported_count and not overview.screened_count:
                    self.pages["Results"].set_empty("No results to compare", "Import returned RPA4 reports in Inbox or run fast screening on generated candidates first.", "Go to Inbox", lambda: self.stack.setCurrentWidget(self.pages["Inbox"]))
            if not rows:
                page.set_empty("No design parameters yet", "Add at least one numeric, categorical, or boolean parameter to define a candidate sweep.", "Edit study file", self.open_dialog)

        def _populate_screen_from_service(self):
            if not self.service:
                return
            rows = []
            for status in self.service.candidate_statuses():
                try:
                    detail = self.service.candidate_detail(status.candidate_id)
                except Exception:
                    continue
                outcome = detail.screening
                if not outcome:
                    continue
                cea = outcome.get("cea", {})
                cea_result = cea.get("result", {})
                cantera = outcome.get("cantera", {})
                rows.append([status.candidate_id, cea.get("status"), cea_result.get("ideal_vacuum_specific_impulse_m_s"), cea_result.get("characteristic_velocity_m_s"), cea_result.get("exit_mach"), cantera.get("status")])
            page = self.pages["Screen"]
            page.show_rows(["Candidate", "CEA status", "CEA ideal vacuum Isp (m/s)", "CEA c* (m/s)", "CEA exit Mach", "Cantera status"], rows)
            if rows:
                page.notice.setText(f"{len(rows)} candidate(s) have saved screening results. CEA values are ideal outputs; Cantera is a chamber-state model only.")
                self._prepare_screen_plot(rows)
                self._draw_screen_plot()

        def _populate_saved_ranking(self):
            if not self.service:
                return False
            snapshot = self.service.ranking_snapshot()
            entries = snapshot.get("entries", [])
            exclusions = snapshot.get("exclusions", [])
            if not entries and not exclusions:
                self._display_ranking_exclusions([])
                return False
            objectives = snapshot.get("objectives", [])
            metric_names = [str(item.get("metric", "")) for item in objectives if item.get("metric")]
            mode = str(snapshot.get("mode", "pareto"))
            headers, header_tooltips = self._ranking_table_headers(mode, metric_names)
            rows = []
            for entry in entries:
                row = [entry.get("rank")]
                if mode == "weighted":
                    score = entry.get("weighted_score")
                    row.append(f"{float(score):.3f}" if isinstance(score, (int, float)) else "—")
                row += [entry.get("candidate_id"), entry.get("case_name"), self._results_validation_text(entry.get("validation_status", "unknown"))]
                row += [entry.get("metrics", {}).get(name) for name in metric_names]
                rows.append(row)
            page = self.pages["Results"]
            if rows:
                    page.show_rows(headers, rows, header_tooltips)
            else:
                page.set_empty("No eligible candidates", "Every candidate was excluded by validation, missing values, or a required filter. Review the exclusion reasons below.", "Review Inbox", lambda: self.stack.setCurrentWidget(self.pages["Inbox"]))
            self._display_ranking_exclusions(exclusions)
            if mode == "weighted":
                explanation = "Weighted score: higher is better (0–1, relative to this study)."
            else:
                explanation = "Pareto rank 1 shows the best trade-off cases; several can share rank 1."
            if metric_names and all(name.startswith("screening.") for name in metric_names):
                explanation = "These are quick CEA estimates, not RPA4 report results. " + explanation
            note = f"{len(entries)} included · {len(exclusions)} excluded. {explanation}"
            note += " Import another RPA4 report to compare designs." if len(entries) == 1 else " Select 2–5 rows, then use Compare." if len(entries) >= 2 else " Review the exclusion reasons below."
            note += " Double-click a row for details." if entries else ""
            review_count = sum(str(entry.get("validation_status", "")).casefold() == "review_required" for entry in entries)
            if review_count:
                subject = "case needs" if review_count == 1 else "cases need"
                note += f" {review_count} {subject} a manual check of the report against its configuration file."
            page.notice.setText(note)
            self.last_ranking = snapshot
            self._prepare_results_axes(metric_names)
            self._draw_results_plot()
            if entries:
                self._set_ranking_setup_expanded(False)
            return True

        def _refresh_statuses(self):
            if not self.service:
                return
            self._refresh_metric_choices()
            rows = self.service.candidate_statuses()
            inbox_rows = [[row.candidate_id, row.case_name, self._status_text(row.status.value), self._validation_text(row.validation_status), row.sent_at or "", row.reason] for row in rows]
            summary = self.service.imported_summary()
            for kind, entries in (("unpaired", summary.get("unpaired", [])), ("duplicate", summary.get("duplicates", [])), ("failed", summary.get("scan_problems", []))):
                for position, item in enumerate(entries):
                    reason = str(item.get("reason", ""))
                    if kind == "failed" and "no rpa4 pdf reports found yet" in reason.casefold():
                        continue
                    inbox_rows.append([f"{kind}-{position + 1}", item.get("case_name") or "Unmatched report", self._status_text(kind), "", item.get("path", ""), reason])
            self.pages["Inbox"].show_rows(["Candidate", "RPA4 case", "Status", "Validation", "Sent at", "Reason"], inbox_rows)
            self.pages["Candidate"].show_rows(["Candidate", "Case", "Status", "Validation", "Flags / reason"], [[row.candidate_id, row.case_name, self._status_text(row.status.value), self._validation_text(row.validation_status), row.reason] for row in rows])
            unresolved = any(row.status.value in {"review", "failed", "stale"} or row.validation_status in {"mismatch", "review_required"} for row in rows) or bool(summary.get("unpaired")) or any("no rpa4 pdf reports found yet" not in str(item.get("reason", "")).casefold() for item in summary.get("scan_problems", []))
            if not self._attention_filter_user_changed:
                self.attention_filter.setChecked(unresolved)
            self._set_attention_filter(self.attention_filter.isChecked())
            if not rows:
                self.pages["Inbox"].set_empty("No candidates are waiting for RPA4", "Generate candidates, build a pack, and run those CFGs on the RPA4 computer. Returned PDFs can then be imported here.", "Go to Study", lambda: self.stack.setCurrentWidget(self.pages["Study"]))
                self.pages["Candidate"].set_empty("Select a candidate to inspect", "Candidate details appear here after you generate a sweep or open a study with staged cases.", "Go to Study", lambda: self.stack.setCurrentWidget(self.pages["Study"]))
            self._update_inbox_summary()

        @staticmethod
        def _validation_text(status):
            value = str(status or "unknown").casefold()
            return {
                "match": "Match",
                "mismatch": "Mismatch",
                "review_required": "Review required",
                "unknown": "Not checked",
            }.get(value, value.replace("_", " ").title())

        @staticmethod
        def _status_text(status):
            return {
                "pending": "Pending", "sent": "Sent", "received": "Received", "parsed": "Parsed",
                "validated": "OK - Validated", "review": "! - Review required", "failed": "X - Failed",
                "duplicate": "Duplicate - already seen", "unpaired": "! - Unpaired", "stale": "! - Stale",
            }.get(str(status), str(status).replace("_", " ").title())

        def _set_attention_filter(self, enabled):
            if hasattr(self, "pages") and "Inbox" in self.pages:
                page = self.pages["Inbox"]
                page.data_model.setAttentionOnly(enabled)
                page._sync_filter_state()

        def _update_inbox_summary(self):
            if not self.service:
                return
            statuses = self.service.candidate_statuses()
            summary = self.service.imported_summary()
            stamp = summary.get("last_scanned_at") or "Not scanned yet"
            attention = sum(row.status.value in {"review", "failed", "stale"} or row.validation_status in {"mismatch", "review_required"} for row in statuses)
            attention += len(summary.get("unpaired", []))
            attention += sum("no rpa4 pdf reports found yet" not in str(item.get("reason", "")).casefold() for item in summary.get("scan_problems", []))
            self.pages["Inbox"].notice.setText(f"{len(statuses)} staged cases - {summary.get('result_count', 0)} reports imported - {attention} item(s) need review - last scan: {stamp}.")
        def _open_selected_candidate(self, index, source="Inbox"):
            if not self.service or not index.isValid():
                return
            if source == "Results":
                headers = self.pages["Results"].data_model.headers
                column = headers.index("Candidate ID") if "Candidate ID" in headers else headers.index("Candidate") if "Candidate" in headers else -1
                if column < 0:
                    return
            else:
                column = 0
            candidate_id = str(index.siblingAtColumn(column).data() or "")
            if not candidate_id:
                return
            self.stack.setCurrentWidget(self.pages["Candidate"])
            candidate_page = self.pages["Candidate"]
            for row in range(candidate_page.table.model().rowCount()):
                if str(candidate_page.table.model().index(row, 0).data()) == candidate_id:
                    candidate_page.table.selectRow(row)
                    self._show_candidate_detail(candidate_page.table.model().index(row, 0))
                    return
            self._show_candidate_detail_for_id(candidate_id)

        def _format_detail(self, value):
            lines = []
            def visit(item, depth=0):
                prefix = "  " * depth
                if isinstance(item, dict):
                    for key, child in item.items():
                        if isinstance(child, (dict, list)):
                            lines.append(f"{prefix}{key}")
                            visit(child, depth + 1)
                        else:
                            rendered = "—" if child is None else str(child)
                            lines.append(f"{prefix}{key}: {rendered}")
                elif isinstance(item, (list, tuple)):
                    for index, child in enumerate(item):
                        if isinstance(child, (dict, list)):
                            lines.append(f"{prefix}[{index}]")
                            visit(child, depth + 1)
                        else:
                            lines.append(f"{prefix}[{index}]: {child}")
                else:
                    lines.append(f"{prefix}{item}")
            visit(value)
            return "\n".join(lines) if lines else "No data is available for this section yet."

        def _show_candidate_detail(self, index):
            if not index.isValid():
                return
            candidate_id = str(index.siblingAtColumn(0).data() or "")
            self._show_candidate_detail_for_id(candidate_id)

        def _show_candidate_detail_for_id(self, candidate_id: str):
            service = self.service
            if not service or not candidate_id:
                return
            try:
                detail = service.candidate_detail(candidate_id)
            except Exception as error:
                self.pages["Candidate"].notice.setText(f"Could not load candidate details: {error}")
                return
            self.pages["Candidate"].notice.setText(f"{detail.case_name} - {detail.candidate_id} - status: {detail.status}")
            sections = {
                "Inputs": {"Design parameters": detail.parameters, "Units": detail.parameter_units, "RPA4 CFG": detail.cfg},
                "Report": {"Metric tiers": "ideal = theoretical; estimated_delivered = RPA4 estimate; derived = calculated from report values", **detail.report_values},
                "CFG vs PDF": {"Validation status": detail.validation.get("status"), "Checks (nozzle inlet area ratio is listed first)": detail.validation.get("checks", []), "Returned CFG audit": detail.validation.get("returned_cfg_audit")},
                "Screening": detail.screening,
                "Provenance": detail.provenance,
            }
            for name, value in sections.items():
                self.candidate_detail_views[name].setPlainText(self._format_detail(value))
        @staticmethod
        def _selected_candidate_id(page):
            index = page.table.currentIndex()
            if not index.isValid():
                return None
            value = index.siblingAtColumn(0).data()
            return str(value) if value else None

        def edit_ranges(self):
            service = self._require_service()
            if not service:
                return
            path = service.study_path
            payload = service.study_payload()
            parameters = payload.get("parameters", {})
            editable = list(parameters.items())
            if not editable:
                QtWidgets.QMessageBox.information(self, "No design parameters", "This study has no design parameters to edit.")
                return
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("Edit design parameters")
            dialog.resize(1050, 520)
            layout = QtWidgets.QVBoxLayout(dialog)
            table = QtWidgets.QTableWidget(len(editable), 10)
            table.setHorizontalHeaderLabels(["Parameter", "Kind", "Unit", "Minimum", "Maximum", "Levels", "Category values (JSON)", "Boolean values", "Precision", "Log scale"])
            table.setAccessibleName("Design parameter ranges and categorical levels")
            boolean_options = [("Both", [False, True]), ("False only", [False]), ("True only", [True])]
            for row, (name, spec) in enumerate(editable):
                kind = str(spec.get("kind", "float"))
                raw_values = spec.get("values", [False, True] if kind == "boolean" else [])
                cells = {
                    0: name,
                    1: kind,
                    2: str(spec.get("unit", "—")),
                    3: str(spec.get("min", "")),
                    4: str(spec.get("max", "")),
                    5: str(spec.get("levels", 5 if kind in {"float", "integer"} else "")),
                    6: json.dumps(raw_values, ensure_ascii=False),
                    8: str(spec.get("precision", 8)),
                }
                for column, value in cells.items():
                    table.setItem(row, column, QtWidgets.QTableWidgetItem(str(value)))
                log_item = QtWidgets.QTableWidgetItem("")
                log_item.setFlags(log_item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                log_item.setCheckState(QtCore.Qt.CheckState.Checked if spec.get("log", False) else QtCore.Qt.CheckState.Unchecked)
                table.setItem(row, 9, log_item)
                for column in (0, 1, 2):
                    table.item(row, column).setFlags(table.item(row, column).flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                if kind in {"categorical", "boolean"}:
                    for column in (3, 4, 5, 8, 9):
                        item = table.item(row, column)
                        item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                        if column == 9:
                            item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                    combo = QtWidgets.QComboBox()
                    for label, values in boolean_options:
                        combo.addItem(label, values)
                    if kind == "boolean":
                        current = list(raw_values)
                        match = next((index for index, (_label, values) in enumerate(boolean_options) if current == values), 0)
                        combo.setCurrentIndex(match)
                    else:
                        combo.setEnabled(False)
                    combo.setAccessibleName(f"{name} boolean values")
                    table.setCellWidget(row, 7, combo)
                    if kind == "boolean":
                        table.item(row, 6).setFlags(table.item(row, 6).flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                else:
                    empty = QtWidgets.QTableWidgetItem("—")
                    empty.setFlags(empty.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                    table.setItem(row, 7, empty)
                    table.item(row, 6).setFlags(table.item(row, 6).flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                for column in range(10):
                    item = table.item(row, column)
                    if item and not (item.flags() & QtCore.Qt.ItemFlag.ItemIsEditable):
                        item.setForeground(QtGui.QBrush(QtGui.QColor((DARK if self._theme == "dark" else LIGHT)["muted"])))
            table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(6, QtWidgets.QHeaderView.ResizeMode.Stretch)
            layout.addWidget(table)
            note = QtWidgets.QLabel("Numeric parameters use the displayed bounds and levels. Categorical values use a JSON list; boolean values can use both states or hold true/false. RP-1 and O2(L) remain fixed by the base CFG.")
            note.setWordWrap(True)
            layout.addWidget(note)
            buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
            layout.addWidget(buttons)
            buttons.rejected.connect(dialog.reject)

            def save_ranges():
                try:
                    for row, (name, spec) in enumerate(editable):
                        kind = str(spec.get("kind", "float"))
                        if kind in {"float", "integer"}:
                            minimum = float(table.item(row, 3).text())
                            maximum = float(table.item(row, 4).text())
                            levels_value = float(table.item(row, 5).text())
                            precision_value = float(table.item(row, 8).text())
                            if not levels_value.is_integer():
                                raise ValueError(f"{name}: levels must be a whole number")
                            if not precision_value.is_integer() or not 0 <= precision_value <= 15:
                                raise ValueError(f"{name}: precision must be from 0 to 15")
                            if kind == "integer":
                                if not minimum.is_integer() or not maximum.is_integer():
                                    raise ValueError(f"{name}: integer bounds must be whole numbers")
                                minimum, maximum = int(minimum), int(maximum)
                            spec["min"], spec["max"], spec["levels"] = minimum, maximum, int(levels_value)
                            spec["precision"] = int(precision_value)
                            spec["log"] = table.item(row, 9).checkState() == QtCore.Qt.CheckState.Checked
                        elif kind == "boolean":
                            spec["values"] = table.cellWidget(row, 7).currentData()
                        elif kind == "categorical":
                            choices = json.loads(table.item(row, 6).text())
                            if not isinstance(choices, list) or not choices:
                                raise ValueError(f"{name}: category values must be a non-empty JSON list")
                            spec["values"] = choices
                    service.save_study_payload(payload)
                except Exception as error:
                    QtWidgets.QMessageBox.warning(dialog, "Invalid study ranges", str(error))
                    return
                dialog.accept()
                self.load_study(path)

            buttons.accepted.connect(save_ranges)
            dialog.exec()

        def edit_design_constraints(self):
            service = self._require_service()
            if not service:
                return
            path = service.study_path
            payload = service.study_payload()
            parameters = payload.get("parameters", {})
            names = [name for name, item in parameters.items() if item.get("kind", "float") in {"float", "integer"}]
            for rule in payload.get("design_constraints", []):
                for name in (rule.get("left"), rule.get("right")):
                    if name and name not in names:
                        names.append(str(name))
            if not names:
                QtWidgets.QMessageBox.information(self, "No numeric parameters", "Add a numeric design parameter before defining feasibility rules.")
                return
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("Design feasibility rules")
            dialog.resize(780, 520)
            layout = QtWidgets.QVBoxLayout(dialog)
            intro = QtWidgets.QLabel("Rules remove physically invalid combinations before candidate CFG files are created. Compare a parameter with another parameter or a fixed value.")
            intro.setWordWrap(True)
            layout.addWidget(intro)
            rule_list = QtWidgets.QListWidget()
            rule_list.setAccessibleName("Design feasibility rules")
            layout.addWidget(rule_list, 1)
            form = QtWidgets.QGroupBox("Add or edit a rule")
            grid = QtWidgets.QGridLayout(form)
            grid.addWidget(QtWidgets.QLabel("Parameter"), 0, 0)
            left = QtWidgets.QComboBox()
            left.addItems(names)
            left.setAccessibleName("Feasibility rule parameter")
            grid.addWidget(left, 0, 1)
            operator = QtWidgets.QComboBox()
            operator.addItems(["<", "<=", "==", ">=", ">"])
            operator.setAccessibleName("Feasibility rule operator")
            grid.addWidget(operator, 0, 2)
            rhs_mode = QtWidgets.QComboBox()
            rhs_mode.addItems(["another parameter", "fixed value"])
            rhs_mode.setAccessibleName("Feasibility rule target type")
            grid.addWidget(rhs_mode, 0, 3)
            right = QtWidgets.QComboBox()
            right.addItems(names)
            right.setAccessibleName("Feasibility rule comparison parameter")
            grid.addWidget(right, 1, 1, 1, 2)
            value = QtWidgets.QDoubleSpinBox()
            value.setRange(-1e15, 1e15)
            value.setDecimals(8)
            value.setAccessibleName("Feasibility rule comparison value")
            grid.addWidget(value, 1, 3)
            label = QtWidgets.QLineEdit()
            label.setPlaceholderText("Optional rule name")
            label.setAccessibleName("Feasibility rule name")
            grid.addWidget(label, 2, 1, 1, 3)
            layout.addWidget(form)
            action_row = QtWidgets.QHBoxLayout()
            add_rule = QtWidgets.QPushButton("Add rule")
            update_rule = QtWidgets.QPushButton("Update selected rule")
            update_rule.setEnabled(False)
            remove_rule = QtWidgets.QPushButton("Remove selected")
            action_row.addWidget(add_rule)
            action_row.addWidget(update_rule)
            action_row.addWidget(remove_rule)
            action_row.addStretch(1)
            layout.addLayout(action_row)
            rules = [dict(item) for item in payload.get("design_constraints", [])]
            edit_index = {"row": None}

            def rule_text(rule):
                target = str(rule.get("right")) if "right" in rule else str(rule.get("value"))
                expression = f"{rule.get('left')} {rule.get('operator')} {target}"
                return f"{rule.get('name')}: {expression}" if rule.get("name") else expression

            def refresh_rules():
                rule_list.clear()
                for rule in rules:
                    rule_list.addItem(rule_text(rule))

            def set_target_mode():
                use_parameter = rhs_mode.currentIndex() == 0
                right.setVisible(use_parameter)
                value.setVisible(not use_parameter)

            def reset_form():
                edit_index["row"] = None
                add_rule.setEnabled(True)
                update_rule.setEnabled(False)
                label.clear()

            def read_form():
                rule = {"left": left.currentText(), "operator": operator.currentText()}
                if rhs_mode.currentIndex() == 0:
                    rule["right"] = right.currentText()
                else:
                    rule["value"] = value.value()
                if label.text().strip():
                    rule["name"] = label.text().strip()
                return rule

            def add_or_update(update=False):
                rule = read_form()
                if rule.get("right") == rule.get("left"):
                    QtWidgets.QMessageBox.warning(dialog, "Invalid rule", "Choose a different comparison parameter.")
                    return
                if update and edit_index["row"] is not None:
                    rules[edit_index["row"]] = rule
                else:
                    rules.append(rule)
                refresh_rules()
                reset_form()

            def load_selected(row):
                if row < 0 or row >= len(rules):
                    return
                rule = rules[row]
                left.setCurrentText(str(rule.get("left", names[0])))
                operator.setCurrentText(str(rule.get("operator", "<")))
                if "right" in rule:
                    rhs_mode.setCurrentIndex(0)
                    right.setCurrentText(str(rule["right"]))
                else:
                    rhs_mode.setCurrentIndex(1)
                    value.setValue(float(rule.get("value", 0)))
                label.setText(str(rule.get("name", "")))
                edit_index["row"] = row
                add_rule.setEnabled(False)
                update_rule.setEnabled(True)
                set_target_mode()

            def remove_selected():
                row = rule_list.currentRow()
                if row >= 0:
                    rules.pop(row)
                    refresh_rules()
                    reset_form()

            rhs_mode.currentIndexChanged.connect(set_target_mode)
            rule_list.itemDoubleClicked.connect(lambda _item: load_selected(rule_list.currentRow()))
            add_rule.clicked.connect(lambda: add_or_update(False))
            update_rule.clicked.connect(lambda: add_or_update(True))
            remove_rule.clicked.connect(remove_selected)
            refresh_rules()
            set_target_mode()
            buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
            layout.addWidget(buttons)
            buttons.rejected.connect(dialog.reject)

            def save_rules():
                try:
                    payload["design_constraints"] = rules
                    service.save_study_payload(payload)
                except Exception as error:
                    QtWidgets.QMessageBox.warning(dialog, "Invalid feasibility rules", str(error))
                    return
                dialog.accept()
                self.load_study(path)

            buttons.accepted.connect(save_rules)
            dialog.exec()

        def apply_sampling_options(self):
            service = self._require_service()
            if not service:
                return
            path = service.study_path
            payload = service.study_payload()
            sampling = payload.setdefault("sampling", {})
            sampling["method"] = self.sampling_method.currentText()
            sampling["seed"] = self.sample_seed.value()
            sampling["max_candidates"] = self.sample_limit.value()
            sampling["count"] = len(sampling.get("candidates", [])) if sampling["method"] == "explicit" else self.sample_count.value()
            try:
                service.save_study_payload(payload)
            except Exception as error:
                QtWidgets.QMessageBox.warning(self, "Invalid sampling settings", str(error))
                return
            self.load_study(path)

        def edit_explicit_candidates(self):
            service = self._require_service()
            if not service:
                return
            path = service.study_path
            payload = service.study_payload()
            sampling = payload.setdefault("sampling", {})
            parameters = payload.get("parameters", {})
            names = list(parameters)
            candidates = list(sampling.get("candidates", []))
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("Edit explicit candidate values")
            dialog.resize(860, 500)
            layout = QtWidgets.QVBoxLayout(dialog)
            note = QtWidgets.QLabel("Enter one complete candidate per row. Values must match the type and range shown by the study parameters.")
            note.setWordWrap(True)
            layout.addWidget(note)
            table = QtWidgets.QTableWidget(max(1, len(candidates)), len(names))
            table.setHorizontalHeaderLabels(names)
            table.setAccessibleName("Explicit candidate values table")
            for row, candidate in enumerate(candidates):
                for column, name in enumerate(names):
                    cell = QtWidgets.QTableWidgetItem(str(candidate.get(name, "")))
                    table.setItem(row, column, cell)
            table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
            layout.addWidget(table, 1)
            controls = QtWidgets.QHBoxLayout()
            add_row = QtWidgets.QPushButton("Add candidate")
            remove_row = QtWidgets.QPushButton("Remove selected candidate")
            add_row.clicked.connect(lambda: table.insertRow(table.rowCount()))
            remove_row.clicked.connect(lambda: table.removeRow(table.currentRow()) if table.currentRow() >= 0 else None)
            controls.addWidget(add_row)
            controls.addWidget(remove_row)
            controls.addStretch(1)
            layout.addLayout(controls)
            buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
            layout.addWidget(buttons)
            buttons.rejected.connect(dialog.reject)

            def parse_value(name, raw):
                item = parameters[name]
                kind = item.get("kind", "float")
                value_text = raw.strip()
                if kind == "boolean":
                    if value_text.casefold() not in {"true", "false"}:
                        raise ValueError(f"{name} must be true or false")
                    return value_text.casefold() == "true"
                if kind == "integer":
                    return int(value_text)
                if kind == "float":
                    return float(value_text)
                try:
                    parsed = json.loads(value_text)
                except json.JSONDecodeError:
                    parsed = value_text
                allowed = item.get("values", [])
                match = next((candidate for candidate in allowed if candidate == parsed or str(candidate) == value_text), None)
                if match is None:
                    raise ValueError(f"{name} must match one of: {', '.join(map(str, allowed))}")
                return match

            def save_explicit():
                old_payload = service.study_payload()
                try:
                    values = []
                    for row in range(table.rowCount()):
                        candidate = {}
                        for column, name in enumerate(names):
                            cell = table.item(row, column)
                            if cell is None or not cell.text().strip():
                                raise ValueError(f"Candidate row {row + 1} needs a value for {name}")
                            candidate[name] = parse_value(name, cell.text())
                        values.append(candidate)
                    if not values:
                        raise ValueError("Add at least one explicit candidate")
                    sampling["method"] = "explicit"
                    sampling["candidates"] = values
                    sampling["count"] = len(values)
                    service.save_study_payload(payload)
                    service.preview()
                except Exception as error:
                    try:
                        service.save_study_payload(old_payload)
                    except Exception:
                        pass
                    QtWidgets.QMessageBox.warning(dialog, "Invalid explicit candidates", str(error))
                    return
                dialog.accept()
                self.load_study(path)

            buttons.accepted.connect(save_explicit)
            dialog.exec()

        def add_ranking_constraint(self):
            metric = self._metric_for_widget(self.constraint_metric)
            if not metric:
                self.pages["Results"].notice.setText("Choose a filter metric before adding a pass/fail limit.")
                return
            self.ranking_constraints.append({"metric": metric, "operator": self.constraint_operator.currentText(), "value": self.constraint_value.value()})
            self._update_constraint_summary()
            self.filter_toggle.setChecked(True)
            self.pages["Results"].notice.setText("Filter added. Candidates outside this limit will be excluded from the next ranking, with the reason listed below the results.")

        def clear_ranking_constraints(self):
            self.ranking_constraints.clear()
            self._update_constraint_summary()

        def remove_ranking_constraint(self):
            row = self.constraint_summary.currentRow()
            if 0 <= row < len(self.ranking_constraints):
                self.ranking_constraints.pop(row)
                self._update_constraint_summary()

        def _update_constraint_summary(self):
            self.constraint_summary.clear()
            count = len(self.ranking_constraints)
            visible = self.filter_toggle.isChecked()
            label = f"{'Hide' if visible else 'Show'} optional pass/fail filters ({count})"
            self.filter_toggle.setText(label)
            self.filter_toggle.setAccessibleName(label)
            if self.ranking_constraints:
                for item in self.ranking_constraints:
                    bound = item.get("value")
                    suffix = f" {bound:g}" if isinstance(bound, (int, float)) else ""
                    self.constraint_summary.addItem(f"{self._metric_label(str(item['metric']))} {item['operator']}{suffix}")
            else:
                self.constraint_summary.addItem("No active filters")

        def preview(self):
            service = self._require_service()
            if not service:
                return
            try:
                samples = service.preview()
                dialog = QtWidgets.QDialog(self)
                dialog.setWindowTitle("Candidate preview")
                dialog.resize(900, 520)
                layout = QtWidgets.QVBoxLayout(dialog)
                shown = samples[:100]
                note = QtWidgets.QLabel(f"Pure preview of {len(samples)} proposed candidates. Showing {len(shown)} rows. Adaptive preview does not create Optuna trials.")
                note.setWordWrap(True)
                layout.addWidget(note)
                table = QtWidgets.QTableView()
                table.setAlternatingRowColors(True)
                table.setSortingEnabled(True)
                table.setModel(RowsModel(list(samples[0]) if samples else ["Candidate"], [list(row.values()) for row in shown], table))
                table.resizeColumnsToContents()
                layout.addWidget(table, 1)
                buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
                buttons.rejected.connect(dialog.reject)
                buttons.accepted.connect(dialog.accept)
                layout.addWidget(buttons)
                dialog.exec()
            except Exception as error:
                self.pages["Study"].notice.setText(f"Preview blocked: {error}")

        def generate(self):
            self._submit("Generate candidates", lambda service, context: service.generate(context=context), self._refresh_study)

        def screen(self):
            self._submit("Screen candidates", lambda service, context: service.screen(context=context))

        def _selected_screen_ids(self):
            page = self.pages["Screen"]
            if not page.table.model():
                return set()
            return {str(index.siblingAtColumn(0).data()) for index in page.table.selectionModel().selectedRows() if index.siblingAtColumn(0).data()}

        def pack_preview(self):
            service = self._require_service()
            if not service:
                return
            selected = self._selected_screen_ids()
            preview = service.preview_pack(selected or None)
            rows = [[candidate_id, case_name, ", ".join(files), "Ready"] for candidate_id, case_name, files in preview.candidates]
            rows.extend([[candidate_id, "", "", reason] for candidate_id, reason in preview.excluded])
            page = self.pages["Export"]
            if rows:
                page.show_rows(["Candidate", "RPA4 case", "Pack contents", "Readiness"], rows)
                default_zip = service.spec.output_dir / service.spec.study_id / f"{service.spec.study_id}-rpa4-pack.zip"
                default_folder = service.spec.output_dir / service.spec.study_id / f"{service.spec.study_id}-rpa4-pack"
                page.notice.setText(f"{len(preview.candidates)} case(s) ready; {len(preview.excluded)} excluded. Select rows in Screen to create a smaller pack, or leave none selected to include every ready case. Default destinations: ZIP {default_zip}; folder {default_folder}.")
            else:
                page.set_empty("No packable cases", "Generate candidate CFG files first. Feasibility failures and missing CFG files will appear here with reasons.", "Go to Study", lambda: self.stack.setCurrentWidget(self.pages["Study"]))

        def pack(self, *, folder: bool = False):
            service = self._require_service()
            if not service:
                return
            selected = self._selected_screen_ids()
            preview = service.preview_pack(selected or None)
            candidate_ids = selected or {candidate_id for candidate_id, _case_name, _files in preview.candidates}
            if not candidate_ids:
                self.pages["Export"].notice.setText("No packable candidate CFGs are available. Review the readiness column in Preview pack.")
                return
            if folder:
                default = service.spec.output_dir / service.spec.study_id / f"{service.spec.study_id}-rpa4-pack"
                output, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Build RPA4 folder pack", str(default), "Folder pack destination (no .zip extension)")
                if output and Path(output).suffix.casefold() == ".zip":
                    QtWidgets.QMessageBox.warning(self, "Choose a folder name", "Folder packs use a destination name without the .zip extension.")
                    return
            else:
                default = service.spec.output_dir / service.spec.study_id / f"{service.spec.study_id}-rpa4-pack.zip"
                output, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Build RPA4 zip pack", str(default), "Zip bundle (*.zip)")
            if output:
                if not folder and Path(output).suffix.casefold() != ".zip":
                    output += ".zip"
                self._submit("Build RPA4 pack", lambda current, context: current.pack(output, candidate_ids, context=context), self._refresh_statuses)

        def open_pack_folder(self):
            path = getattr(self, "last_pack_path", None)
            if path:
                target = Path(path)
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(target if target.is_dir() else target.parent)))

        def import_dialog(self):
            paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Import RPA4 return files", str(Path.home()), "RPA4 returns (*.zip *.pdf *.cfg);;All files (*)")
            if not paths:
                folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose return folder", str(Path.home()))
                paths = [folder] if folder else []
            if paths:
                self._preview_import_source(paths[0] if len(paths) == 1 else paths)

        def _preview_import_source(self, path: str | Path | list[str]):
            service = self._require_service()
            if not service:
                return
            try:
                preview = service.preview_return_source(path)
            except Exception as error:
                self.pages["Inbox"].notice.setText(f"Could not inspect this selection: {error}")
                return
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("Review RPA4 return selection")
            dialog.resize(720, 500)
            layout = QtWidgets.QVBoxLayout(dialog)
            layout.addWidget(QtWidgets.QLabel(f"Source: {preview.source}"))
            layout.addWidget(QtWidgets.QLabel(f"PDF reports: {len(preview.pdf_files)}    CFG files: {len(preview.cfg_files)}    Case folders: {len(preview.case_names)}    Run sheet: {'yes' if preview.run_sheet_found else 'not found'}"))
            files = QtWidgets.QListWidget()
            files.setAccessibleName("Recognized RPA4 return files")
            for name in preview.pdf_files:
                files.addItem(f"PDF   {name}")
            for name in preview.cfg_files:
                files.addItem(f"CFG   {name}")
            if not preview.pdf_files and not preview.cfg_files:
                files.addItem("No RPA4 return files were recognized.")
            layout.addWidget(files, 1)
            if preview.warnings:
                warning = QtWidgets.QLabel("\n".join(preview.warnings))
                warning.setWordWrap(True)
                warning.setStyleSheet("color: #8A5A00;")
                layout.addWidget(warning)
            buttons = QtWidgets.QDialogButtonBox()
            import_button = buttons.addButton("Import recognized PDFs", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
            import_button.setEnabled(bool(preview.pdf_files))
            cancel_button = buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
            cancel_button.clicked.connect(dialog.reject)
            layout.addWidget(buttons)

            def start_import():
                dialog.accept()
                self._submit("Import RPA4 results", lambda current, context: current.import_results(path, context=context), self._refresh_statuses)

            import_button.clicked.connect(start_import)
            dialog.exec()

        def rescan(self):
            service = self._require_service()
            if not service:
                return
            self._submit("Rescan RPA4 inbox", lambda current, context: current.scan(context=context), self._refresh_statuses)

        def toggle_stale(self):
            service = self._require_service()
            if not service:
                return
            candidate_id = self._selected_candidate_id(self.pages["Inbox"])
            if not candidate_id:
                self.pages["Inbox"].notice.setText("Select an inbox row first.")
                return
            candidate = next((row for row in service.candidate_statuses() if row.candidate_id == candidate_id), None)
            if not candidate:
                self.pages["Inbox"].notice.setText("Only staged candidate rows can be marked stale; this scan record is not a candidate.")
                return
            service.mark_stale(candidate.candidate_id, stale=candidate.status.value != "stale")
            self._refresh_statuses()

        def rank(self, mode: str, *, screened: bool = False):
            service = self._require_service()
            if not service:
                return
            if screened:
                self.pages["Results"].notice.setText("Ranking fast CEA screen estimates (ideal vacuum Isp and c*) only. These values are not RPA4 delivered performance.")
                self._submit("Rank screened", lambda current, context: current.rank(None, screened=True))
                return
            ranking_spec = dict(service.spec.ranking)
            ranking_spec["mode"] = mode
            selected = []
            weights = {}
            for metric, direction, weight in self.objective_rows:
                name = self._metric_for_widget(metric)
                if not name:
                    continue
                if mode == "weighted" and weight.value() <= 0:
                    continue
                selected.append({"metric": name, "direction": direction.currentText()})
                weights[name] = weight.value()
            metric_names = [str(item["metric"]) for item in selected]
            if len(set(metric_names)) != len(metric_names):
                self.pages["Results"].notice.setText("Each metric can appear only once. Remove duplicate goals before ranking.")
                return
            if selected:
                ranking_spec["objectives"] = selected
            ranking_spec["constraints"] = list(self.ranking_constraints)
            if mode == "weighted":
                ranking_spec["weights"] = weights
                if sum(weights.values()) <= 0:
                    self.pages["Results"].notice.setText("Give at least one selected goal a positive weight before calculating a weighted score.")
                    return
            if not selected:
                self.pages["Results"].notice.setText("Choose at least one metric goal before ranking. Pareto uses every selected goal; Weighted score uses goals with positive weights.")
                return
            self._submit(f"Rank ({mode})", lambda current, context: current.rank(ranking_spec, screened=screened))
            if mode == "weighted":
                self.pages["Results"].notice.setText("Weighted ranking started. The 0–1 score is relative to eligible candidates in this study; raw values remain in the metric columns.")
            else:
                self.pages["Results"].notice.setText("Pareto ranking started. Rank 1 candidates are not beaten on every selected goal; several designs can share rank 1.")

        def open_selected_report(self):
            service = self._require_service()
            if not service:
                return
            candidate_id = self._selected_candidate_id(self.pages["Candidate"])
            if not candidate_id:
                self.pages["Candidate"].notice.setText("Select a candidate row first.")
                return
            report = service.archived_report_path(candidate_id)
            if report:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(report)))
            else:
                self.pages["Candidate"].notice.setText("No archived report is available for this candidate.")

        def finalists(self):
            service = self._require_service()
            if not service:
                return
            ranking = service.ranking_snapshot()
            entries = sorted(ranking.get("entries", []), key=lambda item: (item.get("rank", 999999), item.get("candidate_id", "")))[:self.finalist_limit.value()]
            self.finalist_ids = [str(row["candidate_id"]) for row in entries]
            page = self.pages["Finalists"]
            if not entries:
                page.set_empty("No ranked finalists", "Run Pareto or weighted ranking in Results, then refresh this list.", "Go to Results", lambda: self.stack.setCurrentWidget(self.pages["Results"]))
                self._update_finalist_controls()
                return
            page.show_rows(["Rank", "Candidate", "RPA4 case", "Validation"], [[row["rank"], row["candidate_id"], row["case_name"], self._validation_text(row["validation_status"])] for row in entries])
            page.notice.setText(f"Showing the top {len(entries)} ranked candidate(s). CFD is screening evidence; it does not replace hot-fire validation.")
            page.table.selectRow(0)
            self.prepared_case = False
            self.prepared_candidate_id = None
            self._update_finalist_controls()
        def _selected_finalist_id(self) -> str | None:
            index = self.pages["Finalists"].table.currentIndex()
            if not index.isValid():
                return None
            value = index.siblingAtColumn(1).data()
            return str(value) if value else None

        def _finalist_paths_changed(self, *_):
            self.prepared_case = False
            self.prepared_candidate_id = None
            self._update_finalist_controls()

        def _finalist_row_changed(self, *_):
            if hasattr(self, "finalist_compare_group"):
                self.finalist_compare_group.hide()
            self.prepared_case = bool(self.prepared_case and self._selected_finalist_id() == self.prepared_candidate_id)
            self._update_finalist_controls()

        def _update_finalist_controls(self, *_):
            if not hasattr(self, "prepare_finalist_button"):
                return
            template_ready = Path(self.finalist_template.text()).is_dir()
            destination_ready = bool(self.finalist_destination.text().strip())
            selected = self._selected_finalist_id()
            self.prepare_finalist_button.setEnabled(bool(template_ready and destination_ready and selected))
            self.run_finalist_button.setEnabled(bool(self.openfoam_available and template_ready and destination_ready and selected and self.prepared_case and selected == self.prepared_candidate_id))
            self.prepare_finalist_button.setToolTip("Select a ranked finalist, choose a case template, and set a WSL destination." if not self.prepare_finalist_button.isEnabled() else "Prepare an isolated OpenFOAM case from this finalist.")
            self.run_finalist_button.setToolTip("Run Doctor, prepare the selected finalist, and confirm OpenFOAM is available in WSL." if not self.run_finalist_button.isEnabled() else "Run the prepared finalist case in WSL.")
            if not self.openfoam_available:
                self.finalist_readiness.setText("OpenFOAM is not confirmed in WSL yet. Run Doctor to see the specific missing setup step.")
            elif not selected:
                self.finalist_readiness.setText("OpenFOAM is available. Rank candidates and select a finalist to prepare a case.")
            elif self.prepared_case and self.prepared_candidate_id == selected:
                self.finalist_readiness.setText(f"Case prepared for {selected}. Review its mesh, models, and boundary conditions before running.")
            else:
                self.finalist_readiness.setText("OpenFOAM is available. Select a reviewed template and WSL destination, then prepare the case.")

        def browse_finalist_template(self):
            path = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose OpenFOAM template", str(Path.home()))
            if path:
                self.finalist_template.setText(path)

        def prepare_finalist(self):
            service = self._require_service()
            candidate_id = self._selected_finalist_id()
            if not service or not candidate_id:
                self.pages["Finalists"].notice.setText("Rank candidates and select a finalist row first.")
                return
            template = self.finalist_template.text().strip()
            destination = self.finalist_destination.text().strip()
            self._submit("Prepare OpenFOAM finalist", lambda current, context: current.prepare_finalist_case(candidate_id, template, destination, limit=self.finalist_limit.value(), context=context))

        def run_finalist(self):
            service = self._require_service()
            candidate_id = self._selected_finalist_id()
            if not service or not candidate_id:
                self.pages["Finalists"].notice.setText("Select a finalist row first.")
                return
            if not self.openfoam_available or not self.prepared_case or candidate_id != self.prepared_candidate_id:
                self.pages["Finalists"].notice.setText("Run Doctor and prepare the WSL case before launching it.")
                return
            destination = self.finalist_destination.text().strip()
            self._submit("Run OpenFOAM finalist", lambda current, context: current.run_finalist_case(candidate_id, destination, limit=self.finalist_limit.value(), context=context))

        def compare_finalist_mach(self):
            service = self._require_service()
            candidate_id = self._selected_finalist_id()
            if not service or not candidate_id:
                self.pages["Finalists"].notice.setText("Select a ranked finalist first.")
                return
            csv_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Choose CFD station Mach CSV", str(Path.home()), "CSV (*.csv)")
            if not csv_path:
                return
            try:
                from rocket_analysis.cfd import compare_station_mach, read_station_csv
                from rocket_analysis.rpa4 import mach_stations_from_report_values
                detail = service.candidate_detail(candidate_id)
                if not detail.report_values:
                    raise ValueError("Import this finalist's RPA4 report before comparing station Mach values.")
                reference = mach_stations_from_report_values(detail.report_values)
                cfd = read_station_csv(csv_path)
                comparisons = compare_station_mach(reference, cfd)
            except Exception as error:
                self.pages["Finalists"].notice.setText(f"Mach comparison failed: {error}")
                return
            rows = [[row.name, row.reference_mach, row.result_mach, row.delta_mach] for row in comparisons]
            self.finalist_compare_table.setModel(RowsModel(["Station", "RPA4 Mach", "CFD Mach", "Delta Mach"], rows, self.finalist_compare_table))
            self.finalist_compare_table.resizeColumnsToContents()
            self.finalist_compare_group.show()
            self.pages["Finalists"].notice.setText("Station Mach is compared with values printed in the RPA4 PDF. CFD remains screening evidence, not hot-fire validation.")
        def browse_shared_folder(self):
            path = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose RPA4 return folder", self.doctor_shared_path.text() or str(Path.home()))
            if path:
                self.doctor_shared_path.setText(path)

        def doctor(self):
            shared = self.doctor_shared_path.text().strip() or __import__("os").environ.get("RPA4_SHARED_DIR")
            if shared:
                settings.setValue("shared_rpa4_folder", shared)
            mechanism = None
            if self.service:
                cantera_options = self.service.spec.analysis.get("cantera", {})
                if isinstance(cantera_options, dict):
                    mechanism = cantera_options.get("mechanism")
            def execute(context):
                return run_doctor(shared_dir=shared, cantera_mechanism=mechanism)
            if self.service:
                self._submit("Run Doctor checks", lambda service, context: execute(context))
            else:
                checks = run_doctor(shared_dir=shared, cantera_mechanism=mechanism)
                self._show_doctor(checks)

        def _show_doctor(self, checks):
            normalized = []
            for row in checks:
                state = str(row["state"])
                marker = {"ok": "OK", "warn": "Review", "missing": "Missing"}.get(state, state.title())
                normalized.append([row["name"], marker, row["detail"], row["fix"] or "No action needed."])
            self.pages["Doctor"].show_rows(["Check", "State", "Detail", "Suggested fix"], normalized)
            ok = sum(row["state"] == "ok" for row in checks)
            needs_attention = len(checks) - ok
            self.pages["Doctor"].notice.setText(f"{ok} check(s) ready; {needs_attention} item(s) need attention. Suggested fixes are informational and are never run automatically.")
            self.openfoam_available = any(row["name"] == "OpenFOAM" and row["state"] == "ok" for row in checks)
            self._update_finalist_controls()

        def open_settings(self):
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("Rocket Study settings")
            dialog.resize(700, 300)
            layout = QtWidgets.QFormLayout(dialog)
            theme = QtWidgets.QComboBox()
            theme.addItems(["Light", "Dark"])
            theme.setCurrentText(self._theme.title())
            theme.setAccessibleName("Application color theme")
            density = QtWidgets.QComboBox()
            density.addItem("Compact - 24 px", 24)
            density.addItem("Comfortable - 28 px", 28)
            density.addItem("Spacious - 32 px", 32)
            density.setCurrentIndex(max(0, density.findData(self.table_density)))
            density.setAccessibleName("Table row density")
            archive_row = QtWidgets.QWidget()
            archive_layout = QtWidgets.QHBoxLayout(archive_row)
            archive_layout.setContentsMargins(0, 0, 0, 0)
            archive_path = QtWidgets.QLineEdit(str(self.archive_root))
            archive_path.setAccessibleName("Local RPA4 report archive folder")
            archive_path.setPlaceholderText(str(default_report_archive_root()))
            browse_archive = QtWidgets.QPushButton("Browse…")
            def choose_archive():
                selected = QtWidgets.QFileDialog.getExistingDirectory(dialog, "Choose local report archive", archive_path.text() or str(default_report_archive_root()))
                if selected:
                    archive_path.setText(selected)
            browse_archive.clicked.connect(choose_archive)
            archive_layout.addWidget(archive_path, 1)
            archive_layout.addWidget(browse_archive)
            layout.addRow("Theme", theme)
            layout.addRow("Table row height", density)
            layout.addRow("Local report archive", archive_row)
            layout.addRow("RPA4 folder", QtWidgets.QLabel(self.doctor_shared_path.text() or "Set on the Doctor screen"))
            buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
            layout.addRow(buttons)
            buttons.rejected.connect(dialog.reject)

            def save():
                new_archive_root = Path(archive_path.text()).expanduser().resolve()
                if new_archive_root.exists() and not new_archive_root.is_dir():
                    QtWidgets.QMessageBox.warning(dialog, "Invalid report archive", "Choose a folder for the local report archive.")
                    return
                if new_archive_root != self.archive_root and self.service:
                    try:
                        self.service.set_report_archive_root(new_archive_root)
                    except Exception as error:
                        QtWidgets.QMessageBox.critical(dialog, "Could not move report archive", str(error))
                        return
                self._theme = theme.currentText().casefold()
                self.table_density = int(density.currentData())
                if new_archive_root != self.archive_root:
                    self.archive_root = new_archive_root
                    settings.setValue("report_archive_root", str(new_archive_root))
                settings.setValue("theme", self._theme)
                settings.setValue("row_height", self.table_density)
                self.setStyleSheet(_style(DARK if self._theme == "dark" else LIGHT))
                for page in self.pages.values():
                    page.table.verticalHeader().setDefaultSectionSize(self.table_density)
                self.job_table.verticalHeader().setDefaultSectionSize(self.table_density)
                if hasattr(self, "finalist_compare_table"):
                    self.finalist_compare_table.verticalHeader().setDefaultSectionSize(self.table_density)
                self._refresh_report_library()
                dialog.accept()

            buttons.accepted.connect(save)
            dialog.exec()

        def toggle_theme(self):
            self._theme = "dark" if self._theme == "light" else "light"
            settings.setValue("theme", self._theme)
            self.setStyleSheet(_style(DARK if self._theme == "dark" else LIGHT))

        def closeEvent(self, event):
            active = [job for job in self.service.jobs.jobs() if job.state.value in {"queued", "running"}] if self.service else []
            if active:
                box = QtWidgets.QMessageBox(self)
                box.setWindowTitle("Analysis jobs are running")
                box.setText("Wait for the current jobs, request cancellation and close after they stop, or keep the study open.")
                wait_button = box.addButton("Wait for jobs", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
                cancel_button = box.addButton("Cancel jobs and close", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
                keep_button = box.addButton("Keep working", QtWidgets.QMessageBox.ButtonRole.RejectRole)
                box.exec()
                if box.clickedButton() == keep_button:
                    event.ignore()
                    return
                if box.clickedButton() == cancel_button:
                    for job in active:
                        self.service.jobs.cancel(job.job_id)
                self.service.close(wait=True)
                self.service = None
            settings.setValue("width", self.width())
            settings.setValue("height", self.height())
            settings.setValue("x", self.x())
            settings.setValue("y", self.y())
            if self.service:
                self.service.close(wait=True)
            event.accept()

        def resizeEvent(self, event):
            if hasattr(self, "rail"):
                logical_width = event.size().width()
                collapsed = logical_width < 850
                compact = logical_width < 720
                self._set_overview_columns(2 if logical_width < 900 else 4)
                self.rail.setFixedWidth(56 if collapsed else 200)
                self.brand_label.setVisible(not collapsed)
                self.study_label.setVisible(not collapsed)
                for button, name in self.nav_buttons:
                    button.setText(name[:2].upper() if collapsed else name)
                    button.setToolTip(f"Open the {name} screen")
                    button.setStyleSheet("text-align: center; padding: 2px 2px;" if collapsed else "text-align: left; padding-left: 12px;")
                self.open_button.setText("Open" if collapsed else "Open study...")
                self.new_button.setText("New" if collapsed else "New from CFG...")
                self.theme_button.setText("Th" if collapsed else "Toggle theme")
                self.settings_button.setText("Se" if collapsed else "Settings...")
                if hasattr(self, "study_preview_button"):
                    self.study_preview_button.setVisible(not compact)
                self.open_button.setToolTip("Open study file")
                self.new_button.setToolTip("Create a study from an RPA4 CFG")
                self.theme_button.setToolTip("Toggle light or dark theme")
                self.settings_button.setToolTip("Open application settings")
            super().resizeEvent(event)

    window = MainWindow(study_path)
    window.setStyleSheet(_style(DARK if window._theme == "dark" else LIGHT))
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

