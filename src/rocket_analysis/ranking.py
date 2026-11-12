"""Metric catalogs, constraints, Pareto fronts, and weighted scoring."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


DEFAULT_OBJECTIVES = (
    {"metric": "estimated_delivered.sea_level.thrust_kN", "direction": "maximize"},
    {"metric": "estimated_delivered.sea_level.specific_impulse_s", "direction": "maximize"},
)
_OPERATING_POINTS = ("sea_level", "optimum_expansion", "vacuum")
_PERFORMANCE_SERIES = {
    "effective_exhaust_velocity_m_s": "effective_exhaust_velocity_m_s",
    "specific_impulse_s": "specific_impulse_s",
    "thrust_coefficient": "thrust_coefficient",
    "thrust_kN": "thrust_kN",
    "ambient_pressure_atm": "ambient_pressure_atm",
    "altitude_km": "altitude_km",
}


@dataclass(frozen=True)
class RankingEntry:
    candidate_id: str
    case_name: str
    validation_status: str
    rank: int
    metrics: dict[str, float]
    weighted_score: float | None = None


@dataclass(frozen=True)
class RankingExclusion:
    candidate_id: str
    reason: str


@dataclass(frozen=True)
class RankingResult:
    mode: str
    objectives: tuple[dict[str, Any], ...]
    weights: dict[str, float]
    entries: tuple[RankingEntry, ...]
    exclusions: tuple[RankingExclusion, ...]
    normalization: str

    @property
    def top_ranked(self) -> tuple[RankingEntry, ...]:
        return tuple(entry for entry in self.entries if entry.rank == 1)


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def report_metric_catalog(report_values: dict[str, Any]) -> dict[str, float]:
    """Flatten parsed RPA4 outputs into selectable, unit-labelled metrics."""
    metrics: dict[str, float] = {}
    for table_name in ("ideal", "estimated_delivered"):
        table = report_values.get(table_name, {})
        for field in ("characteristic_velocity_m_s",):
            if field in table:
                metrics[f"{table_name}.{field}"] = float(table[field])
        for field in _PERFORMANCE_SERIES:
            values = table.get(field)
            if not isinstance(values, list):
                continue
            for index, value in enumerate(values):
                if value is None or index >= len(_OPERATING_POINTS):
                    continue
                point = _OPERATING_POINTS[index]
                metrics[f"{table_name}.{point}.{field}"] = float(value)

    for key, value in report_values.get("geometry", {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            metrics[f"geometry.{key}"] = float(value)
    for role, component in report_values.get("propellants", {}).items():
        if not isinstance(component, dict):
            continue
        for field, value in component.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if math.isfinite(float(value)):
                    metrics[f"propellants.{_key(role)}.{_key(field)}"] = float(value)
    station_names = report_values.get("station_names", [])
    for key, values in report_values.get("station_conditions", {}).items():
        if not isinstance(values, list):
            continue
        for index, item in enumerate(values):
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                if math.isfinite(float(item)):
                    station = station_names[index] if index < len(station_names) else f"station_{index}"
                    metrics[f"station_conditions.{_key(key)}.{_key(station)}"] = float(item)
    scalar_fields = (
        "mixture_ratio",
        "total_mass_flow_kg_s",
        "oxidizer_mass_flow_kg_s",
        "fuel_mass_flow_kg_s",
        "number_of_chambers",
    )
    for field in scalar_fields:
        value = report_values.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            metrics[field] = float(value)

    for index, row in enumerate(report_values.get("altitude_performance_rows", [])):
        for column, value in enumerate(row):
            if math.isfinite(float(value)):
                metrics[f"altitude_sweep.row_{index:03d}.column_{column}"] = float(value)
    return metrics


def candidate_metric_catalog(
    report_values: dict[str, Any],
    screening_values: dict[str, Any] | None = None,
    design_parameters: dict[str, Any] | None = None,
    design_units: dict[str, str | None] | None = None,
) -> dict[str, float]:
    """Return design, report, and screening metrics with separate namespaces."""
    metrics = report_metric_catalog(report_values)

    def add_numeric(prefix: str, values: Any) -> None:
        if isinstance(values, dict):
            for key, value in values.items():
                name = _key(str(key))
                add_numeric(f"{prefix}.{name}" if prefix else name, value)
        elif isinstance(values, (list, tuple)):
            for index, value in enumerate(values):
                add_numeric(f"{prefix}.index_{index}", value)
        elif isinstance(values, (int, float)) and not isinstance(values, bool):
            number = float(values)
            if math.isfinite(number):
                metrics[f"screening.{prefix}"] = number

    if screening_values:
        add_numeric("", screening_values)
    if design_parameters:
        for name, value in design_parameters.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                number = float(value)
                if math.isfinite(number):
                    unit = (design_units or {}).get(name)
                    suffix = f"_{_key(unit)}" if unit else ""
                    metrics[f"design.{_key(str(name))}{suffix}"] = number
    return metrics


def _constraint_passes(value: float, operator: str, bound: float) -> bool:
    if operator == "<":
        return value < bound
    if operator == "<=":
        return value <= bound
    if operator == "==":
        return math.isclose(value, bound, rel_tol=0.0, abs_tol=1e-9)
    if operator == ">=":
        return value >= bound
    if operator == ">":
        return value > bound
    raise ValueError(f"Unsupported constraint operator: {operator}")


def _dominates(left: dict[str, float], right: dict[str, float], objectives: tuple[dict[str, Any], ...]) -> bool:
    no_worse = True
    strictly_better = False
    for objective in objectives:
        key = objective["metric"]
        a, b = left[key], right[key]
        if objective["direction"] == "maximize":
            no_worse &= a >= b
            strictly_better |= a > b
        else:
            no_worse &= a <= b
            strictly_better |= a < b
    return no_worse and strictly_better


def _pareto_ranks(rows: list[dict[str, Any]], objectives: tuple[dict[str, Any], ...]) -> list[int]:
    remaining = set(range(len(rows)))
    ranks = [0] * len(rows)
    rank = 1
    while remaining:
        front = [
            index
            for index in remaining
            if not any(
                other != index and _dominates(rows[other]["metrics"], rows[index]["metrics"], objectives)
                for other in remaining
            )
        ]
        if not front:
            raise RuntimeError("Pareto sorting failed to find a non-dominated candidate")
        for index in front:
            ranks[index] = rank
        remaining.difference_update(front)
        rank += 1
    return ranks


def rank_metric_rows(
    candidates: list[dict[str, Any]],
    ranking_spec: dict[str, Any] | None = None,
) -> RankingResult:
    """Filter constraints and rank candidates with Pareto or weighted scoring.

    Each input row requires ``candidate_id``, ``case_name``, ``validation``
    status, and a flat ``metrics`` dictionary from :func:`report_metric_catalog`.
    Weighted scores use current-set min/max scaling; adding candidates can
    change scores, so raw metrics remain in every output entry.
    """
    spec = ranking_spec or {}
    mode = str(spec.get("mode", "pareto"))
    if mode not in {"pareto", "weighted"}:
        raise ValueError("ranking mode must be 'pareto' or 'weighted'")
    objectives = tuple(spec.get("objectives", DEFAULT_OBJECTIVES))
    if not objectives:
        raise ValueError("At least one objective is required")
    for objective in objectives:
        if objective.get("direction") not in {"maximize", "minimize"}:
            raise ValueError(f"Invalid direction for metric {objective.get('metric')!r}")
    metric_names = [str(objective.get("metric", "")) for objective in objectives]
    if any(not name for name in metric_names) or len(set(metric_names)) != len(metric_names):
        raise ValueError("Objective metrics must be non-empty and unique")
    constraints = tuple(spec.get("constraints", ()))
    for constraint in constraints:
        if constraint.get("operator") not in {"<", "<=", "==", ">=", ">"}:
            raise ValueError(f"Invalid constraint operator: {constraint.get('operator')!r}")
        try:
            bound = float(constraint["value"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Every constraint must have a numeric value") from error
        if not math.isfinite(bound):
            raise ValueError("Constraint values must be finite")
    raw_weights = spec.get("weights", {})
    weights: dict[str, float] = {}
    if mode == "weighted":
        for name in metric_names:
            weight = float(raw_weights.get(name, 0.0))
            if not math.isfinite(weight) or weight < 0.0:
                raise ValueError(f"Weight for {name!r} must be finite and non-negative")
            weights[name] = weight
        total = sum(weights.values())
        if total <= 0.0:
            raise ValueError("Weighted ranking needs at least one positive objective weight")
        weights = {name: weight / total for name, weight in weights.items()}

    eligible: list[dict[str, Any]] = []
    exclusions: list[RankingExclusion] = []
    for candidate in candidates:
        candidate_id = str(candidate["candidate_id"])
        status = str(candidate.get("validation_status", "unknown"))
        if status not in {"match", "review_required", "mismatch"}:
            exclusions.append(RankingExclusion(candidate_id, f"unsupported validation status: {status}"))
            continue
        if status == "mismatch":
            exclusions.append(RankingExclusion(candidate_id, "CFG/PDF validation has a hard mismatch"))
            continue
        metrics = candidate["metrics"]
        missing = [name for name in metric_names if name not in metrics]
        if missing:
            exclusions.append(RankingExclusion(candidate_id, "missing objective metrics: " + ", ".join(missing)))
            continue
        values = {name: float(metrics[name]) for name in metric_names}
        if any(not math.isfinite(value) for value in values.values()):
            exclusions.append(RankingExclusion(candidate_id, "objective metric is non-finite"))
            continue
        failed = None
        for constraint in constraints:
            name = str(constraint["metric"])
            if name not in metrics:
                failed = f"missing constrained metric: {name}"
                break
            if not _constraint_passes(
                float(metrics[name]), str(constraint["operator"]), float(constraint["value"])
            ):
                failed = f"constraint failed: {name} {constraint['operator']} {constraint['value']}"
                break
        if failed:
            exclusions.append(RankingExclusion(candidate_id, failed))
            continue
        retained_metrics: dict[str, float] = {}
        for name, value in metrics.items():
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                retained_metrics[str(name)] = number
        eligible.append(
            {**candidate, "metrics": retained_metrics, "validation_status": status}
        )

    if not eligible:
        return RankingResult(mode, objectives, weights, (), tuple(exclusions), "none")

    scores: list[float | None] = [None] * len(eligible)
    if mode == "pareto":
        ranks = _pareto_ranks(eligible, objectives)
        ordered = sorted(range(len(eligible)), key=lambda index: (ranks[index], eligible[index]["candidate_id"]))
        normalization = "Pareto dominance; no metric scaling"
    else:
        normalized: dict[str, list[float]] = {name: [] for name in metric_names}
        for name in metric_names:
            column = [row["metrics"][name] for row in eligible]
            lower, upper = min(column), max(column)
            direction = next(obj["direction"] for obj in objectives if obj["metric"] == name)
            if math.isclose(lower, upper, rel_tol=0.0, abs_tol=1e-15):
                normalized[name] = [0.5] * len(column)
            elif direction == "maximize":
                normalized[name] = [(value - lower) / (upper - lower) for value in column]
            else:
                normalized[name] = [(upper - value) / (upper - lower) for value in column]
        scores = [
            sum(weights[name] * normalized[name][index] for name in metric_names)
            for index in range(len(eligible))
        ]
        ordered = sorted(range(len(eligible)), key=lambda index: (-float(scores[index]), eligible[index]["candidate_id"]))
        ranks = [0] * len(eligible)
        last_score: float | None = None
        current_rank = 0
        for position, index in enumerate(ordered, start=1):
            score = float(scores[index])
            if last_score is None or not math.isclose(score, last_score, rel_tol=0.0, abs_tol=1e-12):
                current_rank = position
                last_score = score
            ranks[index] = current_rank
        normalization = "min-max over the eligible candidates in this study"

    entries = tuple(
        RankingEntry(
            candidate_id=str(eligible[index]["candidate_id"]),
            case_name=str(eligible[index]["case_name"]),
            validation_status=str(eligible[index]["validation_status"]),
            rank=ranks[index],
            metrics=eligible[index]["metrics"],
            weighted_score=float(scores[index]) if scores[index] is not None else None,
        )
        for index in ordered
    )
    return RankingResult(mode, objectives, weights, entries, tuple(exclusions), normalization)
