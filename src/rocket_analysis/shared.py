"""Read transferred RPA4 CFG/PDF pairs from a shared directory."""

from __future__ import annotations

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
    """One CFG and PDF pair loaded from the current shared-folder snapshot."""

    case_name: str
    config_path: Path
    report_path: Path
    inputs: RPA4Inputs
    report_values: dict[str, Any]
    validation: dict[str, Any]


def _case_key(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().casefold()


def _signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _check_stable(path: Path, initial: tuple[int, int]) -> None:
    if _signature(path) != initial:
        raise RuntimeError(
            f"RPA4 file changed while it was being read: {path}. "
            "Publish completed files atomically, then reload the shared folder."
        )


def load_shared_rpa4_cases(
    shared_dir: str | Path | None = None,
    *,
    allow_incomplete: bool = False,
) -> tuple[SharedRPA4Case, ...]:
    """Load complete CFG/PDF pairs from a shared folder.

    If ``shared_dir`` is omitted, ``RPA4_SHARED_DIR`` is used. Pairing is by
    normalized RPA4 case name, not filename. Keep one current CFG and one PDF
    per case name in the active directory, with each pair in the same folder.
    Calling this function again reads the latest files, which makes it suitable
    for notebook refreshes. ``allow_incomplete`` is for batch imports where
    some staged CFGs are still awaiting reports; unmatched files are skipped.
    """
    configured_dir = (
        shared_dir if shared_dir is not None else os.environ.get("RPA4_SHARED_DIR")
    )
    if configured_dir is None or not str(configured_dir).strip():
        raise ValueError(
            "Provide shared_dir or set the RPA4_SHARED_DIR environment variable "
            "to the folder containing RPA4 .cfg and .pdf exports."
        )
    root = Path(configured_dir).expanduser()
    if not root.is_dir():
        raise NotADirectoryError(f"RPA4 shared folder does not exist or is not a directory: {root}")

    files = sorted(path for path in root.rglob("*") if path.is_file())
    config_paths = [path for path in files if path.suffix.casefold() == ".cfg"]
    report_paths = [path for path in files if path.suffix.casefold() == ".pdf"]
    if not config_paths:
        raise ValueError(f"No RPA4 .cfg files found under {root}")
    if not report_paths:
        raise ValueError(f"No RPA4 .pdf reports found under {root}")

    configs: dict[str, tuple[Path, RPA4Inputs, tuple[int, int]]] = {}
    for path in config_paths:
        initial = _signature(path)
        inputs = load_rpa4_inputs(path)
        _check_stable(path, initial)
        key = _case_key(inputs.case_name)
        if key in configs:
            raise ValueError(
                f"Multiple CFG files use the case name {inputs.case_name!r}: "
                f"{configs[key][0]} and {path}. Give each candidate a unique RPA4 case name."
            )
        configs[key] = (path, inputs, initial)

    reports: dict[str, tuple[Path, dict[str, Any], tuple[int, int]]] = {}
    for path in report_paths:
        initial = _signature(path)
        values = parse_report_values(path)
        _check_stable(path, initial)
        key = _case_key(values["case_name"])
        if key in reports:
            raise ValueError(
                f"Multiple PDF reports use the case name {values['case_name']!r}: "
                f"{reports[key][0]} and {path}. Keep one current report per case name."
            )
        reports[key] = (path, values, initial)

    missing_reports = sorted(set(configs) - set(reports))
    orphan_reports = sorted(set(reports) - set(configs))
    if (missing_reports or orphan_reports) and not allow_incomplete:
        details = []
        if missing_reports:
            details.append("CFG files without matching PDFs: " + ", ".join(missing_reports))
        if orphan_reports:
            details.append("PDF reports without matching CFG files: " + ", ".join(orphan_reports))
        raise ValueError("RPA4 shared-folder pairs do not match by case name. " + "; ".join(details))

    complete_keys = set(configs) & set(reports)
    if not complete_keys:
        raise ValueError(f"No complete matching RPA4 CFG/PDF pairs found under {root}")

    loaded: list[SharedRPA4Case] = []
    for key in sorted(complete_keys):
        config_path, inputs, config_signature = configs[key]
        report_path, report_values, report_signature = reports[key]
        if config_path.parent != report_path.parent:
            raise ValueError(
                f"CFG and PDF for case {inputs.case_name!r} must be in the same folder: "
                f"{config_path} and {report_path}"
            )
        validation = validate_rpa4_case(config_path, report_path)
        _check_stable(config_path, config_signature)
        _check_stable(report_path, report_signature)
        loaded.append(
            SharedRPA4Case(
                case_name=inputs.case_name,
                config_path=config_path,
                report_path=report_path,
                inputs=inputs,
                report_values=report_values,
                validation=validation,
            )
        )
    return tuple(loaded)
