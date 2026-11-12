"""Solver-neutral CFD result ingestion."""

from __future__ import annotations

import csv
import json
import math
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from rocket_analysis.models import MachComparison, MachStation


def run_openfoam_case(
    case_path: str,
    *,
    distro: str = "Ubuntu-24.04",
    run_script: str = "Allrun",
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an OpenFOAM case script inside the selected WSL distribution.

    ``case_path`` must be an absolute Linux path in the WSL filesystem.
    """
    if not case_path.startswith("/"):
        raise ValueError("case_path must be an absolute Linux path")
    if Path(run_script).name != run_script or run_script in {".", ".."}:
        raise ValueError("run_script must be a file name within the case directory")
    command = f"cd -- {shlex.quote(case_path)} && ./{shlex.quote(run_script)}"
    return subprocess.run(
        ["wsl.exe", "-d", distro, "--", "openfoam2512", "-c", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


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
) -> Path:
    """Copy a user-supplied OpenFOAM template and substitute its tokens.

    This expects an appropriate, independently reviewed OpenFOAM case
    template. Tokens use ``{{name}}`` syntax. The function does not create
    boundary conditions or claim that a case is physically validated.
    """
    if candidate_id not in finalist_ids:
        raise ValueError(f"Candidate {candidate_id!r} is not in the selected CFD finalist set")
    source = Path(template_dir).resolve()
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
    shutil.copytree(source, target)
    seen: set[str] = set()
    for path in target.rglob("*"):
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
        shutil.rmtree(target)
        raise ValueError("Template did not contain replacement token(s): " + ", ".join(missing))
    manifest = {
        "candidate_id": candidate_id,
        "template_directory": str(source),
        "replaced_tokens": sorted(seen),
        "status": "prepared_for_user_review",
        "validation_claim": "none; supply appropriate physics, mesh, and boundary conditions",
    }
    (target / "rocket-analysis-finalist.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return target


def run_openfoam_finalist_case(
    candidate_id: str,
    finalist_ids: tuple[str, ...],
    case_path: str,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run an existing case only when its candidate is in the finalist set."""
    if candidate_id not in finalist_ids:
        raise ValueError(f"Candidate {candidate_id!r} is not in the selected CFD finalist set")
    return run_openfoam_case(case_path, **kwargs)


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
