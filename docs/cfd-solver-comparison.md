# CFD solver comparison

## Benchmark target

Use the supplied RPA4 case as the initial comparison target: RP-1/O2(L), O/F 1.5, 2 MPa chamber pressure, 152.40 mm throat diameter, 677.11 mm exit diameter, and contraction ratio 6. The PDF gives Mach values 0, 0.0988, 1, and 4.1322 at the four stations; the PNG is a visual cross-check.

The report identifies propellants and chamber pressure, but not the complete wall heat-transfer and CFD boundary conditions or the thermochemical mechanism needed for a reacting-flow reproduction. Do not claim CFD validation until those inputs are supplied. Begin with solver suitability and a compressible nozzle case; extend to reacting flow when the remaining inputs and mechanism are known.

## Candidate comparison

| Criterion | OpenFOAM | SU2 |
|---|---|---|
| WSL2 Linux support | Strong | Strong |
| Compressible nozzle flow | Strong | Strong |
| Reacting-flow and combustion model breadth | Broad model family; case setup is involved | More limited for this liquid-engine use case |
| Python integration | CLI and file based; Python post-processing can consume exported fields | CLI and file based; Python workflow can consume mesh and solution files |
| Initial fit | Preferred for a later reacting chamber/nozzle case | Useful comparison candidate for compressible nozzle flow |

## Decision

Use OpenFOAM 2512 as the first CFD integration target. Ubuntu 24.04 WSL2 has the solver and tutorials installed, and a `reactingFoam` choked-nozzle tutorial is copied to `/home/rocket/OpenFOAM/cases/test1-chokedNozzle`. Keep the case runner and result reader solver-neutral so SU2 output can be evaluated later. Keep meshes and solver cases in the Ubuntu WSL2 filesystem; exchange a station CSV (`station,mach`) and exported visualization files with the Windows Conda workflow.

## Smoke check

The copied tutorial's `system/controlDict` is set to `endTime 0.4` for a practical installation smoke run. Its `Allrun` completed through simulated time 0.4, wrote the `End` marker in `log.reactingFoam`, and completed `reconstructPar` with exit status 0. The Python WSL wrapper was separately invoked from `rocket-dev` against a minimal WSL `Allrun` script and returned successfully. The project includes helpers to select a small top-ranked finalist set and prepare copies of a supplied OpenFOAM template. The template must define the geometry, mesh, thermochemistry, and boundary conditions; the toolkit does not synthesize these inputs from RPA4 values.

On Windows, finalist case destinations must be WSL UNC paths such as
`\\wsl$\Ubuntu-24.04\home\rocket\OpenFOAM\cases\candidate`. Case preparation
copies to a temporary sibling directory and renames it into place after all
template substitutions succeed. Running a prepared case accepts that UNC path
and resolves it to a Linux path inside the selected distribution.

These checks establish that the environment, solver launch path, and basic result-ingestion package work. They do not validate the tutorial's flow against the RPA4 report or validate an RPA4 reconstruction. The report identifies RP-1/O2(L) and chamber pressure, but a reacting CFD comparison also needs a suitable RP-1 surrogate, mechanism, injector/outlet conditions, wall treatment, and mesh/convergence evidence. CFD results should remain finalist comparisons until those inputs are defined.
