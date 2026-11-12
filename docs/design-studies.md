# RP-1/LOX design studies

## Set up a study

Start from [the example spec](../data/studies/rp1-lox-design-study.example.json). It is a runnable demonstration, not an approved engine or hot-fire operating envelope. Replace its ranges and constraints with design requirements and test-stand limits before using candidate rankings for hardware decisions.

Each parameter name maps to a field in the source RPA4 CFG. Supported names include `mixture_ratio`, `chamber_pressure`, `number_of_chambers`, `throat_diameter`, `nozzle_inlet_area_ratio`, `nozzle_exit_area_ratio`, `chamber_length`, `chamber_contraction_angle`, `r1_to_rt`, `r2_to_r2max`, `nozzle_throat_radius_to_rt`, `nozzle_convergence_angle`, `nozzle_exit_angle`, `ambient_pressure_from`, `ambient_pressure_to`, `ambient_pressure_point_count`, `equilibrium_flow_shifting`, `reaction_efficiency_estimated`, `bell_nozzle_efficiency_estimated`, `nozzle_efficiency_applied`, and `multiphase`.

Continuous and integer parameters take `min`, `max`, and optional grid `levels`; they may also include a unit. Ranges are converted to units in the base CFG. If a CFG field has no unit, provide one explicitly. Categorical and boolean parameters take a `values` list. Sampling methods are `grid`, `sobol`, and `adaptive`. `max_candidates` limits the total number of candidate or adaptive trial IDs in a study.

```python
from rocket_analysis.study import load_study_spec, generate_candidate_cfgs

spec = load_study_spec("data/studies/rp1-lox-design-study.example.json")
candidates = generate_candidate_cfgs(spec)
```

Grid and Sobol candidate IDs are based on their parameter values, so rerunning generation reuses the same folders. Adaptive candidates receive Optuna trial IDs, and the SQLite study is saved beside the candidate data. Use a new `study_id` if changing the objective directions after adaptive trials have been stored.

## Screen and stage candidates

Enable CEA in `analysis.cea` for ideal equilibrium expansion calculations. The adapter uses bundled `RP-1` and `O2(L)` reactant data and supports pure streams. Provide inlet temperatures directly or point to the supplied reference PDF. CEA outputs are labeled ideal and remain separate from RPA4 estimated-delivered outputs.

Cantera is disabled by default. To enable it, select a mechanism and provide explicit fuel and oxidizer species mass-fraction dictionaries that sum to one. The adapter calculates an HP equilibrium chamber state. It does not infer a fuel surrogate or compute nozzle performance.

```python
from rocket_analysis.study import screen_study_candidates, stage_rpa4_candidates

screening = screen_study_candidates(spec, candidates)
staged_folders = stage_rpa4_candidates(spec, candidates=candidates)
```

Set `RPA4_SHARED_DIR` to the shared folder before staging, or pass `shared_dir` directly. Each candidate gets a separate folder with `candidate.cfg`, `manifest.json`, and `screening.json`. Copy each CFG to the RPA4 computer, run it manually, and return its PDF to the matching folder. Reports are matched by the RPA4 case name contained in their contents; keep one CFG/PDF pair per name.

Local screening results can be Pareto-ranked before selecting which cases to run in RPA4:

```python
from rocket_analysis.study import rank_screened_candidates

screen_ranking = rank_screened_candidates(spec, {
    "mode": "pareto",
    "objectives": [
        {"metric": "screening.cea.result.ideal_vacuum_specific_impulse_m_s", "direction": "maximize"},
        {"metric": "screening.cea.result.characteristic_velocity_m_s", "direction": "maximize"},
    ],
})
```

This compares ideal thermochemical screening metrics; it is not a prediction of hot-fire-delivered performance.

## Import RPA4 and rank all objectives

`import_shared_rpa4_results(spec)` reads complete pairs and skips staged candidates still awaiting a PDF. It stores `rpa4-results.json` with the CFG/PDF validation, report values, screening records, and numeric metrics. An area-ratio mapping warning is stored as `review_required`; candidates with a hard mismatch are excluded from ranking. Failed constraints are excluded as well.

The metric catalog includes:

- Ideal and estimated-delivered `c*`, thrust, Isp, effective exhaust velocity, thrust coefficient, altitude, and ambient pressure.
- Propellant inlet temperatures, pressures, enthalpies, and mass fractions.
- Station pressure, temperature, Mach, area ratio, gamma, and molecular weight.
- Chamber/nozzle geometry, mass flow, chamber count, and every parsed altitude-table cell.
- Candidate design parameters under `design.*` and numerical CEA/Cantera screen outputs under `screening.*`.

For example, `estimated_delivered.sea_level.thrust_kN`, `station_conditions.mach.nozzle_exit`, `geometry.exit_to_throat_area_ratio`, and `design.chamber_pressure_mpa` can each be an objective or constraint. Supported constraint operators are `<`, `<=`, `==`, `>=`, and `>`.

The default ranking is Pareto with these two objectives:

1. Maximize estimated-delivered sea-level thrust.
2. Maximize estimated-delivered sea-level specific impulse.

Pareto ranking retains non-dominated tradeoffs. Weighted mode normalizes each objective from zero to one over the eligible candidates in the current results set, applies non-negative weights, and sorts by score. Adding or removing candidates can change weighted scores; the output retains raw metrics and states the normalization used. Add hard constraints from the real engine and test-stand requirements before treating a rank as a design recommendation.

```python
from rocket_analysis.study import import_shared_rpa4_results, rank_study_results, save_study_ranking

import_shared_rpa4_results(spec)
ranking = rank_study_results(spec)
save_study_ranking(spec, ranking)
```

For an adaptive study, the importer calls Optuna `tell()` with the imported RPA4 objective values. Generate another batch to continue the persisted study. Failed validations or missing objectives mark that trial failed rather than feeding incomplete values back into the sampler.

## OpenMDAO and CFD

`build_callable_problem()` wraps a scalar Python analysis function as an OpenMDAO component and estimates derivatives by finite differences. It only provides an optimization interface; the callable supplies the physics.

Use `openfoam_finalist_ids()` to select a small top-ranked set before preparing CFD cases. `prepare_openfoam_finalist_case()` copies a user-provided template and replaces explicit `{{token}}` strings. It does not create mesh, reacting-flow chemistry, heat-transfer models, or boundary conditions. Only run a case after those inputs have been supplied and reviewed. See [the CFD comparison notes](cfd-solver-comparison.md).
