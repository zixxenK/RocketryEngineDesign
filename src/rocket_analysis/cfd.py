"""Solver-neutral CFD result ingestion."""

from __future__ import annotations

import csv
import json
import math
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Any, Callable

from rocket_analysis.models import MachComparison, MachStation


def run_openfoam_case(
    case_path: str,
    *,
    distro: str = "Ubuntu-24.04",
    run_script: str = "Allrun",
    timeout: float | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an OpenFOAM case script inside the selected WSL distribution.

    ``case_path`` must be an absolute Linux path in the WSL filesystem.
    """
    if os.name == "nt" and (case_path.casefold().startswith("\\\\wsl$\\") or case_path.casefold().startswith("\\\\wsl.localhost\\")):
        converted = subprocess.run(
            ["wsl.exe", "-d", distro, "--", "wslpath", "-u", case_path],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        case_path = converted.stdout.strip()
    if not case_path.startswith("/"):
        raise ValueError("case_path must be an absolute Linux path or a WSL UNC path")
    if Path(run_script).name != run_script or run_script in {".", ".."}:
        raise ValueError("run_script must be a file name within the case directory")
    command = f"cd -- {shlex.quote(case_path)} && ./{shlex.quote(run_script)}"
    arguments = ["wsl.exe", "-d", distro, "--", "openfoam2512", "-c", command]
    process = subprocess.Popen(
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        wait_for = 0.25
        if deadline is not None:
            wait_for = max(0.01, min(wait_for, deadline - time.monotonic()))
        try:
            stdout, stderr = process.communicate(timeout=wait_for)
            break
        except subprocess.TimeoutExpired:
            try:
                if cancel_check:
                    cancel_check()
            except Exception:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise
            if deadline is not None and time.monotonic() >= deadline:
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                raise subprocess.TimeoutExpired(arguments, timeout, output=stdout, stderr=stderr)
    completed = subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, arguments, output=stdout, stderr=stderr)
    return completed


def openfoam_finalist_ids(ranking_result: Any, *, limit: int = 3) -> tuple[str, ...]:
    """Select the first ranked candidates for expensive CFD follow-up."""
    if limit < 1:
        raise ValueError("Finalist limit must be at least one")
    entries = tuple(getattr(ranking_result, "entries", ()))
    if not entries:
        return ()
    ordered = sorted(entries, key=lambda entry: (entry.rank, entry.candidate_id))
    return tuple(str(entry.candidate_id) for entry in ordered[:limit])


def prepare_openfoam_finalist_case(
    template_dir: str | Path,
    destination: str | Path,
    *,
    candidate_id: str,
    finalist_ids: tuple[str, ...],
    replacements: dict[str, str | int | float],
    cancel_check: Callable[[], None] | None = None,
) -> Path:
    """Copy a user-supplied OpenFOAM template and substitute its tokens.

    This expects an appropriate, independently reviewed OpenFOAM case
    template. Tokens use ``{{name}}`` syntax. The function does not create
    boundary conditions or claim that a case is physically validated.
    """
    if candidate_id not in finalist_ids:
        raise ValueError(f"Candidate {candidate_id!r} is not in the selected CFD finalist set")
    source = Path(template_dir).resolve()
    raw_destination = str(destination)
    if os.name == "nt":
        windows_destination = PureWindowsPath(raw_destination)
        normalized_destination = raw_destination.casefold()
        if not (normalized_destination.startswith("\\\\wsl$\\") or normalized_destination.startswith("\\\\wsl.localhost\\")):
            raise ValueError("OpenFOAM finalist cases must be stored under a WSL UNC path (\\\\wsl$\\<distro>\\...).")
        if len(windows_destination.parts) < 2:
            raise ValueError("WSL destination must name a distribution and a directory")
    target = Path(destination).resolve()
    if not source.is_dir():
        raise NotADirectoryError(f"OpenFOAM template directory not found: {source}")
    if target.exists():
        raise FileExistsError(f"Finalist case destination already exists: {target}")
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("Template and finalist destination must be separate directories")

    tokens = {"{{" + str(name) + "}}": str(value) for name, value in replacements.items()}
    if not tokens:
        raise ValueError("At least one template replacement is required")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    temporary_target = temporary_root / target.name
    seen: set[str] = set()
    try:
        def copy_with_checkpoint(source_file: str, destination_file: str):
            if cancel_check:
                cancel_check()
            return shutil.copy2(source_file, destination_file)

        shutil.copytree(source, temporary_target, copy_function=copy_with_checkpoint)
        for path in temporary_target.rglob("*"):
            if cancel_check:
                cancel_check()
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for token, value in tokens.items():
                if token in text:
                    seen.add(token)
                    text = text.replace(token, value)
            path.write_text(text, encoding="utf-8")
        missing = sorted(set(tokens) - seen)
        if missing:
            raise ValueError("Template did not contain replacement token(s): " + ", ".join(missing))
        manifest = {
            "candidate_id": candidate_id,
            "template_directory": str(source),
            "replaced_tokens": sorted(seen),
            "status": "prepared_for_user_review",
            "validation_claim": "none; supply appropriate physics, mesh, and boundary conditions",
        }
        (temporary_target / "rocket-analysis-finalist.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary_target, target)
    except Exception:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise
    else:
        shutil.rmtree(temporary_root, ignore_errors=True)
    return target


def run_openfoam_finalist_case(
    candidate_id: str,
    finalist_ids: tuple[str, ...],
    case_path: str,
    cancel_check: Callable[[], None] | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run an existing case only when its candidate is in the finalist set."""
    if candidate_id not in finalist_ids:
        raise ValueError(f"Candidate {candidate_id!r} is not in the selected CFD finalist set")
    return run_openfoam_case(case_path, cancel_check=cancel_check, **kwargs)


def read_station_csv(path: str | Path) -> tuple[MachStation, ...]:
    """Read a simple solver-neutral CSV containing station,mach columns."""
    stations: list[MachStation] = []
    names: set[str] = set()
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not {"station", "mach"}.issubset(reader.fieldnames):
            raise ValueError("CSV must contain 'station' and 'mach' columns")
        for row in reader:
            raw_name = row.get("station")
            raw_mach = row.get("mach")
            if raw_name is None or not raw_name.strip():
                raise ValueError("Each CSV record must have a non-empty station name")
            try:
                mach = float(raw_mach)
            except (TypeError, ValueError) as error:
                raise ValueError(f"Invalid Mach value for station {raw_name!r}") from error
            if not math.isfinite(mach) or mach < 0.0:
                raise ValueError(f"Mach value for station {raw_name!r} must be finite and non-negative")
            station_key = raw_name.strip().casefold()
            if station_key in names:
                raise ValueError(f"CSV contains duplicate station name: {raw_name.strip()}")
            names.add(station_key)
            stations.append(
                MachStation(
                    name=raw_name.strip(),
                    mach=mach,
                    confidence="solver-result",
                )
            )
    if not stations:
        raise ValueError("No station records found")
    return tuple(stations)


def compare_station_mach(
    reference: tuple[MachStation, ...], result: tuple[MachStation, ...]
) -> tuple[MachComparison, ...]:
    """Compare station Mach values by case-insensitive station name."""
    if not reference:
        raise ValueError("At least one reference station is required")
    if not result:
        raise ValueError("At least one CFD result station is required")

    def index_stations(points: tuple[MachStation, ...], group: str) -> dict[str, MachStation]:
        indexed: dict[str, MachStation] = {}
        for point in points:
            name = point.name.strip()
            if not name:
                raise ValueError(f"{group} contains an empty station name")
            try:
                valid_mach = math.isfinite(point.mach) and point.mach >= 0.0
            except TypeError:
                valid_mach = False
            if not valid_mach:
                raise ValueError(f"Mach value for {name!r} must be finite and non-negative")
            key = name.casefold()
            if key in indexed:
                raise ValueError(f"{group} contains duplicate station name: {name}")
            indexed[key] = point
        return indexed

    reference_by_name = index_stations(reference, "Reference")
    result_by_name = index_stations(result, "CFD result")
    missing = [
        point.name
        for key, point in reference_by_name.items()
        if key not in result_by_name
    ]
    if missing:
        raise ValueError(f"CFD result is missing reference stations: {', '.join(missing)}")
    return tuple(
        MachComparison(
            name=point.name.strip(),
            reference_mach=point.mach,
            result_mach=result_by_name[point.name.strip().casefold()].mach,
            delta_mach=result_by_name[point.name.strip().casefold()].mach - point.mach,
            reference_confidence=point.confidence,
        )
        for point in reference
    )
