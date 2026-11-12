"""Wrap an existing local analysis callable as an OpenMDAO component."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def build_callable_problem(
    model: Callable[[dict[str, float]], dict[str, float]],
    *,
    input_defaults: dict[str, float],
    output_defaults: dict[str, float],
    finite_difference_step: float = 1e-6,
):
    """Build an OpenMDAO Problem around a scalar-in/scalar-out Python model.

    This adapter lets OpenMDAO drivers call a local screening function. It
    estimates derivatives by finite differences and does not make a model
    more accurate or provide missing physics.
    """
    if not input_defaults or not output_defaults:
        raise ValueError("At least one input and one output are required")
    if finite_difference_step <= 0:
        raise ValueError("finite_difference_step must be positive")
    try:
        import openmdao.api as om
    except ImportError as error:  # pragma: no cover - package dependency guard
        raise RuntimeError("OpenMDAO integration requires the 'openmdao' package") from error

    input_names = tuple(input_defaults)
    output_names = tuple(output_defaults)

    class CallableModelComponent(om.ExplicitComponent):
        def setup(self):
            for name in input_names:
                self.add_input(name, val=float(input_defaults[name]))
            for name in output_names:
                self.add_output(name, val=float(output_defaults[name]))

        def compute(self, inputs, outputs):
            model_inputs = {name: float(inputs[name][0]) for name in input_names}
            model_outputs = model(model_inputs)
            missing = [name for name in output_names if name not in model_outputs]
            if missing:
                raise ValueError("Callable model omitted outputs: " + ", ".join(missing))
            for name in output_names:
                outputs[name] = float(model_outputs[name])

    problem = om.Problem(reports=False)
    problem.model.add_subsystem("analysis", CallableModelComponent(), promotes=["*"])
    problem.model.approx_totals(method="fd", step=finite_difference_step)
    problem.setup()
    for name, value in input_defaults.items():
        problem.set_val(name, float(value))
    return problem


def evaluate_callable(
    model: Callable[[dict[str, float]], dict[str, float]],
    inputs: dict[str, float],
) -> dict[str, float]:
    """Evaluate a callable with finite inputs and finite scalar outputs."""
    import math

    normalized = {name: float(value) for name, value in inputs.items()}
    if not normalized or any(not math.isfinite(value) for value in normalized.values()):
        raise ValueError("Model inputs must be a non-empty mapping of finite numbers")
    outputs: dict[str, Any] = model(normalized)
    if not isinstance(outputs, dict) or not outputs:
        raise ValueError("Callable model must return a non-empty output dictionary")
    result = {name: float(value) for name, value in outputs.items()}
    if any(not math.isfinite(value) for value in result.values()):
        raise ValueError("Model outputs must be finite scalar numbers")
    return result

