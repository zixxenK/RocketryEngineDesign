"""Atomic RPA4 packs, safe inbox unpacking, and return scanning."""
from __future__ import annotations

import hashlib
import html
import json
import os
import shutil
import stat
import tempfile
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from rocket_analysis.rpa4 import parse_report_values
from rocket_analysis.rpa4_validation import load_rpa4_config, validate_rpa4_case
from rocket_analysis.reports import stable_report_id
from rocket_analysis.shared import case_key, sha256_file
from rocket_analysis.study import (
    SCHEMA_VERSION,
    StudySpec,
    _atomic_write_text,
    _candidate_directories,
    design_constraint_violations,
)

MAX_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024
MAX_BUNDLE_MEMBERS = 20_000


@dataclass(frozen=True)
class WorkflowProblem:
    path: str
    reason: str
    case_name: str | None = None


@dataclass(frozen=True)
class ReturnScanResult:
    imported_candidate_ids: tuple[str, ...]
    duplicates: tuple[WorkflowProblem, ...]
    unpaired: tuple[WorkflowProblem, ...]
    problems: tuple[WorkflowProblem, ...]
    results_path: Path
    imported_report_ids: tuple[str, ...] = ()


def _manifest_rows(spec: StudySpec) -> tuple[tuple[Path, dict[str, Any]], ...]:
    rows = []
    for directory in _candidate_directories(spec):
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            if int(manifest.get("schema_version", SCHEMA_VERSION)) == SCHEMA_VERSION:
                rows.append((directory, manifest))
        except Exception:
            continue
    return tuple(rows)


def _archive_verified(source: Path, destination: Path, expected_digest: str) -> None:
    """Copy one import artifact atomically and verify it against the scan hash."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and sha256_file(destination) == expected_digest:
        return
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(fd)
    try:
        shutil.copyfile(source, temporary_name)
        if sha256_file(temporary_name) != expected_digest:
            raise RuntimeError(f"File changed before it could be archived: {source}")
        os.replace(temporary_name, destination)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _run_sheet_html(study_id: str, candidates: list[dict[str, Any]]) -> str:
    rows = "".join(
        "<tr><td>" + html.escape(str(row["case_name"])) + "</td><td>"
        + html.escape(str(row["candidate_id"]))
        + "</td><td>Run candidate.cfg in RPA4 without editing or re-saving; export candidate.pdf into this case folder.</td><td></td></tr>"
        for row in candidates
    )
    return (
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>RPA4 run sheet</title>"
        "<style>body{font:14px Segoe UI,Arial;margin:32px;color:#1b1f24}table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #c8cdd4;padding:8px;text-align:left}th{background:#eef0f3}"
        ".note{padding:12px;background:#fff2cc}</style><body><h1>RPA4 run sheet: "
        + html.escape(study_id)
        + "</h1><p class=\"note\">Do not edit or re-save CFG files. Record load errors, warnings, and changed values.</p>"
        "<table><thead><tr><th>Case</th><th>Candidate</th><th>Procedure</th><th>Notes</th></tr></thead><tbody>"
        + rows
        + "</tbody></table></body></html>"
    )


def pack_study_candidates(
    spec: StudySpec,
    output_path: str | Path,
    *,
    candidate_ids: set[str] | tuple[str, ...] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> Path:
    """Build an atomic ZIP or folder pack with cases, hashes, and run sheets."""
    destination = Path(output_path).expanduser().resolve()
    if destination.exists():
        raise FileExistsError(destination)
    requested = set(candidate_ids) if candidate_ids is not None else None
    selected = [
        (directory, manifest)
        for directory, manifest in _manifest_rows(spec)
        if requested is None or manifest.get("candidate_id") in requested
    ]
    if requested is not None:
        found = {str(manifest.get("candidate_id")) for _, manifest in selected}
        missing = sorted(requested - found)
        if missing:
            raise ValueError("Unknown candidate ID(s): " + ", ".join(missing))
    if not selected:
        raise ValueError("No candidate folders are available to pack")
    infeasible = [
        (str(manifest.get("candidate_id")), design_constraint_violations(spec, manifest.get("parameters", {})))
        for _, manifest in selected
    ]
    infeasible = [(candidate_id, reasons) for candidate_id, reasons in infeasible if reasons]
    if infeasible:
        details = "; ".join(f"{candidate_id}: {', '.join(reasons)}" for candidate_id, reasons in infeasible)
        raise ValueError("Infeasible candidates cannot be packed: " + details)
    hashes: dict[str, str] = {}
    sent_at = datetime.now(timezone.utc).isoformat()
    manifests = []
    for directory, original in selected:
        sent = dict(original)
        sent.update({"status": "sent", "sent_at": sent_at})
        config_name = str(sent.get("config_file", "candidate.cfg"))
        if Path(config_name).name != config_name or not (directory / config_name).is_file():
            raise FileNotFoundError(f"Candidate CFG is missing or unsafe: {directory / config_name}")
        manifests.append(sent)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.suffix.casefold() != ".zip":
        temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
        try:
            for (directory, _), manifest in zip(selected, manifests, strict=True):
                if cancel_check:
                    cancel_check()
                case_name = str(manifest["case_name"])
                if Path(case_name).name != case_name or case_name in {".", ".."}:
                    raise ValueError(f"Unsafe RPA4 case name: {case_name!r}")
                config_name = str(manifest.get("config_file", "candidate.cfg"))
                if Path(config_name).name != config_name or config_name in {".", ".."}:
                    raise ValueError(f"Unsafe CFG filename in candidate manifest: {config_name!r}")
                case_directory = temporary / case_name
                case_directory.mkdir(parents=True)
                for filename in (config_name, "manifest.json", "screening.json"):
                    if cancel_check:
                        cancel_check()
                    source = directory / filename
                    if not source.is_file():
                        continue
                    target = case_directory / filename
                    if filename == "manifest.json":
                        contents = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
                        target.write_bytes(contents)
                    else:
                        shutil.copyfile(source, target)
                        contents = target.read_bytes()
                    hashes[f"{case_name}/{filename}"] = hashlib.sha256(contents).hexdigest()
            (temporary / "RUN-SHEET.html").write_text(_run_sheet_html(spec.study_id, manifests), encoding="utf-8")
            (temporary / "RUN-SHEET.md").write_text(
                "# RPA4 run sheet\n\nDo not edit or re-save CFG files. Run each case and save its PDF as candidate.pdf in the matching case folder. Record errors, warnings, and changed values.\n",
                encoding="utf-8",
            )
            (temporary / "pack-manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "study_id": spec.study_id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "files": hashes,
                        "candidates": manifests,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, destination)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        try:
            for (directory, _), manifest in zip(selected, manifests, strict=True):
                _atomic_write_text(directory / "manifest.json", json.dumps(manifest, indent=2) + "\n")
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        return destination

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}.", suffix=".zip.tmp", dir=destination.parent
    )
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary_name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for (directory, original), manifest in zip(selected, manifests, strict=True):
                if cancel_check:
                    cancel_check()
                case_name = str(manifest["case_name"])
                if Path(case_name).name != case_name or case_name in {".", ".."}:
                    raise ValueError(f"Unsafe RPA4 case name: {case_name!r}")
                config_name = str(manifest.get("config_file", "candidate.cfg"))
                if Path(config_name).name != config_name or config_name in {".", ".."}:
                    raise ValueError(f"Unsafe CFG filename in candidate manifest: {config_name!r}")
                names = {config_name, "manifest.json", "screening.json"}
                for filename in sorted(names):
                    if cancel_check:
                        cancel_check()
                    source = directory / filename
                    if source.is_file():
                        archive_name = f"{case_name}/{filename}"
                        hashes[archive_name] = sha256_file(source)
                        if filename == "manifest.json":
                            payload = json.dumps(manifest, indent=2) + "\n"
                            hashes[archive_name] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                            archive.writestr(archive_name, payload)
                        else:
                            archive.write(source, arcname=archive_name)
            archive.writestr("RUN-SHEET.html", _run_sheet_html(spec.study_id, manifests))
            archive.writestr(
                "RUN-SHEET.md",
                "# RPA4 run sheet\n\nDo not edit or re-save CFG files. Run each case and save its PDF as candidate.pdf in the matching case folder. Record errors, warnings, and changed values.\n",
            )
            archive.writestr(
                "pack-manifest.json",
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "study_id": spec.study_id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "files": hashes,
                        "candidates": manifests,
                    },
                    indent=2,
                )
                + "\n",
            )
        os.replace(temporary_name, destination)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    # Persist sent state only after the complete archive has been atomically
    # installed at its destination.
    try:
        for (directory, _), manifest in zip(selected, manifests, strict=True):
            _atomic_write_text(directory / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def unpack_rpa4_bundle(
    bundle_path: str | Path,
    inbox_dir: str | Path,
    *,
    cancel_check: Callable[[], None] | None = None,
) -> Path:
    """Safely extract a zip into a content-addressed inbox folder."""
    source = Path(bundle_path).expanduser().resolve()
    if not source.is_file() or source.suffix.casefold() != ".zip":
        raise FileNotFoundError(f"RPA4 return bundle is not a .zip file: {source}")
    digest = sha256_file(source)
    inbox = Path(inbox_dir).expanduser().resolve()
    target = inbox / digest[:16]
    if target.exists():
        return target
    inbox.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".incoming-", dir=inbox))
    total = 0
    try:
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if len(members) > MAX_BUNDLE_MEMBERS:
                raise ValueError(f"Bundle has more than {MAX_BUNDLE_MEMBERS} entries")
            for member in members:
                if cancel_check:
                    cancel_check()
                relative = PurePosixPath(member.filename.replace("\\", "/"))
                mode = member.external_attr >> 16
                if relative.is_absolute() or any(part in {"..", ""} for part in relative.parts):
                    raise ValueError(f"Bundle contains an unsafe path: {member.filename!r}")
                if relative.parts and ":" in relative.parts[0]:
                    raise ValueError(f"Bundle contains a drive path: {member.filename!r}")
                if stat.S_ISLNK(mode):
                    raise ValueError(f"Bundle contains a symbolic link: {member.filename!r}")
                total += member.file_size
                if total > MAX_BUNDLE_BYTES:
                    raise ValueError("Bundle expands beyond the 2 GiB safety limit")
                output = temporary.joinpath(*relative.parts)
                if member.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as input_stream, output.open("wb") as output_stream:
                    shutil.copyfileobj(input_stream, output_stream)
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def scan_returns(
    spec: StudySpec,
    source: str | Path | None = None,
    *,
    cancel_check: Callable[[], None] | None = None,
) -> ReturnScanResult:
    """Scan return PDFs by case name, archive content, and report recoverable issues."""
    configured = source if source is not None else os.environ.get("RPA4_SHARED_DIR")
    if configured is None or not str(configured).strip():
        raise ValueError("Provide a return folder or set RPA4_SHARED_DIR")
    source_path = Path(configured).expanduser().resolve()
    bundle_name: str | None = None
    if source_path.is_file() and source_path.suffix.casefold() == ".zip":
        root = unpack_rpa4_bundle(
            source_path,
            spec.output_dir / spec.study_id / "inbox",
            cancel_check=cancel_check,
        )
        bundle_name = source_path.name
    elif source_path.is_dir():
        root = source_path
    elif source_path.is_file() and source_path.suffix.casefold() == ".pdf":
        root = source_path.parent
    else:
        raise FileNotFoundError(f"Return source is not a folder, PDF, or zip: {source_path}")

    manifests: dict[str, tuple[Path, dict[str, Any]]] = {}
    problems: list[WorkflowProblem] = []
    ambiguous_manifests: set[str] = set()
    for directory, manifest in _manifest_rows(spec):
        key = case_key(str(manifest.get("case_name", "")))
        if not key:
            continue
        if key in manifests and manifests[key][1].get("candidate_id") != manifest.get("candidate_id"):
            problems.append(WorkflowProblem(str(directory), "Multiple project candidates use this case name.", key))
            manifests.pop(key, None)
            ambiguous_manifests.add(key)
        elif key not in manifests:
            if key not in ambiguous_manifests:
                manifests[key] = (directory, manifest)

    pdfs = [source_path] if source_path.is_file() and source_path.suffix.casefold() == ".pdf" else sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.casefold() == ".pdf"
    )
    if not pdfs:
        problems.append(WorkflowProblem(str(root), "No RPA4 PDF reports found yet; nothing to import."))
    parsed: dict[str, tuple[Path, dict[str, Any], str]] = {}
    duplicates: list[WorkflowProblem] = []
    unpaired: list[WorkflowProblem] = []
    for path in pdfs:
        if cancel_check:
            cancel_check()
        try:
            before = path.stat()
            digest = sha256_file(path)
            values = parse_report_values(path)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise RuntimeError("PDF changed while it was being parsed")
        except Exception as error:
            problems.append(WorkflowProblem(str(path), f"{type(error).__name__}: {error}"))
            continue
        name = str(values.get("case_name", ""))
        key = case_key(name)
        if digest in parsed:
            duplicates.append(WorkflowProblem(str(path), "Identical report already scanned; copy collapsed.", name))
        else:
            parsed[digest] = (path, values, digest)

    result_path = spec.output_dir / spec.study_id / "rpa4-results.json"
    previous_payload = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
    by_id = {row["candidate_id"]: row for row in previous_payload.get("results", [])}
    history_by_hash: dict[str, dict[str, Any]] = {}
    for row in [*previous_payload.get("report_history", []), *by_id.values()]:
        if isinstance(row, dict) and row.get("report_sha256"):
            history_by_hash[str(row["report_sha256"])] = row
    newly_imported: list[str] = []
    newly_imported_reports: list[str] = []
    imports_dir = spec.output_dir / spec.study_id / "imports"
    for digest, (report_path, values, digest) in sorted(parsed.items(), key=lambda item: (item[1][0].stat().st_mtime_ns, str(item[1][0]).casefold())):
        if cancel_check:
            cancel_check()
        name = str(values.get("case_name", ""))
        key = case_key(name)
        match = None if key in ambiguous_manifests else manifests.get(key)
        if match is None:
            unpaired.append(WorkflowProblem(str(report_path), "No staged project candidate has this RPA4 case name.", name))
            continue
        candidate_dir, manifest = match
        candidate_id = str(manifest["candidate_id"])
        if digest in history_by_hash:
            duplicates.append(WorkflowProblem(str(report_path), "Report hash is already imported.", name))
            continue
        cfg_path = candidate_dir / str(manifest.get("config_file", "candidate.cfg"))
        if not cfg_path.is_file():
            problems.append(WorkflowProblem(str(cfg_path), "Staged project CFG is missing.", name))
            continue
        try:
            validation = validate_rpa4_case(cfg_path, report_path)
        except Exception as error:
            problems.append(WorkflowProblem(str(report_path), f"Validation failed: {type(error).__name__}: {error}", name))
            continue

        cfg_audit: dict[str, Any] | None = None
        returned_cfg: Path | None = None
        for possible_cfg in sorted(report_path.parent.glob("*.cfg")):
            try:
                returned = load_rpa4_config(possible_cfg)
            except Exception:
                continue
            if case_key(str(returned["engineDefinition"]["name"])) == key:
                returned_cfg = possible_cfg
                break
        if returned_cfg is not None:
            try:
                staged = load_rpa4_config(cfg_path)
                returned = load_rpa4_config(returned_cfg)
                staged["engineDefinition"]["name"] = case_key(staged["engineDefinition"]["name"])
                returned["engineDefinition"]["name"] = case_key(returned["engineDefinition"]["name"])
                cfg_audit = {
                    "status": "match" if staged == returned else "mismatch",
                    "path": str(returned_cfg),
                    "sha256": sha256_file(returned_cfg),
                }
            except Exception as error:
                cfg_audit = {"status": "review_required", "path": str(returned_cfg), "reason": str(error)}

        screening_path = candidate_dir / "screening.json"
        screening = json.loads(screening_path.read_text(encoding="utf-8")) if screening_path.exists() else {}
        from rocket_analysis.ranking import candidate_metric_catalog

        metrics = candidate_metric_catalog(
            values,
            screening,
            design_parameters=manifest.get("parameters", {}),
            design_units=manifest.get("parameter_units", {}),
            separation_pressure_ratio_limit=spec.analysis.get("separation_pressure_ratio_limit"),
        )
        report_id = stable_report_id(str(spec.source_path), candidate_id, digest)
        import_dir = imports_dir / report_id
        import_dir.mkdir(parents=True, exist_ok=True)
        archived_pdf = import_dir / "report.pdf"
        try:
            _archive_verified(report_path, archived_pdf, digest)
        except Exception as error:
            problems.append(WorkflowProblem(str(report_path), f"Archive failed: {type(error).__name__}: {error}", name))
            continue
        archived_cfg: Path | None = None
        cfg_digest: str | None = None
        if returned_cfg is not None:
            cfg_digest = sha256_file(returned_cfg)
            archived_cfg = import_dir / f"returned-{cfg_digest[:16]}.cfg"
            try:
                _archive_verified(returned_cfg, archived_cfg, cfg_digest)
            except Exception as error:
                problems.append(WorkflowProblem(str(returned_cfg), f"CFG archive failed: {type(error).__name__}: {error}", name))
                continue
        imported_at = datetime.now(timezone.utc).isoformat()
        provenance = {
            "schema_version": SCHEMA_VERSION,
            "report_id": report_id,
            "candidate_id": candidate_id,
            "case_name": name,
            "imported_at": imported_at,
            "source_path": str(report_path),
            "bundle_name": bundle_name,
            "report_sha256": digest,
            "archived_report": str(archived_pdf),
            "returned_cfg_path": str(returned_cfg) if returned_cfg else None,
            "returned_cfg_sha256": cfg_digest,
            "archived_returned_cfg": str(archived_cfg) if archived_cfg else None,
        }
        _atomic_write_text(import_dir / "provenance.json", json.dumps(provenance, indent=2) + "\n")
        row = {
            "schema_version": SCHEMA_VERSION,
            "study_id": spec.study_id,
            "report_id": report_id,
            "candidate_id": candidate_id,
            "case_name": name,
            "parameters": manifest.get("parameters", {}),
            "parameter_units": manifest.get("parameter_units", {}),
            "optuna_trial_number": manifest.get("optuna_trial_number"),
            "validation_status": validation["status"],
            "validation": validation,
            "returned_cfg_audit": cfg_audit,
            "config_path": str(cfg_path),
            "report_path": str(archived_pdf),
            "report_sha256": digest,
            "screening": screening,
            "report_values": values,
            "metrics": metrics,
            "provenance": provenance,
        }
        by_id[candidate_id] = row
        history_by_hash[digest] = row
        newly_imported.append(candidate_id)
        newly_imported_reports.append(report_id)
        manifest["status"] = "rpa4_imported"
        manifest["validation_status"] = validation["status"]
        _atomic_write_text(candidate_dir / "manifest.json", json.dumps(manifest, indent=2) + "\n")

    payload = {
        "schema_version": SCHEMA_VERSION,
        "study_id": spec.study_id,
        "sampling_method": spec.sampling_method,
        "last_scanned_at": datetime.now(timezone.utc).isoformat(),
        "results": sorted(by_id.values(), key=lambda row: row["candidate_id"]),
        "report_history": sorted(history_by_hash.values(), key=lambda row: (str((row.get("provenance") or {}).get("imported_at", "")), str(row.get("report_id", "")))),
        "last_imported_candidate_ids": list(dict.fromkeys(newly_imported)),
        "last_imported_report_ids": newly_imported_reports,
        "scan_problems": [asdict(item) for item in problems],
        "duplicates": [asdict(item) for item in duplicates],
        "unpaired": [asdict(item) for item in unpaired],
    }
    _atomic_write_text(result_path, json.dumps(payload, indent=2) + "\n")
    return ReturnScanResult(
        tuple(dict.fromkeys(newly_imported)), tuple(duplicates), tuple(unpaired), tuple(problems), result_path,
        tuple(newly_imported_reports),
    )

