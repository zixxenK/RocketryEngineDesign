"""Application service and background-job layer for the desktop and CLI."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from rocket_analysis.bundles import pack_study_candidates, scan_returns, unpack_rpa4_bundle
from rocket_analysis.reports import (
    ReportLibrary,
    ReportArchiveBackend,
    default_report_archive_root,
    rank_report_rows,
    report_comparison,
    validate_metric_profile,
)
from rocket_analysis.study import (
    SCHEMA_VERSION,
    StudySpec,
    _atomic_write_text,
    _candidate_directories,
    _optuna_study,
    generate_candidate_cfgs,
    import_shared_rpa4_results,
    load_study_spec,
    preview_parameter_sets,
    rank_screened_candidates,
    rank_study_results,
    save_study_ranking,
    screen_study_candidates,
    stage_rpa4_candidates,
    design_constraint_violations,
)
from rocket_analysis.ranking import candidate_metric_catalog


class CandidateStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    RECEIVED = "received"
    PARSED = "parsed"
    VALIDATED = "validated"
    REVIEW = "review"
    FAILED = "failed"
    DUPLICATE = "duplicate"
    UNPAIRED = "unpaired"
    STALE = "stale"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobCancelled(RuntimeError):
    """Raised at a cooperative cancellation checkpoint."""


@dataclass
class JobContext:
    job_id: str
    title: str
    cancel_event: threading.Event
    progress: float | None = None
    message: str = "Queued"
    state: JobState = JobState.QUEUED
    error: str | None = None
    result: Any = None
    started_at: str | None = None
    finished_at: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def report(self, progress: float | None, message: str) -> None:
        if progress is not None and not 0.0 <= progress <= 1.0:
            raise ValueError("Job progress must be between 0 and 1")
        with self._lock:
            self.progress = progress
            self.message = str(message)

    def checkpoint(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled(f"Job {self.title!r} was cancelled")


@dataclass(frozen=True)
class CandidateStatusRow:
    candidate_id: str
    case_name: str
    status: CandidateStatus
    reason: str = ""
    sent_at: str | None = None
    validation_status: str | None = None


@dataclass(frozen=True)
class StudyOverview:
    study_id: str
    sampling_method: str
    seed: int
    candidate_limit: int
    proposed_count: int | None
    preview_error: str | None
    generated_count: int
    screened_count: int
    imported_count: int
    constraint_count: int


@dataclass(frozen=True)
class MetricDescriptor:
    name: str
    group: str
    tier: str
    unit: str


@dataclass(frozen=True)
class CandidateDetail:
    candidate_id: str
    case_name: str
    status: str
    parameters: dict[str, Any]
    parameter_units: dict[str, Any]
    cfg: dict[str, Any]
    report_values: dict[str, Any]
    validation: dict[str, Any]
    screening: dict[str, Any]
    provenance: dict[str, Any]


@dataclass(frozen=True)
class BundlePreview:
    source: str
    case_names: tuple[str, ...]
    pdf_files: tuple[str, ...]
    cfg_files: tuple[str, ...]
    run_sheet_found: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackPreview:
    candidates: tuple[tuple[str, str, tuple[str, ...]], ...]
    excluded: tuple[tuple[str, str], ...]


def _metric_descriptor(name: str) -> MetricDescriptor:
    pieces = name.split(".")
    tier = pieces[0] if pieces[0] in {"ideal", "estimated_delivered", "derived", "screening"} else "design/report"
    units = (
        ("specific_impulse_m_s", "m/s"), ("specific_impulse_s", "s"),
        ("characteristic_velocity_m_s", "m/s"),
        ("effective_exhaust_velocity_m_s", "m/s"), ("thrust_coefficient", "unitless"),
        ("thrust_kn", "kN"), ("mass_flow_kg_s", "kg/s"), ("pressure_mpa", "MPa"),
        ("pressure_atm", "atm"), ("altitude_km", "km"), ("temperature_k", "K"),
        ("mach", "Mach"), ("fraction", "fraction"), ("diameter_mm", "mm"),
        ("diameter_in", "in"), ("ratio", "ratio"),
    )
    leaf = pieces[-1].casefold()
    unit = next((unit for suffix, unit in units if leaf.endswith(suffix)), "")
    if leaf == "characteristic_velocity_efficiency":
        unit = "fraction"
    return MetricDescriptor(name, pieces[0].replace("_", " ").title(), tier, unit)


class JobManager:
    """Small thread-pool job runner; operations cooperate via checkpoints."""

    def __init__(self, workers: int = 2):
        if workers < 1:
            raise ValueError("workers must be at least one")
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rocket-study")
        self._jobs: dict[str, JobContext] = {}
        self._futures: dict[str, Future[Any]] = {}
        self._lock = threading.RLock()

    def submit(self, title: str, operation: Callable[[JobContext], Any]) -> JobContext:
        job = JobContext(uuid.uuid4().hex, title, threading.Event())
        with self._lock:
            self._jobs[job.job_id] = job

        def run() -> Any:
            with job._lock:
                job.state = JobState.RUNNING
                job.started_at = datetime.now(timezone.utc).isoformat()
                job.message = "Running"
            try:
                job.checkpoint()
                result = operation(job)
                job.checkpoint()
                with job._lock:
                    job.result = result
                    job.state = JobState.DONE
                    job.progress = 1.0
                    job.message = "Done"
                return result
            except JobCancelled as error:
                with job._lock:
                    job.state = JobState.CANCELLED
                    job.message = str(error)
                return None
            except Exception as error:
                with job._lock:
                    job.state = JobState.FAILED
                    job.error = f"{type(error).__name__}: {error}"
                    job.message = job.error
                raise
            finally:
                with job._lock:
                    job.finished_at = datetime.now(timezone.utc).isoformat()

        future = self._executor.submit(run)
        with self._lock:
            self._futures[job.job_id] = future
        return job

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            future = self._futures.get(job_id)
        if job is None:
            return False
        job.cancel_event.set()
        if future and future.cancel():
            with job._lock:
                job.state = JobState.CANCELLED
                job.message = "Cancelled before starting"
                job.finished_at = datetime.now(timezone.utc).isoformat()
        else:
            job.report(job.progress, "Cancellation requested")
        return True

    def jobs(self) -> tuple[JobContext, ...]:
        with self._lock:
            return tuple(self._jobs.values())

    def close(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=not wait)


def _result_path(spec: StudySpec) -> Path:
    return spec.output_dir / spec.study_id / "rpa4-results.json"


def candidate_statuses(spec: StudySpec) -> tuple[CandidateStatusRow, ...]:
    """Build the stable status-board rows from project manifests and imports."""
    results_file = _result_path(spec)
    results: dict[str, dict[str, Any]] = {}
    if results_file.is_file():
        try:
            results = {
                str(row["candidate_id"]): row
                for row in json.loads(results_file.read_text(encoding="utf-8")).get("results", [])
            }
        except (json.JSONDecodeError, KeyError, TypeError):
            results = {}
    rows: list[CandidateStatusRow] = []
    for directory in _candidate_directories(spec):
        manifest_path = directory / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rows.append(CandidateStatusRow(directory.name, directory.name, CandidateStatus.FAILED, "Unreadable manifest"))
            continue
        candidate_id = str(manifest.get("candidate_id", directory.name))
        case_name = str(manifest.get("case_name", candidate_id))
        result = results.get(candidate_id)
        validation = str((result or {}).get("validation_status", manifest.get("validation_status", "")))
        reason = ""
        status_value = str(manifest.get("status", "pending"))
        if manifest.get("user_marked_stale"):
            status = CandidateStatus.STALE
            reason = "Marked stale by user"
        elif result:
            if validation == "mismatch":
                status = CandidateStatus.FAILED
                reason = "CFG/PDF validation has a hard mismatch"
            elif validation == "review_required":
                status = CandidateStatus.REVIEW
                reason = "Report imported with unresolved validation review items"
            else:
                status = CandidateStatus.VALIDATED
        elif status_value in {"sent", "staged"}:
            status = CandidateStatus.SENT
        elif status_value in {"rpa4_imported", "parsed"}:
            status = CandidateStatus.PARSED
        elif status_value == "received":
            status = CandidateStatus.RECEIVED
        else:
            status = CandidateStatus.PENDING
        rows.append(
            CandidateStatusRow(
                candidate_id,
                case_name,
                status,
                reason,
                manifest.get("sent_at"),
                validation or None,
            )
        )
    return tuple(sorted(rows, key=lambda row: (row.case_name.casefold(), row.candidate_id)))


def mark_candidate_stale(spec: StudySpec, candidate_id: str, stale: bool = True) -> None:
    for directory in _candidate_directories(spec):
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if str(manifest.get("candidate_id")) == candidate_id:
            manifest["user_marked_stale"] = bool(stale)
            manifest["stale_marked_at"] = datetime.now(timezone.utc).isoformat() if stale else None
            _atomic_write_text(manifest_path, json.dumps(manifest, indent=2) + "\n")
            return
    raise KeyError(f"Unknown candidate ID: {candidate_id}")


def abandon_adaptive_trials(spec: StudySpec, trial_numbers: list[int] | None = None) -> tuple[int, ...]:
    """Mark selected running Optuna trials failed so they no longer count as pending."""
    if spec.sampling_method != "adaptive":
        raise ValueError("abandon is available only for adaptive studies")
    import optuna

    study = _optuna_study(spec)
    requested = set(trial_numbers) if trial_numbers is not None else None
    abandoned: list[int] = []
    for trial in study.get_trials(deepcopy=False, states=(optuna.trial.TrialState.RUNNING,)):
        if requested is None or trial.number in requested:
            study.tell(trial.number, state=optuna.trial.TrialState.FAIL)
            abandoned.append(trial.number)
    missing = requested - set(abandoned) if requested is not None else set()
    if missing:
        raise ValueError("Trial(s) are not currently running: " + ", ".join(map(str, sorted(missing))))
    return tuple(abandoned)


def _doctor_check(name: str, state: str, detail: str, fix: str = "") -> dict[str, str]:
    return {"name": name, "state": state, "detail": detail, "fix": fix}


def run_doctor(
    *,
    shared_dir: str | Path | None = None,
    wsl_distro: str = "Ubuntu-24.04",
    cantera_mechanism: str | None = None,
) -> tuple[dict[str, str], ...]:
    """Read-only environment and path checks; suggested fixes are never run."""
    checks: list[dict[str, str]] = []
    supported_python = (3, 12) <= sys.version_info[:2] < (3, 13)
    checks.append(_doctor_check("Python", "ok" if supported_python else "warn", sys.version.split()[0], "Use the rocket-dev Conda environment with Python 3.12."))
    conda = os.environ.get("CONDA_DEFAULT_ENV")
    checks.append(_doctor_check("Conda environment", "ok" if conda else "warn", conda or "No active Conda environment detected", "Run conda activate rocket-dev."))
    modules = {
        "CEA": ("cea", "Install the project dependencies in the active environment."),
        "Cantera": ("cantera", "Install Cantera only if this study has a validated mechanism."),
        "Optuna": ("optuna", "Install project dependencies to use adaptive sampling."),
        "OpenMDAO": ("openmdao", "Install project dependencies to use the OpenMDAO adapter."),
        "pdfplumber": ("pdfplumber", "Install project dependencies to import RPA4 reports."),
        "PySide6": ("PySide6", "Install the desktop extra: pip install -e .[desktop]."),
        "pyqtgraph": ("pyqtgraph", "Install the desktop extra: pip install -e .[desktop]."),
    }
    for label, (module, fix) in modules.items():
        try:
            available = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            available = False
        state = "ok" if available else ("warn" if label in {"CEA", "Cantera", "PySide6", "pyqtgraph"} else "missing")
        checks.append(_doctor_check(label, state, "Import available" if available else "Package not found", "" if available else fix))
    mechanism = (cantera_mechanism or os.environ.get("CANTERA_MECHANISM", "")).strip()
    checks.append(_doctor_check("Cantera mechanism", "ok" if mechanism else "warn", mechanism or "No default mechanism configured", "Configure a study-specific mechanism and verify its RP-1 surrogate before enabling Cantera."))
    wsl = shutil.which("wsl.exe")
    if not wsl:
        checks.append(_doctor_check("WSL", "missing", "wsl.exe was not found", "Install WSL and a Linux distribution."))
    else:
        try:
            result = subprocess.run([wsl, "-l", "-q"], capture_output=True, text=True, timeout=8, check=False)
            output = result.stdout.replace("\x00", "").strip()
            found = any(line.strip() == wsl_distro for line in output.splitlines())
            checks.append(_doctor_check("WSL distribution", "ok" if found else "warn", f"{wsl_distro}: " + ("available" if found else "not listed"), "Install or select the configured WSL distribution."))
            if found:
                foam = subprocess.run([wsl, "-d", wsl_distro, "--", "bash", "-lc", "command -v openfoam2512"], capture_output=True, text=True, timeout=8, check=False)
                detected = foam.stdout.strip()
                checks.append(_doctor_check("OpenFOAM", "ok" if foam.returncode == 0 and detected else "warn", detected or "OpenFOAM command not found in the distro", "Install and configure OpenFOAM in WSL; CFD remains disabled until available."))
        except (OSError, subprocess.TimeoutExpired) as error:
            checks.append(_doctor_check("WSL", "warn", str(error), "Check WSL startup and distribution configuration."))
    if shared_dir:
        path = Path(shared_dir).expanduser()
        readable = path.is_dir() and os.access(path, os.R_OK)
        writable = path.is_dir() and os.access(path, os.W_OK)
        state = "ok" if readable and writable else "warn"
        checks.append(_doctor_check("RPA4 folder", state, f"{path.resolve()} (readable={readable}, writable={writable})", "Choose a local inbox or a reachable shared folder with read/write access."))
        checks.append(_doctor_check("Path length", "warn" if len(str(path.resolve())) >= 220 else "ok", f"{len(str(path.resolve()))} characters", "Move the project closer to the drive root if paths approach 240 characters."))
    else:
        checks.append(_doctor_check("RPA4 folder", "warn", "No folder configured; RPA4 import/export actions need a path", "Set RPA4_SHARED_DIR or choose a folder in the app."))
    checks.append(_doctor_check("OneDrive hydration", "warn", "Cloud-only status is not reliably detectable from this process", "Mark the inbox as Always keep on this device and rescan after sync completes."))
    return tuple(checks)


class StudyService:
    """Thin orchestration API shared by the GUI and command line."""

    def __init__(self, study_path: str | Path, *, archive_root: str | Path | None = None):
        self.study_path = Path(study_path).expanduser().resolve()
        self.spec = load_study_spec(self.study_path)
        self.report_library: ReportArchiveBackend = ReportLibrary(archive_root)
        self.report_library.sync_study(self.study_path, self.spec.study_id, _result_path(self.spec))
        self.jobs = JobManager()

    def set_report_archive_root(self, archive_root: str | Path) -> None:
        self.report_library = self.report_library.relocate(archive_root)
        self.report_library.sync_study(self.study_path, self.spec.study_id, _result_path(self.spec))

    def preview(self, count: int | None = None) -> tuple[dict[str, Any], ...]:
        return preview_parameter_sets(self.spec, count=count)

    def study_overview(self) -> StudyOverview:
        """Return the compact counts and sampling summary used by the desktop UI."""
        statuses = candidate_statuses(self.spec)
        dirs = _candidate_directories(self.spec)
        try:
            proposed = len(self.preview())
            preview_error = None
        except Exception as error:
            proposed = None
            preview_error = str(error)
        imported = sum(row.status in {CandidateStatus.VALIDATED, CandidateStatus.REVIEW, CandidateStatus.FAILED} for row in statuses)
        screened = sum((directory / "screening.json").is_file() for directory in dirs)
        return StudyOverview(
            study_id=self.spec.study_id,
            sampling_method=self.spec.sampling_method,
            seed=self.spec.seed,
            candidate_limit=self.spec.max_candidates,
            proposed_count=proposed,
            preview_error=preview_error,
            generated_count=len(statuses),
            screened_count=screened,
            imported_count=imported,
            constraint_count=len(self.spec.design_constraints),
        )

    def metric_catalog(self) -> tuple[MetricDescriptor, ...]:
        """Return metrics present in imported reports or screening results."""
        names: set[str] = set()
        result_path = _result_path(self.spec)
        if result_path.is_file():
            try:
                for row in json.loads(result_path.read_text(encoding="utf-8")).get("results", []):
                    names.update(str(name) for name in row.get("metrics", {}))
            except (OSError, json.JSONDecodeError, TypeError):
                pass
        for directory in _candidate_directories(self.spec):
            screen_path = directory / "screening.json"
            if not screen_path.is_file():
                continue
            try:
                values = json.loads(screen_path.read_text(encoding="utf-8"))
                names.update(candidate_metric_catalog({}, values).keys())
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
        for objective in self.spec.ranking.get("objectives", []):
            if objective.get("metric"):
                names.add(str(objective["metric"]))
        for report in self.report_library.list_reports():
            names.update(str(name) for name in report.get("metrics", {}))
        return tuple(_metric_descriptor(name) for name in sorted(names))

    def ranking_snapshot(self) -> dict[str, Any]:
        path = self.spec.output_dir / self.spec.study_id / "ranking.json"
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def archived_report_path(self, candidate_id: str) -> Path | None:
        result_path = _result_path(self.spec)
        if not result_path.is_file():
            return None
        try:
            rows = json.loads(result_path.read_text(encoding="utf-8")).get("results", [])
        except (OSError, json.JSONDecodeError):
            return None
        report = next((row.get("report_path") for row in rows if row.get("candidate_id") == candidate_id), None)
        path = Path(report) if report else None
        return path if path and path.is_file() else None

    def imported_summary(self) -> dict[str, Any]:
        path = _result_path(self.spec)
        if not path.is_file():
            return {"last_scanned_at": None, "result_count": 0}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return {
                "last_scanned_at": payload.get("last_scanned_at"),
                "result_count": len(payload.get("report_history", payload.get("results", []))),
                "duplicates": payload.get("duplicates", []),
                "unpaired": payload.get("unpaired", []),
                "scan_problems": payload.get("scan_problems", []),
            }
        except (OSError, json.JSONDecodeError, TypeError):
            return {"last_scanned_at": None, "result_count": 0, "scan_error": "Imported results could not be read."}

    def candidate_detail(self, candidate_id: str) -> CandidateDetail:
        """Load a candidate's GUI-ready detail from the project archive."""
        directory = next((path for path in _candidate_directories(self.spec) if path.name == candidate_id), None)
        if directory is None:
            raise KeyError(f"Unknown candidate ID: {candidate_id}")
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        config_name = str(manifest.get("config_file", "candidate.cfg"))
        cfg_path = directory / config_name
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cfg = {"error": "CFG unavailable or unreadable"}
        result_path = _result_path(self.spec)
        results = json.loads(result_path.read_text(encoding="utf-8")).get("results", []) if result_path.is_file() else []
        result = next((row for row in results if row.get("candidate_id") == candidate_id), {})
        screening_path = directory / "screening.json"
        screening = json.loads(screening_path.read_text(encoding="utf-8")) if screening_path.is_file() else {}
        status = next((row for row in candidate_statuses(self.spec) if row.candidate_id == candidate_id), None)
        validation = dict(result.get("validation", {}))
        validation.setdefault("status", result.get("validation_status"))
        validation["returned_cfg_audit"] = result.get("returned_cfg_audit")
        if isinstance(validation.get("checks"), list):
            validation["checks"] = sorted(
                validation["checks"],
                key=lambda row: (0 if "nozzle inlet area ratio" in row.get("field", "").casefold() else 1, row.get("field", "")),
            )
        return CandidateDetail(
            candidate_id=candidate_id,
            case_name=str(manifest.get("case_name", candidate_id)),
            status=status.status.value if status else "unknown",
            parameters=dict(manifest.get("parameters", {})),
            parameter_units=dict(manifest.get("parameter_units", {})),
            cfg=cfg,
            report_values=dict(result.get("report_values", {})),
            validation=validation,
            screening=screening,
            provenance=dict(result.get("provenance", {})),
        )

    def preview_return_source(self, source: str | Path | list[str] | tuple[str, ...]) -> BundlePreview:
        """Inspect an import source without extracting or importing files."""
        warnings: list[str] = []
        members: list[str] = []
        selected_files: list[Path] | None = None
        if isinstance(source, (list, tuple)):
            selected_files = [Path(item).expanduser().resolve() for item in source]
            if not selected_files:
                raise ValueError("Select at least one RPA4 return file")
            invalid = [item for item in selected_files if not item.is_file() or item.suffix.casefold() not in {".pdf", ".cfg"}]
            if invalid:
                raise ValueError("Select loose PDF and CFG files; ZIP bundles and folders are selected one at a time.")
            members = [f"{index + 1}/{item.name}" for index, item in enumerate(selected_files)]
            source_label = "; ".join(str(item) for item in selected_files)
        else:
            path = Path(source).expanduser().resolve()
            if path.is_file() and path.suffix.casefold() == ".zip":
                try:
                    with zipfile.ZipFile(path) as archive:
                        members = [item.filename.replace("\\", "/") for item in archive.infolist() if not item.is_dir()]
                except (OSError, zipfile.BadZipFile) as error:
                    raise ValueError(f"Could not inspect return bundle: {error}") from error
            elif path.is_dir():
                members = [item.relative_to(path).as_posix() for item in path.rglob("*") if item.is_file()]
            elif path.is_file() and path.suffix.casefold() == ".pdf":
                members = [path.name]
            else:
                raise FileNotFoundError(f"Return source is not a folder, PDF, or zip: {path}")
            source_label = str(path)
        pdfs = tuple(name for name in members if Path(name).suffix.casefold() == ".pdf")
        cfgs = tuple(name for name in members if Path(name).suffix.casefold() == ".cfg")
        if selected_files is not None:
            case_names = tuple(sorted({item.stem for item in selected_files if item.suffix.casefold() in {".pdf", ".cfg"}}))
        else:
            case_names = tuple(sorted({Path(name).parent.name for name in (*pdfs, *cfgs) if Path(name).parent.name not in {"", "."}}))
        if not pdfs:
            warnings.append("No RPA4 PDF reports found in this selection.")
        known = {row.case_name.casefold() for row in candidate_statuses(self.spec)}
        unknown = [name for name in case_names if name.casefold() not in known]
        if unknown:
            source_kind = "selected filename(s)" if selected_files is not None else "folder name(s)"
            warnings.append(f"{len(unknown)} {source_kind} do not match a staged case; report pairing uses the case name printed in the PDF.")
        return BundlePreview(source_label, case_names, pdfs, cfgs, any(Path(name).name.casefold().startswith("run-sheet") for name in members), tuple(warnings))

    def preview_pack(self, candidate_ids: set[str] | None = None) -> PackPreview:
        """Describe packable cases and reasons they cannot be included."""
        requested = set(candidate_ids) if candidate_ids is not None else None
        included: list[tuple[str, str, tuple[str, ...]]] = []
        excluded: list[tuple[str, str]] = []
        found: set[str] = set()
        for directory in _candidate_directories(self.spec):
            manifest_path = directory / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                excluded.append((directory.name, "Unreadable manifest"))
                continue
            candidate_id = str(manifest.get("candidate_id", directory.name))
            if requested is not None and candidate_id not in requested:
                continue
            found.add(candidate_id)
            reasons = list(design_constraint_violations(self.spec, manifest.get("parameters", {})))
            cfg_name = str(manifest.get("config_file", "candidate.cfg"))
            cfg_path = directory / cfg_name
            if not cfg_path.is_file():
                reasons.append("Candidate CFG is missing")
            if reasons:
                excluded.append((candidate_id, "; ".join(reasons)))
                continue
            files = [cfg_name, "manifest.json", "RUN-SHEET.md", "RUN-SHEET.html", "pack-manifest.json"]
            if (directory / "screening.json").is_file():
                files.append("screening.json")
            included.append((candidate_id, str(manifest.get("case_name", candidate_id)), tuple(files)))
        if requested is not None:
            excluded.extend((item, "Candidate was not found in this study") for item in sorted(requested - found))
        return PackPreview(tuple(included), tuple(excluded))

    def generate(self, count: int | None = None, *, context: JobContext | None = None) -> tuple[Any, ...]:
        return generate_candidate_cfgs(self.spec, count=count, cancel_check=context.checkpoint if context else None)

    def screen(self, *, context: JobContext | None = None) -> dict[str, dict[str, Any]]:
        return screen_study_candidates(self.spec, cancel_check=context.checkpoint if context else None)

    def pack(
        self,
        output_path: str | Path,
        candidate_ids: set[str] | None = None,
        *,
        context: JobContext | None = None,
    ) -> Path:
        return pack_study_candidates(
            self.spec,
            output_path,
            candidate_ids=candidate_ids,
            cancel_check=context.checkpoint if context else None,
        )

    def unpack(
        self,
        bundle_path: str | Path,
        inbox_dir: str | Path | None = None,
        *,
        context: JobContext | None = None,
    ) -> Path:
        destination = inbox_dir or (self.spec.output_dir / self.spec.study_id / "inbox")
        return unpack_rpa4_bundle(bundle_path, destination, cancel_check=context.checkpoint if context else None)

    def scan(self, source: str | Path | None = None, *, context: JobContext | None = None):
        return scan_returns(self.spec, source, cancel_check=context.checkpoint if context else None)

    def import_results(self, source: str | Path | list[str] | tuple[str, ...] | None = None, *, context: JobContext | None = None) -> dict[str, Any]:
        cancel_check = context.checkpoint if context else None
        if isinstance(source, (list, tuple)):
            with tempfile.TemporaryDirectory(prefix="rpa4-loose-return-") as temporary:
                root = Path(temporary)
                for index, item in enumerate(source):
                    if cancel_check:
                        cancel_check()
                    path = Path(item).expanduser().resolve()
                    if not path.is_file() or path.suffix.casefold() not in {".pdf", ".cfg"}:
                        raise ValueError(f"Loose return file is missing or unsupported: {path}")
                    target = root / path.name
                    if target.exists():
                        target = root / f"{index + 1}-{path.name}"
                    shutil.copyfile(path, target)
                result = import_shared_rpa4_results(self.spec, root, cancel_check=cancel_check)
        else:
            result = import_shared_rpa4_results(self.spec, source, cancel_check=cancel_check)
        self.report_library.sync_study(self.study_path, self.spec.study_id, _result_path(self.spec))
        return result

    def report_profile(self) -> dict[str, Any]:
        saved = self.report_library.load_profile()
        if saved:
            return validate_metric_profile(saved)
        objectives = self.spec.ranking.get("objectives", [])
        metrics = []
        for index, objective in enumerate(objectives[:5]):
            metrics.append({
                "metric": str(objective.get("metric", "")),
                "goal": str(objective.get("direction", "maximize")),
                "weight": float(self.spec.ranking.get("weights", {}).get(str(objective.get("metric", "")), 1.0)),
                "priority": index + 1,
            })
        if not metrics:
            metrics = [{"metric": "estimated_delivered.sea_level.specific_impulse_s", "goal": "maximize", "weight": 1.0, "priority": 1}]
        return validate_metric_profile({"metrics": metrics})

    def save_report_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        normalized = validate_metric_profile(profile)
        self.report_library.save_profile(normalized)
        return normalized

    def list_reports(self, *, scope: str = "study", limit: int | None = None) -> list[dict[str, Any]]:
        if scope not in {"study", "all"}:
            raise ValueError("Report scope must be 'study' or 'all'")
        return self.report_library.list_reports(study_path=self.study_path if scope == "study" else None, limit=limit)

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        return self.report_library.get_report(report_id)

    def rank_reports(self, *, scope: str = "study", mode: str = "weighted", selected_metric: str | None = None) -> dict[str, Any]:
        rows = self.list_reports(scope=scope)
        ranked = rank_report_rows(rows, self.report_profile(), mode, selected_metric)
        ranked["total_reports"] = len(rows)
        ranked["scope"] = scope
        return ranked

    def compare_latest_report(
        self, *, scope: str = "study", mode: str = "weighted", selected_metric: str | None = None
    ) -> dict[str, Any]:
        rows = self.list_reports(scope=scope)
        latest = rows[0] if rows else None
        ranked = rank_report_rows(rows, self.report_profile(), mode, selected_metric)
        references = ranked.get("entries", [])[:5]
        return {
            "scope": scope,
            "mode": mode,
            "latest": latest,
            "references": references,
            "comparisons": report_comparison(latest, references, self.report_profile()),
            "exclusions": ranked.get("exclusions", []),
        }

    def rank(self, ranking_spec: dict[str, Any] | None = None, *, screened: bool = False):
        selected_spec = ranking_spec or self.spec.ranking
        if screened and ranking_spec is None:
            selected_spec = {
                "mode": "pareto",
                "objectives": [
                    {"metric": "screening.cea.result.ideal_vacuum_specific_impulse_m_s", "direction": "maximize"},
                    {"metric": "screening.cea.result.characteristic_velocity_m_s", "direction": "maximize"},
                ],
                "constraints": [],
            }
        result = rank_screened_candidates(self.spec, selected_spec) if screened else rank_study_results(self.spec, ranking_spec)
        save_study_ranking(self.spec, result)
        return result

    def candidate_statuses(self) -> tuple[CandidateStatusRow, ...]:
        return candidate_statuses(self.spec)

    def finalist_ids(self, limit: int = 3) -> tuple[str, ...]:
        if limit < 1:
            raise ValueError("Finalist limit must be at least one")
        ranking_path = self.spec.output_dir / self.spec.study_id / "ranking.json"
        if not ranking_path.is_file():
            raise FileNotFoundError("Rank candidates before selecting CFD finalists")
        ranking = json.loads(ranking_path.read_text(encoding="utf-8"))
        entries = sorted(ranking.get("entries", []), key=lambda row: (int(row["rank"]), str(row["candidate_id"])))
        return tuple(str(row["candidate_id"]) for row in entries[:limit])

    def prepare_finalist_case(
        self,
        candidate_id: str,
        template_dir: str | Path,
        destination: str | Path,
        *,
        limit: int = 3,
        context: JobContext | None = None,
    ) -> Path:
        from rocket_analysis.cfd import prepare_openfoam_finalist_case

        finalists = self.finalist_ids(limit)
        if candidate_id not in finalists:
            raise ValueError(f"Candidate {candidate_id!r} is not in the top {limit} finalists")
        manifest_path = next(
            (directory / "manifest.json" for directory in _candidate_directories(self.spec) if directory.name == candidate_id),
            None,
        )
        if manifest_path is None:
            raise KeyError(f"Unknown candidate ID: {candidate_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        replacements = {
            str(name): str(value)
            for name, value in manifest.get("parameters", {}).items()
            if isinstance(value, (str, int, float, bool))
        }
        return prepare_openfoam_finalist_case(
            template_dir,
            destination,
            candidate_id=candidate_id,
            finalist_ids=finalists,
            replacements=replacements,
            cancel_check=context.checkpoint if context else None,
        )

    def run_finalist_case(
        self,
        candidate_id: str,
        case_path: str | Path,
        *,
        limit: int = 3,
        distro: str = "Ubuntu-24.04",
        timeout: float | None = None,
        context: JobContext | None = None,
    ):
        from rocket_analysis.cfd import run_openfoam_finalist_case

        finalists = self.finalist_ids(limit)
        return run_openfoam_finalist_case(
            candidate_id,
            finalists,
            str(case_path),
            distro=distro,
            timeout=timeout,
            cancel_check=context.checkpoint if context else None,
        )

    def abandon(self, trial_numbers: list[int] | None = None) -> tuple[int, ...]:
        return abandon_adaptive_trials(self.spec, trial_numbers)

    def submit(self, title: str, operation: Callable[[JobContext], Any]) -> JobContext:
        return self.jobs.submit(title, operation)

    def close(self, *, wait: bool = True) -> None:
        self.jobs.close(wait=wait)

    @staticmethod
    def create_from_cfg(base_cfg: str | Path, output_path: str | Path) -> Path:
        return create_initial_study(base_cfg, output_path)

    def study_payload(self) -> dict[str, Any]:
        return json.loads(self.study_path.read_text(encoding="utf-8"))

    def save_study_payload(self, payload: dict[str, Any]) -> StudySpec:
        """Validate and atomically replace this study's editable JSON settings."""
        if not isinstance(payload, dict):
            raise TypeError("Study settings must be a JSON object")
        original = self.study_path.read_text(encoding="utf-8")
        _atomic_write_text(self.study_path, json.dumps(payload, indent=2) + "\n")
        try:
            updated = load_study_spec(self.study_path)
        except Exception:
            _atomic_write_text(self.study_path, original)
            raise
        self.spec = updated
        return updated

    def mark_stale(self, candidate_id: str, stale: bool = True) -> None:
        mark_candidate_stale(self.spec, candidate_id, stale=stale)


def create_initial_study(base_cfg: str | Path, output_path: str | Path) -> Path:
    """Write a conservative one-point study spec from a valid RPA4 CFG."""
    from rocket_analysis.rpa4_validation import load_rpa4_inputs

    cfg = Path(base_cfg).expanduser().resolve()
    inputs = load_rpa4_inputs(cfg)
    target = Path(output_path).expanduser().resolve()
    study_id = "".join(character.lower() if character.isalnum() else "-" for character in target.stem).strip("-") or "new-study"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "study_id": study_id,
        "base_cfg": str(cfg),
        "output_dir": "runs",
        "parameters": {"mixture_ratio": {"kind": "float", "min": inputs.mixture_ratio, "max": inputs.mixture_ratio, "levels": 1}},
        "sampling": {"method": "grid", "count": 1, "seed": 1, "max_candidates": 100},
        "analysis": {"cea": {"enabled": False}, "cantera": {"enabled": False}},
        "ranking": {"mode": "pareto", "objectives": [
            {"metric": "estimated_delivered.sea_level.specific_impulse_s", "direction": "maximize"},
            {"metric": "derived.characteristic_velocity_efficiency", "direction": "maximize"},
            {"metric": "estimated_delivered.sea_level.thrust_coefficient", "direction": "maximize"},
        ], "constraints": []},
        "design_constraints": [],
    }
    if target.exists():
        raise FileExistsError(target)
    _atomic_write_text(target, json.dumps(payload, indent=2) + "\n")
    load_study_spec(target)
    return target

