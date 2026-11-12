# Rocket Analysis Toolkit

This Python workspace supports RP-1/liquid-oxygen design studies with RPA4, NASA CEA, configurable Cantera models, report ranking, and finalist CFD preparation. RPA4 runs manually on the other computer; this project creates candidate CFGs and reads the resulting PDFs from a shared folder.

## Set up the Windows Conda environment

Install Anaconda3, open Anaconda Prompt in this directory, then run:

```powershell
conda create --name rocket-dev --override-channels --channel conda-forge --file conda-dependencies.txt
conda activate rocket-dev
python -m pip install "jupyter-book>=2.0.0" -e .
jupyter lab
```

`environment.yml` is also provided for installations already configured to use `conda-forge`. The upstream course notes are at <https://kyleniemeyer.github.io/rocket-propulsion/>; its [CEA and Cantera notebook](https://github.com/kyleniemeyer/rocket-propulsion/blob/main/thermochemistry/cea_cantera.ipynb) is a useful thermochemistry reference.

## Study workflow

The example spec is [rp1-lox-design-study.example.json](data/studies/rp1-lox-design-study.example.json). It samples O/F, chamber pressure, throat diameter, nozzle exit area ratio, and the RPA4 nozzle inlet area-ratio field. Its ranges are placeholders around the supplied case, not approved hot-fire limits. Replace them with your design envelope, and set hard constraints from the actual feed system, test stand, cooling, materials, and instrumentation limits before selecting hardware.

The example uses Sobol sampling. Grid and persistent Optuna adaptive sampling are also available. Candidate IDs remain stable across repeated grid/Sobol generation, and adaptive trial state is stored in SQLite under the study output directory.

```python
from rocket_analysis.study import (
    load_study_spec,
    generate_candidate_cfgs,
    screen_study_candidates,
    stage_rpa4_candidates,
    import_shared_rpa4_results,
    rank_study_results,
    save_study_ranking,
)

spec = load_study_spec("data/studies/rp1-lox-design-study.example.json")
candidates = generate_candidate_cfgs(spec)
screening = screen_study_candidates(spec, candidates)
staged_folders = stage_rpa4_candidates(spec, candidates=candidates)
```

Set `RPA4_SHARED_DIR` to a folder reachable from both computers before staging. Each candidate gets a subfolder containing its CFG, manifest, and screening record. Run each CFG manually in RPA4 and place that candidate's PDF in the same folder. After those reports are available:

```powershell
$env:RPA4_SHARED_DIR = '\\RPA4-COMPUTER\RPA4\exports'
```

```python
imported = import_shared_rpa4_results(spec)
ranking = rank_study_results(spec)
save_study_ranking(spec, ranking)
```

The importer accepts partial batches and reloads the current files each time. It records parsed report metrics, validation findings, and model-screen outputs in `rpa4-results.json`. Adaptive Optuna trials are completed from imported report objectives, so another call to `generate_candidate_cfgs()` proposes the next batch.

See [the design-study guide](docs/design-studies.md) for all supported RPA4 parameters, metric names, constraints, adaptive-study behavior, and screening limits.

## Screening, ranking, and model scope

NASA CEA's bundled RP-1 and O2(L) data provide ideal equilibrium screening metrics such as characteristic velocity, exit Mach, and ideal specific impulse. The supplied PDF's reported propellant inlet temperatures are used when the example spec points to the reference PDF. CEA results are an independent code/database comparison, not experimental validation or predicted delivered performance.

Cantera screening is disabled until a study explicitly supplies a mechanism and fuel/oxidizer species mass-fraction mappings. The adapter returns an HP equilibrium chamber state only; it does not calculate nozzle performance. Do not treat an unvalidated surrogate as RP-1.

Pareto ranking and weighted scoring are separate modes. Default report objectives maximize estimated-delivered sea-level thrust and Isp. Any parsed performance, station, propellant, geometry, mass-flow, or altitude-sweep metric can be selected as an objective or constraint. Weighted scores normalize each chosen metric from 0 to 1 over the eligible candidates in the current import; raw values are retained in the ranking output. Validation review warnings are shown and remain eligible, while hard mismatches and failed constraints are excluded.

The nozzle inlet `areaRatio` is swept from 2 to 15 in the example, but the supplied PDF's “Nozzle inlet” station does not establish that it maps to the CFG field. The validator reports the difference as a review warning; it neither changes the source files nor blocks ranking on that point alone.

## Supplied reference case

`data/reference/Test6in.cfg`, `data/reference/test1-6in-of-1.5.pdf`, and `data/reference/test1-mach.png` are the transferred test files. The PDF parser extracts propellant conditions, performance tables, geometry, and exact Table 2 Mach samples. The generated station plot is saved at [test1-mach-station-validation.png](data/reference/test1-mach-station-validation.png). The snapshot and interpretation are in [rpa4-validation.md](docs/rpa4-validation.md).

## CFD finalist path

OpenFOAM 2512 is installed in Ubuntu 24.04 on WSL2. `openfoam_finalist_ids()` selects a limited ranked set, and the finalist helper can copy a supplied case template and substitute its explicit tokens. Running CFD requires an appropriate mesh, thermochemical/transport model, boundary conditions, and validated case setup supplied by the user. The included OpenFOAM tutorial is an installation smoke case, not validation against the RPA4 report. See [the solver comparison](docs/cfd-solver-comparison.md).

OpenMDAO can wrap callable local analysis functions with finite-difference derivatives. It is an optimization driver interface, not a physical model. CSV station results from OpenFOAM, SU2, or other solvers can be compared with the report Mach values through `rocket_analysis.cfd`.

## Main package modules

- `rocket_analysis.study`: study specs, grid/Sobol/adaptive samples, RPA4 CFG generation, screening, share staging, report import, and ranking persistence.
- `rocket_analysis.ranking`: report metric catalog, constraints, Pareto fronts, and weighted scoring.
- `rocket_analysis.thermochemistry`: ideal CEA and explicitly configured Cantera screening adapters.
- `rocket_analysis.rpa4` and `rocket_analysis.rpa4_validation`: PDF parsing and CFG/report validation.
- `rocket_analysis.shared`: complete RPA4 shared-folder pairs, including partial batch loading.
- `rocket_analysis.openmdao_adapter`: callable analysis problem wrapper.
- `rocket_analysis.cfd`: solver-neutral station comparison and OpenFOAM finalist helpers.

## Verification

Run the project checks in `rocket-dev` with `python -m pytest`. These cover parsing, ranking, sampling and persistence, model adapters, shared-folder imports, and CFD finalist selection. Solver runs remain dependent on the user's RPA4 share and a suitable OpenFOAM case template.
