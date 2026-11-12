# Rocket Analysis Toolkit

This Python workspace supports RP-1/liquid-oxygen design studies with RPA4, NASA CEA, configurable Cantera models, report ranking, and finalist CFD preparation. RPA4 runs manually on the other computer; this project creates candidate CFGs and reads the resulting PDFs from a shared folder.

## Set up the Windows Conda environment

Install Anaconda3, open Anaconda Prompt in this directory, then run:

```powershell
conda create --name rocket-dev --override-channels --channel conda-forge --file conda-dependencies.txt
conda activate rocket-dev
python -m pip install "jupyter-book>=2.0.0" -e ".[desktop]"
jupyter lab
```

`environment.yml` is also provided for installations already configured to use `conda-forge`. The upstream course notes are at <https://kyleniemeyer.github.io/rocket-propulsion/>; its [CEA and Cantera notebook](https://github.com/kyleniemeyer/rocket-propulsion/blob/main/thermochemistry/cea_cantera.ipynb) is a useful thermochemistry reference.

## Study workflow

The PySide6 desktop app and CLI use the same service layer:

```powershell
rocket-study-desktop data/studies/rp1-lox-design-study.example.json
rocket-study doctor
rocket-study preview data/studies/rp1-lox-design-study.example.json
```

The app has Study, Screen, Export, Inbox, Results, Candidate, Finalists, and
Doctor views. Long operations appear in the job drawer. CLI actions include
`init`, `generate`, `screen`, `pack`, `unpack`, `import`, `rank`, `abandon`,
`status`, and `mark-stale`; run `rocket-study --help` for arguments. Doctor
output is informational and does not execute fix steps.

Study includes a local RPA4 report library with separate top-100 ranked and
top-100 recent views, plus a latest-versus-top-five comparison. Reports keep
stable IDs and PDFs even when the same candidate is imported again. The metric
profile supports up to five maximize, minimize, or target goals, with weighted,
priority, and per-metric rankings. Comparison differences are observations,
not proven causes. The archive defaults to `%LOCALAPPDATA%\RocketAnalysis\ReportArchive`
on Windows and can be changed in Settings. Server synchronization is not
configured yet; the local archive is the source of truth until a destination
and server type are selected.

The supplied [calibration study](data/studies/cal-study.json) is pre-staged
under [data/studies/calibration](data/studies/calibration/README.md). Copy its
`rpa4-machine` folder to the computer that runs RPA4. Keep the listed
calibration decisions on hold until the PDFs and notes are returned.

The example spec is [rp1-lox-design-study.example.json](data/studies/rp1-lox-design-study.example.json). It samples O/F, chamber pressure, throat diameter, nozzle exit area ratio, and the RPA4 nozzle inlet area-ratio field. Its ranges are placeholders around the supplied case, not approved hot-fire limits. Replace them with your design envelope, and set hard constraints from the actual feed system, test stand, cooling, materials, and instrumentation limits before selecting hardware.

The example uses Sobol sampling. Grid, explicit one-at-a-time, and persistent Optuna adaptive sampling are also available. Candidate IDs remain stable across repeated grid/Sobol generation, and adaptive trial state is stored in SQLite under the study output directory. Preview is pure; it does not create adaptive trials. Use `design_constraints` to reject infeasible parameter combinations before their CFGs are written, and set each parameter's `precision` to round values before candidate IDs are calculated.

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

Build an atomic zip pack from the app or CLI. Each case folder contains its CFG and manifest, with a run sheet and file hashes at the root. Run each CFG manually in RPA4 and save the PDF as `candidate.pdf` in that case folder, then zip the returned folders or PDFs. The importer pairs by the report's case name against project manifests, so PDF-only returns work. `RPA4_SHARED_DIR` can also be set for direct folder scans:

```powershell
$env:RPA4_SHARED_DIR = '\\RPA4-COMPUTER\RPA4\exports'
```

```python
imported = import_shared_rpa4_results(spec)
ranking = rank_study_results(spec)
save_study_ranking(spec, ranking)
```

The importer accepts empty and partial batches, records per-file problems, and reloads current files on each scan. Imported reports are copied into `imports/<candidate-id>/` with SHA-256 provenance, so results do not depend on the source share remaining available. Identical conflict copies collapse; different report contents for the same case are flagged for review. Adaptive Optuna trials are completed from imported report objectives, and `rocket-study abandon <study.json>` marks unreturned running trials failed.

See [the design-study guide](docs/design-studies.md) for all supported RPA4 parameters, metric names, constraints, adaptive-study behavior, and screening limits.

## Screening, ranking, and model scope

NASA CEA's bundled RP-1 and O2(L) data provide ideal equilibrium screening metrics such as characteristic velocity, exit Mach, and ideal specific impulse. The example uses finite-area chamber mode with the supplied report's contraction ratio of 6 when supported by the installed binding; set `finite_area_chamber` false for an infinite-area solve. The supplied PDF's reported propellant inlet temperatures are used when the example spec points to the reference PDF. CEA results are an independent code/database comparison, not experimental validation or predicted delivered performance.

Cantera screening is disabled until a study explicitly supplies a mechanism and fuel/oxidizer species mass-fraction mappings. The adapter returns an HP equilibrium chamber state only; it does not calculate nozzle performance. Do not treat an unvalidated surrogate as RP-1.

Pareto ranking and weighted scoring are separate modes. Default objectives maximize delivered sea-level Isp, delivered-to-ideal c-star efficiency, and delivered sea-level thrust coefficient, which avoid rewarding throat size alone. Delivered thrust can be made a target constraint after the stand limit is set. Derived report metrics include sea-level exit pressure ratio, an optional separation-risk indicator, c-star efficiency, and exit condensed mass fraction. Any parsed performance, station, propellant, geometry, mass-flow, or altitude-sweep metric can be selected as an objective or constraint. Weighted scores normalize each chosen metric from 0 to 1 over the eligible candidates in the current study; raw values are retained in the ranking output. Validation review warnings remain visible and eligible, while hard mismatches and failed constraints are excluded.

The nozzle inlet `areaRatio` is swept from 2 to 15 in the example, but the supplied PDF's “Nozzle inlet” station does not establish that it maps to the CFG field. The validator reports the difference as a review warning; it neither changes the source files nor blocks ranking on that point alone.

## Supplied reference case

`data/reference/Test6in.cfg`, `data/reference/test1-6in-of-1.5.pdf`, and `data/reference/test1-mach.png` are the transferred test files. The PDF parser extracts propellant conditions, performance tables, geometry, and exact Table 2 Mach samples. The generated station plot is saved at [test1-mach-station-validation.png](data/reference/test1-mach-station-validation.png). The snapshot and interpretation are in [rpa4-validation.md](docs/rpa4-validation.md).

## CFD finalist path

OpenFOAM 2512 is installed in Ubuntu 24.04 on WSL2. `openfoam_finalist_ids()` selects a limited ranked set, and the finalist helper can copy a supplied case template and substitute its explicit tokens. Running CFD requires an appropriate mesh, thermochemical/transport model, boundary conditions, and validated case setup supplied by the user. The included OpenFOAM tutorial is an installation smoke case, not validation against the RPA4 report. See [the solver comparison](docs/cfd-solver-comparison.md).

OpenMDAO can wrap callable local analysis functions with finite-difference derivatives. It is an optimization driver interface, not a physical model. CSV station results from OpenFOAM, SU2, or other solvers can be compared with the report Mach values through `rocket_analysis.cfd`.

## Main package modules

- `rocket_analysis.study`: versioned study specs, grid/Sobol/explicit/adaptive samples, feasibility screening, RPA4 CFG generation, screening, share staging, report import, and ranking persistence.
- `rocket_analysis.ranking`: report metric catalog, constraints, Pareto fronts, and weighted scoring.
- `rocket_analysis.service`: CLI/GUI orchestration, candidate statuses, cancellable jobs, stale and adaptive-trial actions, and Doctor checks.
- `rocket_analysis.bundles`: atomic zip export, safe inbox unpacking, PDF-only return pairing, import archival, and provenance.
- `rocket_analysis.cli` and `rocket_analysis.desktop`: command line and PySide6 entry points.
- `rocket_analysis.thermochemistry`: ideal CEA and explicitly configured Cantera screening adapters.
- `rocket_analysis.rpa4` and `rocket_analysis.rpa4_validation`: PDF parsing and CFG/report validation.
- `rocket_analysis.shared`: complete RPA4 shared-folder pairs, including partial batch loading.
- `rocket_analysis.openmdao_adapter`: callable analysis problem wrapper.
- `rocket_analysis.cfd`: solver-neutral station comparison and OpenFOAM finalist helpers.

## Verification

Run the project checks in `rocket-dev` with `python -m pytest`. These cover parsing, ranking, sampling and persistence, model adapters, shared-folder imports, and CFD finalist selection. Solver runs remain dependent on the user's RPA4 share and a suitable OpenFOAM case template.
