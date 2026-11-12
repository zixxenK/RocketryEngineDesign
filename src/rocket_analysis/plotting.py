"""Plots for the RPA4 benchmark and CFD station data."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Sequence

import matplotlib.pyplot as plt

from rocket_analysis.models import MachStation


def plot_mach_reference(
    stations: Sequence[MachStation],
    *,
    title: str = "Mach number by station",
    output: str | Path | None = None,
):
    """Plot Mach values against ordered engine stations."""
    if not stations:
        raise ValueError("At least one station is required")
    for point in stations:
        if not point.name.strip():
            raise ValueError("Station names must be non-empty")
        if not math.isfinite(point.mach) or point.mach < 0.0:
            raise ValueError(f"Mach value for {point.name!r} must be finite and non-negative")
    x_values = range(len(stations))
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(x_values, [point.mach for point in stations], marker="o", linestyle="None")
    for x_value, point in enumerate(stations):
        ax.annotate(
            f"{point.mach:g}",
            (x_value, point.mach),
            xytext=(0, 7),
            textcoords="offset points",
            ha="center",
        )
    ax.set_xticks(list(x_values), [point.name for point in stations])
    ax.set_ylabel("Mach number")
    ax.set_xlabel("Station")
    ax.set_title(title)
    ax.set_ylim(bottom=0.0)
    ax.grid(True, linestyle=":", alpha=0.6)
    if output is not None:
        fig.savefig(output, dpi=160, bbox_inches="tight")
    return fig, ax
