"""Local, durable archive and ranking helpers for imported RPA4 reports."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Protocol


def default_report_archive_root() -> Path:
    """Return the per-user archive directory, honoring the standard Windows location."""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "RocketAnalysis" / "ReportArchive"
    return Path.home() / ".rocket-analysis" / "report-archive"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def stable_report_id(study_path: str, candidate_id: str, digest: str) -> str:
    material = f"{study_path}\0{candidate_id}\0{digest}".encode("utf-8")
    return "rpt-" + hashlib.sha256(material).hexdigest()[:20]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _recentness(row: dict[str, Any]) -> float:
    value = str(row.get("imported_at", ""))
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return 0.0


class ReportArchiveBackend(Protocol):
    """Storage contract for a future server-backed report archive provider."""

    def relocate(self, root: str | Path) -> "ReportArchiveBackend": ...
    def sync_study(self, study_path: str | Path, study_id: str, result_path: str | Path) -> int: ...
    def list_reports(self, *, study_path: str | Path | None = None, limit: int | None = None) -> list[dict[str, Any]]: ...
    def get_report(self, report_id: str) -> dict[str, Any] | None: ...
    def save_profile(self, profile: dict[str, Any]) -> None: ...
    def load_profile(self) -> dict[str, Any] | None: ...


class ReportLibrary:
    """SQLite-backed cross-study index with content-verified local PDF copies."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or default_report_archive_root()).expanduser().resolve()
        self.database_path = self.root / "library.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS reports (
                report_id TEXT PRIMARY KEY,
                study_path TEXT NOT NULL,
                study_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                case_name TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                report_sha256 TEXT NOT NULL,
                validation_status TEXT NOT NULL,
                report_path TEXT,
                metrics_json TEXT NOT NULL,
                report_values_json TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                parameter_units_json TEXT NOT NULL,
                validation_json TEXT NOT NULL,
                provenance_json TEXT NOT NULL
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS reports_recent ON reports(imported_at DESC, report_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS reports_study_recent ON reports(study_path, imported_at DESC)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)"
        )
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def relocate(self, root: str | Path) -> "ReportLibrary":
        destination = Path(root).expanduser().resolve()
        if destination == self.root:
            return self
        if destination.is_relative_to(self.root) or self.root.is_relative_to(destination):
            raise ValueError("Choose a report archive folder that does not contain or sit inside the current archive")
        destination.mkdir(parents=True, exist_ok=True)
        if self.root.is_dir():
            shutil.copytree(self.root, destination, dirs_exist_ok=True)
        relocated = ReportLibrary(destination)
        with relocated._connection() as connection:
            rows = connection.execute("SELECT report_id FROM reports").fetchall()
            for row in rows:
                path = destination / "reports" / f"{row['report_id']}.pdf"
                if path.is_file():
                    connection.execute("UPDATE reports SET report_path = ? WHERE report_id = ?", (str(path), row["report_id"]))
        return relocated

    def sync_study(self, study_path: str | Path, study_id: str, result_path: str | Path) -> int:
        """Index current and historical rows, migrating legacy single-report rows."""
        result_file = Path(result_path)
        if not result_file.is_file():
            return 0
        try:
            payload = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        source_rows = list(payload.get("report_history", [])) + list(payload.get("results", []))
        unique: dict[str, dict[str, Any]] = {}
        normalized_study_path = str(Path(study_path).expanduser().resolve())
        for original in source_rows:
            if not isinstance(original, dict):
                continue
            row = dict(original)
            candidate_id = str(row.get("candidate_id", ""))
            digest = str(row.get("report_sha256", ""))
            if not candidate_id or not digest:
                continue
            report_id = str(row.get("report_id") or stable_report_id(normalized_study_path, candidate_id, digest))
            row["report_id"] = report_id
            unique[report_id] = row

        imported = 0
        with self._connection() as connection:
            for report_id, row in unique.items():
                source_pdf = Path(str(row.get("report_path", ""))) if row.get("report_path") else None
                archived_pdf: Path | None = None
                if source_pdf and source_pdf.is_file():
                    archived_pdf = self.root / "reports" / f"{report_id}.pdf"
                    existing = connection.execute(
                        "SELECT report_path, report_sha256 FROM reports WHERE report_id = ?", (report_id,)
                    ).fetchone()
                    previous_copy = Path(existing["report_path"]) if existing and existing["report_path"] else None
                    already_verified = bool(
                        existing and existing["report_sha256"] == row["report_sha256"]
                        and previous_copy and previous_copy.is_file() and previous_copy.is_relative_to(self.root)
                    )
                    if already_verified:
                        archived_pdf = previous_copy
                    elif not archived_pdf.is_file() or _sha256(archived_pdf) != row["report_sha256"]:
                        archived_pdf.parent.mkdir(parents=True, exist_ok=True)
                        descriptor, temporary = tempfile.mkstemp(
                            prefix=f".{report_id}.", suffix=".tmp", dir=archived_pdf.parent
                        )
                        os.close(descriptor)
                        try:
                            shutil.copyfile(source_pdf, temporary)
                            if _sha256(Path(temporary)) != row["report_sha256"]:
                                raise RuntimeError(f"Archived report hash changed while copying: {source_pdf}")
                            os.replace(temporary, archived_pdf)
                        except Exception:
                            try:
                                os.unlink(temporary)
                            except FileNotFoundError:
                                pass
                            raise
                elif row.get("report_path"):
                    existing = connection.execute(
                        "SELECT report_path FROM reports WHERE report_id = ?", (report_id,)
                    ).fetchone()
                    if existing and existing["report_path"] and Path(existing["report_path"]).is_file():
                        archived_pdf = Path(existing["report_path"])

                provenance = dict(row.get("provenance") or {})
                imported_at = str(provenance.get("imported_at") or row.get("imported_at") or "")
                if not imported_at:
                    imported_at = datetime.fromtimestamp(
                        source_pdf.stat().st_mtime, timezone.utc
                    ).isoformat() if source_pdf and source_pdf.is_file() else datetime.now(timezone.utc).isoformat()
                values = (
                    report_id,
                    normalized_study_path,
                    str(row.get("study_id") or study_id),
                    str(row.get("candidate_id", "")),
                    str(row.get("case_name", "")),
                    imported_at,
                    str(row.get("report_sha256", "")),
                    str(row.get("validation_status", "unknown")),
                    str(archived_pdf) if archived_pdf else None,
                    _json(row.get("metrics", {})),
                    _json(row.get("report_values", {})),
                    _json(row.get("parameters", {})),
                    _json(row.get("parameter_units", {})),
                    _json(row.get("validation", {})),
                    _json(provenance),
                )
                cursor = connection.execute(
                    """INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(report_id) DO UPDATE SET
                        case_name=excluded.case_name, imported_at=excluded.imported_at,
                        validation_status=excluded.validation_status, report_path=excluded.report_path,
                        metrics_json=excluded.metrics_json, report_values_json=excluded.report_values_json,
                        parameters_json=excluded.parameters_json, parameter_units_json=excluded.parameter_units_json,
                        validation_json=excluded.validation_json, provenance_json=excluded.provenance_json""",
                    values,
                )
                imported += int(cursor.rowcount > 0)
        return imported

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        for column, key in (
            ("metrics_json", "metrics"),
            ("report_values_json", "report_values"),
            ("parameters_json", "parameters"),
            ("parameter_units_json", "parameter_units"),
            ("validation_json", "validation"),
            ("provenance_json", "provenance"),
        ):
            value[key] = json.loads(value.pop(column))
        return value

    def list_reports(self, *, study_path: str | Path | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if study_path is None:
                rows = connection.execute("SELECT * FROM reports ORDER BY imported_at DESC, report_id").fetchall()
            else:
                key = str(Path(study_path).expanduser().resolve())
                rows = connection.execute(
                    "SELECT * FROM reports WHERE study_path = ? ORDER BY imported_at DESC, report_id", (key,)
                ).fetchall()
        records = [self._decode(row) for row in rows]
        if limit is not None:
            records = records[:max(0, int(limit))]
        return records

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM reports WHERE report_id = ?", (report_id,)).fetchone()
        return self._decode(row) if row else None

    def save_profile(self, profile: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO settings(key, value_json) VALUES('analysis_profile', ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (_json(profile),),
            )

    def load_profile(self) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT value_json FROM settings WHERE key='analysis_profile'").fetchone()
        return json.loads(row[0]) if row else None


def validate_metric_profile(profile: dict[str, Any]) -> dict[str, Any]:
    metrics = profile.get("metrics", [])
    if not isinstance(metrics, list) or len(metrics) > 5:
        raise ValueError("An analysis profile can contain up to five metrics")
    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, item in enumerate(metrics):
        name = str(item.get("metric", "")).strip()
        if not name:
            continue
        if name in names:
            raise ValueError(f"Metric {name!r} is listed more than once")
        names.add(name)
        goal = str(item.get("goal", "maximize"))
        if goal not in {"maximize", "minimize", "target"}:
            raise ValueError(f"Unsupported goal for {name!r}: {goal}")
        weight = float(item.get("weight", 1.0))
        priority = int(item.get("priority", index + 1))
        if not math.isfinite(weight) or weight < 0:
            raise ValueError(f"Weight for {name!r} must be finite and non-negative")
        row = {"metric": name, "goal": goal, "weight": weight, "priority": priority}
        if goal == "target":
            target = float(item.get("target"))
            if not math.isfinite(target):
                raise ValueError(f"Target for {name!r} must be finite")
            row["target"] = target
        normalized.append(row)
    normalized.sort(key=lambda item: (item["priority"], item["metric"]))
    if not normalized:
        raise ValueError("Choose at least one metric")
    if not any(item["weight"] > 0 for item in normalized):
        raise ValueError("At least one metric must have a positive weight")
    return {"metrics": normalized}


def _metric_percentiles(rows: list[dict[str, Any]], objective: dict[str, Any]) -> dict[str, float]:
    metric = objective["metric"]
    goal = objective["goal"]
    def key(row: dict[str, Any]):
        number = float(row["metrics"][metric])
        if goal == "maximize":
            return -number
        if goal == "minimize":
            return number
        return abs(number - float(objective["target"]))

    ordered = sorted(rows, key=lambda row: (key(row), row["report_id"]))
    scores: dict[str, float] = {}
    position = 0
    while position < len(ordered):
        stop = position + 1
        current = key(ordered[position])
        while stop < len(ordered) and math.isclose(key(ordered[stop]), current, rel_tol=1e-12, abs_tol=1e-12):
            stop += 1
        average_rank = (position + 1 + stop) / 2.0
        score = 1.0 if len(ordered) == 1 else (len(ordered) - average_rank) / (len(ordered) - 1)
        for row in ordered[position:stop]:
            scores[row["report_id"]] = score
        position = stop
    return scores


def rank_report_rows(
    rows: list[dict[str, Any]], profile: dict[str, Any], mode: str = "weighted", selected_metric: str | None = None
) -> dict[str, Any]:
    """Rank validated report records without mixing metric units."""
    profile = validate_metric_profile(profile)
    objectives = profile["metrics"]
    if mode not in {"weighted", "priority", "per_metric"}:
        raise ValueError("Ranking mode must be weighted, priority, or per_metric")
    allowed_status = {"match", "review_required"}
    exclusions: list[dict[str, str]] = []
    base: list[dict[str, Any]] = []
    for row in rows:
        status = str(row.get("validation_status", "unknown"))
        if status not in allowed_status:
            exclusions.append({"report_id": row["report_id"], "reason": f"Validation status is {status}"})
        else:
            base.append(row)

    def complete(items: list[dict[str, Any]], objective_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for row in items:
            missing = [obj["metric"] for obj in objective_rows if obj["metric"] not in row.get("metrics", {})]
            if missing:
                exclusions.append({"report_id": row["report_id"], "reason": "Missing metric(s): " + ", ".join(missing)})
                continue
            try:
                finite = all(math.isfinite(float(row["metrics"][obj["metric"]])) for obj in objective_rows)
            except (TypeError, ValueError, OverflowError):
                finite = False
            if not finite:
                exclusions.append({"report_id": row["report_id"], "reason": "Selected metric is not finite"})
                continue
            result.append(row)
        return result

    if mode == "per_metric":
        requested = selected_metric or objectives[0]["metric"]
        target = next((item for item in objectives if item["metric"] == requested), None)
        if target is None:
            raise ValueError(f"Metric {requested!r} is not in the active profile")
        eligible = complete(base, [target])
        scores = _metric_percentiles(eligible, target) if eligible else {}
        ordered = sorted(eligible, key=lambda row: (-scores[row["report_id"]], -_recentness(row), row["report_id"]))
        entries = [dict(row, rank=index + 1, score=scores[row["report_id"]]) for index, row in enumerate(ordered)]
        return {"mode": mode, "metric": requested, "entries": entries, "metric_entries": {requested: entries}, "exclusions": exclusions}

    eligible = complete(base, objectives)
    score_by_metric = {item["metric"]: _metric_percentiles(eligible, item) for item in objectives} if eligible else {}
    weights_total = sum(item["weight"] for item in objectives)
    scored: list[dict[str, Any]] = []
    for row in eligible:
        values = {item["metric"]: score_by_metric[item["metric"]][row["report_id"]] for item in objectives}
        composite = sum(values[item["metric"]] * item["weight"] for item in objectives) / weights_total
        scored.append(dict(row, _preference_values=values, _score=composite))
    if mode == "weighted":
        scored.sort(key=lambda row: (-row["_score"], -_recentness(row), row["report_id"]))
    else:
        scored.sort(
            key=lambda row: (
                tuple(-row["_preference_values"][item["metric"]] for item in objectives),
                -_recentness(row), row["report_id"],
            )
        )
    primary_metric = objectives[0]["metric"]
    entries = [
        dict(row, rank=index + 1, score=row["_score"] if mode == "weighted" else row["_preference_values"][primary_metric])
        for index, row in enumerate(scored)
    ]
    return {"mode": mode, "entries": entries, "metric_entries": {}, "exclusions": exclusions}


def report_comparison(latest: dict[str, Any] | None, references: list[dict[str, Any]], profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Return metric, input, and report-field changes as observations."""
    if not latest:
        return []
    objectives = validate_metric_profile(profile)["metrics"]
    result: list[dict[str, Any]] = []
    latest_parameters = latest.get("parameters", {})
    latest_units = latest.get("parameter_units", {})
    latest_fields = _flatten(latest.get("report_values", {}))
    for reference in references:
        metric_deltas = []
        for objective in objectives:
            name = objective["metric"]
            left = latest.get("metrics", {}).get(name)
            right = reference.get("metrics", {}).get(name)
            metric_deltas.append({
                "metric": name,
                "unit": _unit(name),
                "latest": left,
                "reference": right,
                "delta": float(left) - float(right) if left is not None and right is not None else None,
            })
        parameter_deltas = []
        reference_units = reference.get("parameter_units", {})
        for name in sorted(set(latest_parameters) | set(reference.get("parameters", {}))):
            left = latest_parameters.get(name)
            right = reference.get("parameters", {}).get(name)
            if left == right:
                continue
            left_unit, right_unit = latest_units.get(name), reference_units.get(name)
            parameter_deltas.append({
                "field": name,
                "latest": left,
                "reference": right,
                "unit": left_unit if left_unit == right_unit else None,
                "comparable": left_unit == right_unit,
            })
        fields = _flatten(reference.get("report_values", {}))
        report_deltas = []
        for name in sorted(set(latest_fields) | set(fields)):
            left, right = latest_fields.get(name), fields.get(name)
            if left == right:
                continue
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                report_deltas.append({"field": name, "latest": left, "reference": right, "delta": float(left) - float(right)})
            else:
                report_deltas.append({"field": name, "latest": left, "reference": right, "delta": None})
        result.append({
            "reference": reference,
            "metric_deltas": metric_deltas,
            "parameter_deltas": parameter_deltas,
            "report_deltas": report_deltas[:30],
        })
    return result


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    output: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            output.update(_flatten(child, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            output.update(_flatten(child, f"{prefix}[{index}]"))
    elif value is None or isinstance(value, (str, int, float, bool)):
        output[prefix] = value
    return output


def _unit(metric: str) -> str:
    folded = metric.casefold()
    for suffix, unit in (
        ("_specific_impulse_s", "s"), ("_characteristic_velocity_m_s", "m/s"),
        ("_effective_exhaust_velocity_m_s", "m/s"), ("_thrust_kn", "kN"),
        ("_mass_flow_kg_s", "kg/s"), ("_pressure_mpa", "MPa"),
        ("_pressure_atm", "atm"), ("_altitude_km", "km"), ("_temperature_k", "K"),
        ("_diameter_mm", "mm"), ("_diameter_in", "in"),
    ):
        if folded.endswith(suffix):
            return unit
    return ""
