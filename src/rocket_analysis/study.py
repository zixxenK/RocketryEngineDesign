"""Range studies, candidate sampling, and RPA4 CFG generation."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any, Literal

import numpy as np
from scipy.stats import qmc

from rocket_analysis.models import Q_
from rocket_analysis.ranking import (
    DEFAULT_OBJECTIVES,
    RankingResult,
    candidate_metric_catalog,
    rank_metric_rows,
)
from rocket_analysis.rpa4 import parse_report_values
from rocket_analysis.rpa4_validation import load_rpa4_config

SamplingMethod = Literal["grid", "sobol", "adaptive"]
ParameterKind = Literal["float", "integer", "categorical", "boolean"]


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    kind: ParameterKind
    minimum: float | int | None = None
    maximum: float | int | None = None
    levels: int | None = None
    values: tuple[Any, ...] = ()
    unit: str | None = None
    log: bool = False

    @classmethod
    def from_dict(cls, name: str, values: dict[str, Any]) -> "ParameterSpec":
        kind = str(values.get("kind", "float"))
        if kind not in {"float", "integer", "categorical", "boolean"}:
            raise ValueError(f"Unsupported parameter kind for {name!r}: {kind}")
        if kind in {"categorical", "boolean"}:
            choices = values.get("values", [False, True] if kind == "boolean" else None)
            if not isinstance(choices, list) or not choices:
                raise ValueError(f"Parameter {name!r} requires a non-empty values list")
            if kind == "boolean" and any(not isinstance(value, bool) for value in choices):
                raise ValueError(f"Boolean parameter {name!r} values must all be booleans")
            return cls(name=name, kind=kind, values=tuple(choices))

        try:
            minimum = float(values["min"])
            maximum = float(values["max"])
            levels = int(values.get("levels", 5))
            if float(values.get("levels", 5)) != levels:
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ValueError(f"Parameter {name!r} needs numeric min, max, and integer levels") from error
        if not math.isfinite(minimum) or not math.isfinite(maximum) or minimum > maximum:
            raise ValueError(f"Parameter {name!r} must have finite bounds with min <= max")
        if levels < 1:
            raise ValueError(f"Parameter {name!r} levels must be at least one")
        if kind == "integer" and (int(minimum) != minimum or int(maximum) != maximum):
            raise ValueError(f"Integer parameter {name!r} requires integer bounds")
        if bool(values.get("log", False)) and minimum <= 0:
            raise ValueError(f"Log-scaled parameter {name!r} requires a positive minimum")
        return cls(
            name=name,
            kind=kind,
            minimum=int(minimum) if kind == "integer" else minimum,
            maximum=int(maximum) if kind == "integer" else maximum,
            levels=levels,
            unit=values.get("unit"),
            log=bool(values.get("log", False)),
        )


@dataclass(frozen=True)
class StudySpec:
    study_id: str
    source_path: Path
    base_cfg: Path
    output_dir: Path
    parameters: tuple[ParameterSpec, ...]
    sampling_method: SamplingMethod
    sample_count: int
    seed: int
    max_candidates: int
    ranking: dict[str, Any]
    analysis: dict[str, Any]


@dataclass(frozen=True)
class GeneratedCandidate:
    study_id: str
    candidate_id: str
    case_name: str
    directory: Path
    config_path: Path
    manifest_path: Path
    parameters: dict[str, Any]
    optuna_trial_number: int | None = None


_PATHS: dict[str, tuple[tuple[str, ...], tuple[str, ...] | None, str | None]] = {
    "mixture_ratio": (("engineDefinition", "bipropellantMixture", "mixtureRatio", "value"), None, None),
    "chamber_pressure": (("engineDefinition", "chamberPressure", "value"), ("engineDefinition", "chamberPressure", "units"), "pressure"),
    "number_of_chambers": (("thrustChamberSize", "sizing", "numberOfChambers"), None, None),
    "throat_diameter": (("thrustChamberSize", "sizing", "throatDiameter", "value"), ("thrustChamberSize", "sizing", "throatDiameter", "units"), "length"),
    "nozzle_inlet_area_ratio": (("nozzleConditions", "nozzleInletConditions", "areaRatio"), None, None),
    "nozzle_exit_area_ratio": (("nozzleConditions", "nozzleExitConditions", "areaRatio"), None, None),
    "chamber_length": (("thrustChamberSize", "combustionChamberGeometry", "chamberLength", "value", "value"), ("thrustChamberSize", "combustionChamberGeometry", "chamberLength", "value", "units"), "length"),
    "chamber_contraction_angle": (("thrustChamberSize", "combustionChamberGeometry", "contractionAngle"), None, "angle"),
    "r1_to_rt": (("thrustChamberSize", "combustionChamberGeometry", "R1toRt"), None, None),
    "r2_to_r2max": (("thrustChamberSize", "combustionChamberGeometry", "R2toR2max"), None, None),
    "nozzle_throat_radius_to_rt": (("thrustChamberSize", "nozzleGeometry", "RntoRt"), None, None),
    "nozzle_convergence_angle": (("thrustChamberSize", "nozzleGeometry", "parabolicContour", "Tn"), None, "angle"),
    "nozzle_exit_angle": (("thrustChamberSize", "nozzleGeometry", "parabolicContour", "Te"), None, "angle"),
    "ambient_pressure_from": (("nozzleConditions", "ambientConditions", "range", "from", "value"), ("nozzleConditions", "ambientConditions", "range", "from", "units"), "pressure"),
    "ambient_pressure_to": (("nozzleConditions", "ambientConditions", "range", "to", "value"), ("nozzleConditions", "ambientConditions", "range", "to", "units"), "pressure"),
    "ambient_pressure_point_count": (("nozzleConditions", "ambientConditions", "range", "numberOfPoints"), None, None),
    "equilibrium_flow_shifting": (("nozzleConditions", "equilibriumFlow", "shifting"), None, None),
    "reaction_efficiency_estimated": (("nozzleConditions", "reactionEficiency", "estimateReactionEfficiency"), None, None),
    "bell_nozzle_efficiency_estimated": (("nozzleConditions", "nozzleEficiency", "estimateBellNozzleEfficicncy"), None, None),
    "nozzle_efficiency_applied": (("thrustChamberSize", "nozzleGeometry", "applyNozzleEfficiency"), None, None),
    "multiphase": (("nozzleConditions", "nozzleFlowEffcets", "multiphase"), None, None),
}


def load_study_spec(path: str | Path) -> StudySpec:
    """Load and validate a JSON study specification."""
    source = Path(path).resolve()
    data = json.loads(source.read_text(encoding="utf-8"))
    study_id = str(data.get("study_id", "")).strip()
    if not study_id or re.fullmatch(r"[A-Za-z0-9._-]+", study_id) is None:
        raise ValueError("study_id must contain only letters, numbers, dot, underscore, or hyphen")
    base_cfg = (source.parent / data.get("base_cfg", "")).resolve()
    if not base_cfg.is_file():
        raise FileNotFoundError(f"Study base_cfg does not exist: {base_cfg}")
    load_rpa4_config(base_cfg)
    output_dir = Path(data.get("output_dir", "data/studies")).expanduser()
    if not output_dir.is_absolute():
        output_dir = (source.parent / output_dir).resolve()

    raw_parameters = data.get("parameters")
    if not isinstance(raw_parameters, dict) or not raw_parameters:
        raise ValueError("Study parameters must be a non-empty object")
    parameters = tuple(ParameterSpec.from_dict(name, spec) for name, spec in raw_parameters.items())
    unknown = sorted(parameter.name for parameter in parameters if parameter.name not in _PATHS)
    if unknown:
        raise ValueError("Unsupported RPA4 design parameters: " + ", ".join(unknown))

    sampling = data.get("sampling", {})
    if not isinstance(sampling, dict):
        raise ValueError("sampling must be an object")
    method = str(sampling.get("method", "sobol"))
    if method not in {"grid", "sobol", "adaptive"}:
        raise ValueError("sampling.method must be grid, sobol, or adaptive")
    count = int(sampling.get("count", 32))
    seed = int(sampling.get("seed", 12345))
    max_candidates = int(sampling.get("max_candidates", 10000))
    if count < 1 or max_candidates < 1 or count > max_candidates:
        raise ValueError("sampling count and max_candidates must be positive, with count <= max_candidates")

    ranking = data.get("ranking", {})
    analysis = data.get("analysis", {})
    if not isinstance(ranking, dict) or not isinstance(analysis, dict):
        raise ValueError("ranking and analysis must be objects")
    return StudySpec(
        study_id=study_id,
        source_path=source,
        base_cfg=base_cfg,
        output_dir=output_dir,
        parameters=parameters,
        sampling_method=method,  # type: ignore[arg-type]
        sample_count=count,
        seed=seed,
        max_candidates=max_candidates,
        ranking=ranking,
        analysis=analysis,
    )


def _grid_values(parameter: ParameterSpec) -> list[Any]:
    if parameter.kind in {"categorical", "boolean"}:
        return list(parameter.values)
    assert parameter.minimum is not None and parameter.maximum is not None
    assert parameter.levels is not None
    if parameter.levels == 1:
        values = [parameter.minimum]
    else:
        values = np.linspace(parameter.minimum, parameter.maximum, parameter.levels).tolist()
    if parameter.kind == "integer":
        return sorted({int(round(value)) for value in values})
    return [float(value) for value in values]


def _sobol_values(
    parameters: tuple[ParameterSpec, ...], count: int, seed: int
) -> list[dict[str, Any]]:
    continuous = [p for p in parameters if p.kind not in {"categorical", "boolean"}]
    categorical = [p for p in parameters if p.kind in {"categorical", "boolean"}]
    matrix = qmc.Sobol(d=max(1, len(continuous) + len(categorical)), scramble=True, seed=seed)
    power = int(math.ceil(math.log2(count)))
    points = matrix.random_base2(power)[:count]
    result: list[dict[str, Any]] = []
    for row in points:
        sample: dict[str, Any] = {}
        column = 0
        for parameter in continuous:
            assert parameter.minimum is not None and parameter.maximum is not None
            raw = float(parameter.minimum) + row[column] * (float(parameter.maximum) - float(parameter.minimum))
            sample[parameter.name] = int(round(raw)) if parameter.kind == "integer" else raw
            column += 1
        for parameter in categorical:
            index = min(int(row[column] * len(parameter.values)), len(parameter.values) - 1)
            sample[parameter.name] = parameter.values[index]
            column += 1
        result.append(sample)
    return result


def _adaptive_values(
    spec: StudySpec,
    count: int,
    *,
    study: Any | None = None,
) -> tuple[list[dict[str, Any]], list[int]]:
    try:
        import optuna
    except ImportError as error:
        raise RuntimeError("Adaptive sampling requires Optuna; install the rocket-analysis package dependencies") from error
    objective_specs = spec.ranking.get("objectives", DEFAULT_OBJECTIVES)
    directions = [str(objective["direction"]) for objective in objective_specs]
    for direction in directions:
        if direction not in {"maximize", "minimize"}:
            raise ValueError(f"Invalid Optuna objective direction: {direction}")
    if study is None:
        study = _optuna_study(spec)
    existing_trial_count = len(study.get_trials(deepcopy=False))
    if existing_trial_count + count > spec.max_candidates:
        raise ValueError(
            f"Adaptive study would exceed max_candidates={spec.max_candidates}; "
            f"already has {existing_trial_count} Optuna trials and requested {count} more"
        )
    samples: list[dict[str, Any]] = []
    trials: list[int] = []
    for _ in range(count):
        trial = study.ask()
        values: dict[str, Any] = {}
        for parameter in spec.parameters:
            if parameter.kind in {"categorical", "boolean"}:
                values[parameter.name] = trial.suggest_categorical(parameter.name, list(parameter.values))
            elif parameter.kind == "integer":
                assert parameter.minimum is not None and parameter.maximum is not None
                values[parameter.name] = trial.suggest_int(
                    parameter.name, int(parameter.minimum), int(parameter.maximum)
                )
            else:
                assert parameter.minimum is not None and parameter.maximum is not None
                values[parameter.name] = trial.suggest_float(
                    parameter.name,
                    float(parameter.minimum),
                    float(parameter.maximum),
                    log=parameter.log,
                )
        samples.append(values)
        trials.append(trial.number)
    return samples, trials


def sample_parameter_sets(
    spec: StudySpec,
    *,
    count: int | None = None,
    optuna_study: Any | None = None,
) -> tuple[list[dict[str, Any]], list[int | None]]:
    """Sample parameter dictionaries with grid, Sobol, or adaptive search."""
    requested = spec.sample_count if count is None else int(count)
    if requested < 1 or requested > spec.max_candidates:
        raise ValueError(f"Requested sample count must be between 1 and {spec.max_candidates}")
    if spec.sampling_method == "grid":
        axes = [_grid_values(parameter) for parameter in spec.parameters]
        total = math.prod(len(axis) for axis in axes)
        if total > spec.max_candidates:
            raise ValueError(
                f"Full factorial grid has {total} candidates, above max_candidates={spec.max_candidates}"
            )
        samples = [dict(zip((p.name for p in spec.parameters), values, strict=True)) for values in product(*axes)]
        if count is not None and requested != len(samples):
            raise ValueError(
                f"Full factorial grid produces {len(samples)} candidates; change parameter levels instead of count"
            )
        return samples, [None] * len(samples)
    if spec.sampling_method == "sobol":
        samples = _sobol_values(spec.parameters, requested, spec.seed)
        return samples, [None] * len(samples)
    samples, trial_numbers = _adaptive_values(spec, requested, study=optuna_study)
    return samples, list(trial_numbers)


def _lookup(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = data
    for key in path:
        value = value[key]
    return value


def _set_path(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    target: Any = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def _apply_parameter(config: dict[str, Any], parameter: ParameterSpec, value: Any) -> None:
    value_path, unit_path, dimensionality = _PATHS[parameter.name]
    if dimensionality is None:
        if parameter.kind == "boolean" and not isinstance(value, bool):
            raise ValueError(f"Parameter {parameter.name!r} only accepts booleans")
        _set_path(config, value_path, value)
        return
    if unit_path is None:
        target_unit = "degree"
    else:
        try:
            target_unit = _lookup(config, unit_path)
        except KeyError:
            if parameter.name != "chamber_length" or not parameter.unit:
                raise ValueError(
                    f"Parameter {parameter.name!r} has no unit in the base CFG; specify a unit explicitly"
                )
            target_unit = parameter.unit
            _set_path(config, unit_path, target_unit)
    source_unit = parameter.unit or target_unit
    converted = float(Q_(value, source_unit).to(target_unit).magnitude)
    _set_path(config, value_path, converted)
    if parameter.name == "chamber_length" and parameter.unit and unit_path:
        _set_path(config, unit_path, parameter.unit)


def _resolved_parameter_units(config: dict[str, Any], parameters: tuple[ParameterSpec, ...]) -> dict[str, str | None]:
    units: dict[str, str | None] = {}
    for parameter in parameters:
        value_path, unit_path, dimensionality = _PATHS[parameter.name]
        if parameter.unit:
            units[parameter.name] = parameter.unit
        elif dimensionality == "angle":
            units[parameter.name] = "degree"
        elif unit_path is not None:
            try:
                units[parameter.name] = str(_lookup(config, unit_path))
            except KeyError:
                units[parameter.name] = None
        else:
            units[parameter.name] = None
    return units


def _atomic_write_text(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(contents)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def generate_candidate_cfgs(
    spec: StudySpec,
    *,
    count: int | None = None,
    optuna_study: Any | None = None,
) -> tuple[GeneratedCandidate, ...]:
    """Write sampled, uniquely named RPA4 CFG files and candidate manifests."""
    base = json.loads(spec.base_cfg.read_text(encoding="utf-8"))
    parameter_units = _resolved_parameter_units(base, spec.parameters)
    samples, trial_numbers = sample_parameter_sets(spec, count=count, optuna_study=optuna_study)
    root = spec.output_dir / spec.study_id / "candidates"
    root.mkdir(parents=True, exist_ok=True)
    existing_ids = {
        str(json.loads(path.read_text(encoding="utf-8"))["candidate_id"])
        for path in root.rglob("manifest.json")
    }
    prospective_ids: set[str] = set()
    for values, trial_number in zip(samples, trial_numbers, strict=True):
        serialized = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
        fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:12]
        prospective_ids.add(
            f"trial-{trial_number:06d}" if trial_number is not None else f"candidate-{fingerprint}"
        )
    new_ids = prospective_ids - existing_ids
    if len(existing_ids | prospective_ids) > spec.max_candidates:
        raise ValueError(
            f"Study would exceed max_candidates={spec.max_candidates}; "
            f"already have {len(existing_ids)} and this batch adds {len(new_ids)} unique candidate IDs"
        )
    generated: list[GeneratedCandidate] = []
    seen_samples: set[str] = set()
    for values, trial_number in zip(samples, trial_numbers, strict=True):
        canonical_values = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
        fingerprint = hashlib.sha256(canonical_values.encode("utf-8")).hexdigest()[:12]
        if trial_number is None and fingerprint in seen_samples:
            continue
        seen_samples.add(fingerprint)
        candidate_id = f"trial-{trial_number:06d}" if trial_number is not None else f"candidate-{fingerprint}"
        case_name = f"{spec.study_id}-{candidate_id}"
        config = copy.deepcopy(base)
        config["engineDefinition"]["name"] = case_name
        for parameter in spec.parameters:
            _apply_parameter(config, parameter, values[parameter.name])
        directory = root / candidate_id
        directory.mkdir(parents=True, exist_ok=True)
        cfg_path = directory / "candidate.cfg"
        config_text = json.dumps(config, indent=2) + "\n"
        if cfg_path.exists() and cfg_path.read_text(encoding="utf-8") != config_text:
            raise FileExistsError(f"Candidate ID collision with different CFG contents: {cfg_path}")
        if not cfg_path.exists():
            _atomic_write_text(cfg_path, config_text)
        load_rpa4_config(cfg_path)
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        manifest.update({
            "study_id": spec.study_id,
            "candidate_id": candidate_id,
            "case_name": case_name,
            "status": manifest.get("status", "awaiting_screening"),
            "parameters": values,
            "parameter_units": parameter_units,
            "optuna_trial_number": trial_number,
            "config_file": cfg_path.name,
            "expected_report_file": "candidate.pdf",
        })
        _atomic_write_text(manifest_path, json.dumps(manifest, indent=2) + "\n")
        generated.append(
            GeneratedCandidate(
                study_id=spec.study_id,
                candidate_id=candidate_id,
                case_name=case_name,
                directory=directory,
                config_path=cfg_path,
                manifest_path=manifest_path,
                parameters=values,
                optuna_trial_number=trial_number,
            )
        )
    return tuple(generated)


def _candidate_directories(spec: StudySpec) -> tuple[Path, ...]:
    root = spec.output_dir / spec.study_id / "candidates"
    if not root.is_dir():
        return ()
    return tuple(sorted(path.parent for path in root.rglob("manifest.json")))


def screen_study_candidates(
    spec: StudySpec,
    candidates: tuple[GeneratedCandidate, ...] | None = None,
) -> dict[str, dict[str, Any]]:
    """Run enabled ideal CEA and configured Cantera screening models."""
    from rocket_analysis.rpa4_validation import load_rpa4_inputs
    from rocket_analysis.thermochemistry import (
        run_cantera_equilibrium_screen,
        run_cea_ideal_screen,
    )

    directories = (
        tuple(candidate.directory for candidate in candidates)
        if candidates is not None
        else _candidate_directories(spec)
    )
    cea_spec = spec.analysis.get("cea", {})
    cantera_spec = spec.analysis.get("cantera", {})
    if not isinstance(cea_spec, dict) or not isinstance(cantera_spec, dict):
        raise ValueError("analysis.cea and analysis.cantera must be objects")
    reference_values: dict[str, Any] = {}
    reference_pdf = cea_spec.get("reference_pdf")
    if reference_pdf:
        report_path = (spec.source_path.parent / str(reference_pdf)).resolve()
        reference_values = parse_report_values(report_path)
    reference_props = reference_values.get("propellants", {})
    fuel_temperature_default = float(
        cea_spec.get("fuel_temperature_k", reference_props.get("fuel", {}).get("temperature_k", 298.1))
    )
    oxidizer_temperature_default = float(
        cea_spec.get(
            "oxidizer_temperature_k",
            reference_props.get("oxidizer", {}).get("temperature_k", 90.2),
        )
    )
    outcomes: dict[str, dict[str, Any]] = {}
    for directory in directories:
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        cfg_path = directory / manifest.get("config_file", "candidate.cfg")
        inputs = load_rpa4_inputs(cfg_path)
        screening: dict[str, Any] = {"candidate_id": manifest["candidate_id"]}
        if cea_spec.get("enabled", False):
            pure_rp1_lox = (
                inputs.propellants["fuel"] == (("RP-1", 1.0),)
                and inputs.propellants["oxidizer"] == (("O2(L)", 1.0),)
            )
            if not pure_rp1_lox:
                screening["cea"] = {
                    "status": "unavailable",
                    "reason": "CEA adapter currently requires pure RP-1 and O2(L) streams",
                }
            else:
                try:
                    result = run_cea_ideal_screen(
                        mixture_ratio=inputs.mixture_ratio,
                        chamber_pressure_mpa=float(inputs.chamber_pressure.to("MPa").magnitude),
                        exit_area_ratio=inputs.nozzle_exit_area_ratio,
                        fuel_temperature_k=float(cea_spec.get("fuel_temperature_k", fuel_temperature_default)),
                        oxidizer_temperature_k=float(
                            cea_spec.get("oxidizer_temperature_k", oxidizer_temperature_default)
                        ),
                        fuel_name=str(cea_spec.get("fuel_name", "RP-1")),
                        oxidizer_name=str(cea_spec.get("oxidizer_name", "O2(L)")),
                    )
                    screening["cea"] = {"status": "completed", "result": result.to_dict()}
                except Exception as error:
                    screening["cea"] = {"status": "failed", "reason": f"{type(error).__name__}: {error}"}
        else:
            screening["cea"] = {"status": "not_requested"}

        if cantera_spec.get("enabled", False):
            try:
                result = run_cantera_equilibrium_screen(
                    mechanism=str(cantera_spec["mechanism"]),
                    fuel_species_mass_fractions=cantera_spec["fuel_species_mass_fractions"],
                    oxidizer_species_mass_fractions=cantera_spec["oxidizer_species_mass_fractions"],
                    mixture_ratio=inputs.mixture_ratio,
                    chamber_pressure_pa=float(inputs.chamber_pressure.to("Pa").magnitude),
                    fuel_temperature_k=float(
                        cantera_spec.get("fuel_temperature_k", fuel_temperature_default)
                    ),
                    oxidizer_temperature_k=float(
                        cantera_spec.get("oxidizer_temperature_k", oxidizer_temperature_default)
                    ),
                )
                screening["cantera"] = {"status": "completed", "result": result.to_dict()}
            except Exception as error:
                screening["cantera"] = {
                    "status": "failed",
                    "reason": f"{type(error).__name__}: {error}",
                }
        else:
            screening["cantera"] = {
                "status": "not_configured",
                "reason": "Supply a mechanism and explicit fuel/oxidizer species mapping to enable Cantera.",
            }

        _atomic_write_text(directory / "screening.json", json.dumps(screening, indent=2) + "\n")
        manifest["screening_status"] = "completed" if any(
            item.get("status") == "completed" for key, item in screening.items() if key != "candidate_id"
        ) else "review_required"
        _atomic_write_text(manifest_path, json.dumps(manifest, indent=2) + "\n")
        outcomes[manifest["candidate_id"]] = screening
    return outcomes


def stage_rpa4_candidates(
    spec: StudySpec,
    shared_dir: str | Path | None = None,
    candidates: tuple[GeneratedCandidate, ...] | None = None,
) -> tuple[Path, ...]:
    """Copy CFGs and manifests into per-case folders on the RPA4 share."""
    configured = shared_dir if shared_dir is not None else os.environ.get("RPA4_SHARED_DIR")
    if configured is None or not str(configured).strip():
        raise ValueError("Provide shared_dir or set RPA4_SHARED_DIR before staging RPA4 candidates")
    root = Path(configured).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    directories = (
        tuple(candidate.directory for candidate in candidates)
        if candidates is not None
        else _candidate_directories(spec)
    )
    staged: list[Path] = []
    for directory in directories:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        case_dir = root / str(manifest["case_name"])
        case_dir.mkdir(parents=True, exist_ok=True)
        for filename in (manifest.get("config_file", "candidate.cfg"), "manifest.json", "screening.json"):
            source = directory / filename
            if not source.is_file():
                continue
            destination = case_dir / filename
            contents = source.read_bytes()
            if destination.exists() and destination.read_bytes() != contents and destination.suffix.casefold() == ".cfg":
                raise FileExistsError(f"A different RPA4 candidate CFG is already staged: {destination}")
            if not destination.exists() or destination.read_bytes() != contents:
                temporary = destination.with_name(destination.name + ".tmp")
                temporary.write_bytes(contents)
                os.replace(temporary, destination)
        staged.append(case_dir)
    return tuple(staged)


def _optuna_study(spec: StudySpec):
    try:
        import optuna
    except ImportError as error:  # pragma: no cover - package dependency guard
        raise RuntimeError("Adaptive studies require Optuna") from error
    objective_specs = spec.ranking.get("objectives", DEFAULT_OBJECTIVES)
    directions = [str(objective["direction"]) for objective in objective_specs]
    study_dir = spec.output_dir / spec.study_id
    study_dir.mkdir(parents=True, exist_ok=True)
    storage = f"sqlite:///{(study_dir / 'optuna.sqlite3').as_posix()}"
    sampler = optuna.samplers.NSGAIISampler(seed=spec.seed)
    try:
        study = optuna.load_study(study_name=spec.study_id, storage=storage, sampler=sampler)
    except KeyError:
        return optuna.create_study(
            study_name=spec.study_id,
            storage=storage,
            directions=directions,
            sampler=sampler,
        )
    actual_directions = [direction.name.casefold() for direction in study.directions]
    expected_directions = [direction.casefold() for direction in directions]
    if actual_directions != expected_directions:
        raise ValueError(
            "Adaptive study objective directions changed after trials were persisted; "
            "use a new study_id to start with a different objective set."
        )
    return study


def import_shared_rpa4_results(
    spec: StudySpec,
    shared_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Import completed reports from RPA4_SHARED_DIR and persist their metrics."""
    from rocket_analysis.ranking import candidate_metric_catalog
    from rocket_analysis.shared import load_shared_rpa4_cases

    cases = load_shared_rpa4_cases(shared_dir, allow_incomplete=True)
    directories = _candidate_directories(spec)
    manifest_by_case: dict[str, tuple[Path, dict[str, Any]]] = {}
    for directory in directories:
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_by_case[str(manifest["case_name"]).strip().casefold()] = (directory, manifest)
    results_path = spec.output_dir / spec.study_id / "rpa4-results.json"
    previous = json.loads(results_path.read_text(encoding="utf-8")) if results_path.exists() else {}
    by_id = {row["candidate_id"]: row for row in previous.get("results", [])}
    imported: list[str] = []
    ignored: list[str] = []
    adaptive_study = _optuna_study(spec) if spec.sampling_method == "adaptive" else None
    for case in cases:
        matched = manifest_by_case.get(case.case_name.strip().casefold())
        if matched is None:
            ignored.append(case.case_name)
            continue
        directory, manifest = matched
        candidate_id = str(manifest["candidate_id"])
        screening_path = directory / "screening.json"
        screening = json.loads(screening_path.read_text(encoding="utf-8")) if screening_path.exists() else {}
        metrics = candidate_metric_catalog(
            case.report_values,
            screening,
            design_parameters=manifest.get("parameters", {}),
            design_units=manifest.get("parameter_units", {}),
        )
        validation_status = str(case.validation.get("status", "unknown"))
        row = {
            "study_id": spec.study_id,
            "candidate_id": candidate_id,
            "case_name": case.case_name,
            "parameters": manifest.get("parameters", {}),
            "optuna_trial_number": manifest.get("optuna_trial_number"),
            "validation_status": validation_status,
            "validation": case.validation,
            "config_path": str(case.config_path),
            "report_path": str(case.report_path),
            "report_values": case.report_values,
            "screening": screening,
            "metrics": metrics,
        }
        by_id[candidate_id] = row
        imported.append(candidate_id)
        manifest["status"] = "rpa4_imported"
        manifest["validation_status"] = validation_status
        _atomic_write_text(directory / "manifest.json", json.dumps(manifest, indent=2) + "\n")
        if adaptive_study is not None and manifest.get("optuna_trial_number") is not None:
            trial_number = int(manifest["optuna_trial_number"])
            trial = next(
                (trial for trial in adaptive_study.get_trials(deepcopy=False) if trial.number == trial_number),
                None,
            )
            if trial is not None and trial.state.name == "RUNNING":
                objective_specs = spec.ranking.get("objectives", DEFAULT_OBJECTIVES)
                objective_values = [metrics.get(str(item["metric"])) for item in objective_specs]
                if validation_status == "mismatch" or any(value is None for value in objective_values):
                    import optuna

                    adaptive_study.tell(trial_number, state=optuna.trial.TrialState.FAIL)
                else:
                    adaptive_study.tell(trial_number, values=[float(value) for value in objective_values])

    payload = {
        "study_id": spec.study_id,
        "sampling_method": spec.sampling_method,
        "results": sorted(by_id.values(), key=lambda row: row["candidate_id"]),
        "last_imported_candidate_ids": imported,
        "ignored_shared_case_names": ignored,
    }
    _atomic_write_text(results_path, json.dumps(payload, indent=2) + "\n")
    return payload


def rank_study_results(
    spec: StudySpec,
    ranking_spec: dict[str, Any] | None = None,
) -> RankingResult:
    """Rank all imported candidates using Pareto or weighted objectives."""
    results_path = spec.output_dir / spec.study_id / "rpa4-results.json"
    if not results_path.is_file():
        raise FileNotFoundError(f"No imported RPA4 results found: {results_path}")
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    rows = [
        {
            "candidate_id": row["candidate_id"],
            "case_name": row["case_name"],
            "validation_status": row["validation_status"],
            "metrics": row["metrics"],
        }
        for row in payload.get("results", [])
    ]
    return rank_metric_rows(rows, ranking_spec if ranking_spec is not None else spec.ranking)


def rank_screened_candidates(
    spec: StudySpec,
    ranking_spec: dict[str, Any],
) -> RankingResult:
    """Rank local screening outputs before selecting candidates for RPA4 runs."""
    from rocket_analysis.ranking import candidate_metric_catalog

    rows: list[dict[str, Any]] = []
    for directory in _candidate_directories(spec):
        manifest_path = directory / "manifest.json"
        screening_path = directory / "screening.json"
        if not screening_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        screening = json.loads(screening_path.read_text(encoding="utf-8"))
        metrics = candidate_metric_catalog(
            {},
            screening,
            design_parameters=manifest.get("parameters", {}),
            design_units=manifest.get("parameter_units", {}),
        )
        rows.append(
            {
                "candidate_id": str(manifest["candidate_id"]),
                "case_name": str(manifest["case_name"]),
                "validation_status": "review_required",
                "metrics": metrics,
            }
        )
    return rank_metric_rows(rows, ranking_spec)


def save_study_ranking(spec: StudySpec, ranking: RankingResult) -> Path:
    """Write the current ranking and exclusions as a JSON snapshot."""
    from dataclasses import asdict

    path = spec.output_dir / spec.study_id / "ranking.json"
    _atomic_write_text(path, json.dumps(asdict(ranking), indent=2) + "\n")
    return path
