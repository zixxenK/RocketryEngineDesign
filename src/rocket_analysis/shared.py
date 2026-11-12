"""Fault-tolerant discovery of RPA4 files in shared folders and inboxes."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rocket_analysis.models import RPA4Inputs
from rocket_analysis.rpa4 import parse_report_values
from rocket_analysis.rpa4_validation import load_rpa4_inputs, validate_rpa4_case


@dataclass(frozen=True)
class SharedRPA4Case:
    """One validated CFG/PDF pair loaded from a shared-folder snapshot."""

    case_name: str
    config_path: Path
    report_path: Path
    inputs: RPA4Inputs
    report_values: dict[str, Any]
    validation: dict[str, Any]


@dataclass(frozen=True)
class ScanProblem:
    path: str
    reason: str
    case_name: str | None = None


@dataclass(frozen=True)
class SharedRPA4Scan:
    cases: tuple[SharedRPA4Case, ...]
    problems: tuple[ScanProblem, ...]
    duplicates: tuple[ScanProblem, ...]
    unpaired: tuple[ScanProblem, ...]


def case_key(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().casefold()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_hash(path: Path) -> str:
    before = path.stat()
    digest = sha256_file(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"File changed while it was being read: {path}")
    return digest


def scan_shared_rpa4_cases(shared_dir: str | Path | None = None) -> SharedRPA4Scan:
    """Scan all files independently and return usable pairs plus problems.

    Identical conflict copies collapse by content hash. Different files with
    the same case name are reported as conflicts and excluded from pairing.
    Bad or partial files never prevent unrelated cases from being read.
    """
    configured_dir = shared_dir if shared_dir is not None else os.environ.get("RPA4_SHARED_DIR")
    if configured_dir is None or not str(configured_dir).strip():
        raise ValueError(
            "Provide shared_dir or set RPA4_SHARED_DIR to the RPA4 export or return folder."
        )
    root = Path(configured_dir).expanduser()
    if not root.is_dir():
        raise NotADirectoryError(f"RPA4 shared folder is not a directory: {root}")

    problems: list[ScanProblem] = []
    duplicates: list[ScanProblem] = []
    unpaired: list[ScanProblem] = []
    config_rows: dict[str, tuple[Path, RPA4Inputs, str]] = {}
    report_rows: dict[str, tuple[Path, dict[str, Any], str]] = {}
    conflicted_configs: set[str] = set()
    conflicted_reports: set[str] = set()

    files = sorted(path for path in root.rglob("*") if path.is_file())
    config_paths = [path for path in files if path.suffix.casefold() == ".cfg"]
    report_paths = [path for path in files if path.suffix.casefold() == ".pdf"]

    for path in config_paths:
        try:
            digest = _stable_hash(path)
            inputs = load_rpa4_inputs(path)
        except Exception as error:
            problems.append(ScanProblem(str(path), f"{type(error).__name__}: {error}"))
            continue
        key = case_key(inputs.case_name)
        previous = config_rows.get(key)
        if previous is None:
            config_rows[key] = (path, inputs, digest)
        elif previous[2] == digest:
            duplicates.append(
                ScanProblem(str(path), "Identical CFG content already scanned; copy collapsed.", inputs.case_name)
            )
        else:
            conflicted_configs.add(key)
            config_rows.pop(key, None)
            problems.append(
                ScanProblem(
                    str(path),
                    f"Different CFG contents use the same case name as {previous[0]}.",
                    inputs.case_name,
                )
            )

    for path in report_paths:
        try:
            digest = _stable_hash(path)
            values = parse_report_values(path)
        except Exception as error:
            problems.append(ScanProblem(str(path), f"{type(error).__name__}: {error}"))
            continue
        name = str(values.get("case_name", ""))
        key = case_key(name)
        previous = report_rows.get(key)
        if previous is None:
            report_rows[key] = (path, values, digest)
        elif previous[2] == digest:
            duplicates.append(ScanProblem(str(path), "Identical PDF content already scanned; copy collapsed.", name))
        else:
            conflicted_reports.add(key)
            report_rows.pop(key, None)
            problems.append(
                ScanProblem(
                    str(path),
                    f"Different PDF contents use the same case name as {previous[0]}.",
                    name,
                )
            )

    for key in sorted(set(config_rows) - set(report_rows) | conflicted_configs):
        row = config_rows.get(key)
        name = row[1].case_name if row else key
        unpaired.append(ScanProblem(str(row[0]) if row else str(root), "CFG has no unambiguous matching PDF.", name))
    for key in sorted(set(report_rows) - set(config_rows) | conflicted_reports):
        row = report_rows.get(key)
        name = str(row[1].get("case_name")) if row else key
        unpaired.append(ScanProblem(str(row[0]) if row else str(root), "PDF has no unambiguous matching CFG.", name))

    cases: list[SharedRPA4Case] = []
    for key in sorted(set(config_rows) & set(report_rows) - conflicted_configs - conflicted_reports):
        config_path, inputs, config_digest = config_rows[key]
        report_path, report_values, report_digest = report_rows[key]
        if config_path.parent != report_path.parent:
            problems.append(
                ScanProblem(
                    str(report_path),
                    f"CFG and PDF for {inputs.case_name!r} are in different folders.",
                    inputs.case_name,
                )
            )
            continue
        try:
            validation = validate_rpa4_case(config_path, report_path)
            if _stable_hash(config_path) != config_digest or _stable_hash(report_path) != report_digest:
                raise RuntimeError("File changed during validation")
        except Exception as error:
            problems.append(ScanProblem(str(report_path), f"{type(error).__name__}: {error}", inputs.case_name))
            continue
        cases.append(
            SharedRPA4Case(
                case_name=inputs.case_name,
                config_path=config_path,
                report_path=report_path,
                inputs=inputs,
                report_values=report_values,
                validation=validation,
            )
        )

    if not files:
        problems.append(ScanProblem(str(root), "Folder is empty."))
    elif not config_paths and not report_paths:
        problems.append(ScanProblem(str(root), "No RPA4 CFG or PDF files found."))
    elif not report_paths:
        problems.append(ScanProblem(str(root), "No RPA4 PDF reports found yet; nothing to import."))
    return SharedRPA4Scan(tuple(cases), tuple(problems), tuple(duplicates), tuple(unpaired))


def load_shared_rpa4_cases(
    shared_dir: str | Path | None = None,
    *,
    allow_incomplete: bool = False,
) -> tuple[SharedRPA4Case, ...]:
    """Compatibility reader for complete pairs.

    With ``allow_incomplete=True``, return every usable pair and leave callers
    free to inspect detailed scan issues using :func:`scan_shared_rpa4_cases`.
    An empty or not-yet-returned folder is a valid empty scan in this mode.
    """
    scan = scan_shared_rpa4_cases(shared_dir)
    if not allow_incomplete and scan.problems:
        first = scan.problems[0]
        if "Different CFG contents" in first.reason:
            raise ValueError(f"Multiple CFG files use the case name {first.case_name!r}: {first.path}")
        if "different folders" in first.reason:
            case = first.case_name or "unknown"
            raise ValueError(f"CFG and PDF for case {case!r} must be in the same folder: {first.path}")
        if "No RPA4 PDF reports found yet" in first.reason:
            raise ValueError(f"No RPA4 .pdf reports found yet in the shared folder: {first.path}")
        raise ValueError(f"RPA4 scan found {len(scan.problems)} problem(s); first: {first.path}: {first.reason}")
    if not allow_incomplete and scan.unpaired:
        first = scan.unpaired[0]
        if "PDF" in first.reason:
            raise ValueError(f"No matching RPA4 .cfg found for case {first.case_name!r}: {first.path}")
        raise ValueError(f"No matching RPA4 .pdf report found for case {first.case_name!r}: {first.path}")
    return scan.cases
