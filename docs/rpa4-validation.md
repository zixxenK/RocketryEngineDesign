# RPA 4 configuration validation

`data/reference/Test6in.cfg`, transferred from the computer running RPA 4.06, is the source of the case inputs. `load_rpa4_inputs()` imports its values into a unit-aware model. The PDF is the exported-results cross-check. `validate_rpa4_case()` compares them; its saved result is `data/reference/test1-validation.json`.

The current snapshot records 27 matches, no hard mismatches, four items for review, and one field the PDF cannot check. The nozzle inlet area-ratio difference is visible in the report but is warning-only because the CFG/PDF station mapping is not established.

## Matching values

The configuration and report agree on the case name, RP-1 fuel, liquid oxygen oxidizer, O/F 1.5, chamber pressure 2 MPa, 6 in throat diameter, 19.74 exit area ratio, one chamber, and the reported nozzle contour angles. The O/F ratio implies fuel and oxidizer shares of 0.4 and 0.6, matching the report's mass fractions. The ambient sweep endpoints also agree at 1.0 and 0.2 atm.

The report's Table 2 station Mach values are 0 at the injector, 0.0988 at the nozzle inlet, 1 at the throat, and 4.1322 at the exit. The toolkit plot shows these as labeled points without interpolating between them; the PNG is a visual cross-check and rounds the exit value to about 4.2.

The delivered-performance table's optimum-expansion ambient pressure is 0.0881 atm. The reported nozzle-exit pressure converts to about 0.0879 atm, which closes within the report's rounding. This checks the report's expansion condition. Multi-parameter studies now generate candidate CFGs for manual RPA4 execution and rank imported results by Pareto dominance or current-set weighted scoring. The workflow does not launch RPA4.

## Findings to resolve

| Field | CFG | PDF | Result |
|---|---:|---:|---|
| Nozzle inlet area ratio | 14.43 | 6.0 | Review warning: numeric difference, CFG-to-PDF field mapping is ambiguous |
| Chamber length | 1020 | 1020 mm | Numeric match; CFG value has no unit |
| Equilibrium shifting | `false` | Molecular weight varies from 17.6605 to 17.9438 by station | Review the RPA 4.06 flag meaning and compare the intended flow mode |
| Ambient samples | 30 | 31 rows including both endpoints | 30 intervals would produce 31 rows; confirm whether the CFG field counts intervals or points |
| `R2 / R2max` | 0.089 | R2 is reported, R2max is not | Cannot check from the supplied PDF |

### Nozzle inlet area-ratio investigation

The transferred CFG contains `nozzleConditions.nozzleInletConditions.areaRatio = 14.43`. The supplied PDF's Table 2 reports `A/At = 6.0` at its explicitly named Nozzle inlet station; its Injector station is also 6.0, while the throat is 1.0 and exit is 19.74. The CFG's exit area ratio, 19.74, agrees with the PDF.

This establishes a real numeric difference between the two supplied files. It does not establish whether the RPA 4.06 CFG field means the same calculation station as Table 2's label. The shared files contain no schema description or RPA4 run identifier tying this PDF to this exact CFG. The validator retains both values and records the mapping ambiguity as a review warning; it does not rewrite either value or silently treat the field as a match. The design-study example sweeps the CFG input from 2 to 15. Resolve the mapping by checking the RPA 4.06 field definition on the RPA4 computer or exporting a fresh PDF from this exact CFG. This warning alone does not remove a candidate from ranking.

## Report arithmetic

The delivered sea-level Isp converts to 1171.8878 m/s using standard gravity, versus 1171.8881 m/s in the report. Total mass flow times reported effective exhaust velocity gives 26.73276 kN, versus 26.7328 kN reported. Both close within printed rounding.

## Scope

This is a CFG-to-report consistency check plus arithmetic checks on the exported report. It does not launch RPA 4 or independently regenerate the report's combustion solution. The report parser is tailored to this PDF layout; shared-folder reports must retain the same table structure.

The local RPA application is v2.3.2 and uses a legacy CFG syntax, so it is not involved in reading or validating the transferred v4.06 JSON file. NASA CEA screening of this case gives characteristic velocity 1642.576 m/s and exit Mach 4.13154, closely matching the report's ideal c-star 1642.571 m/s and exit Mach 4.1322. This is an ideal-model cross-check only. Cantera remains unavailable for RP-1 until a mechanism and fuel surrogate mapping are explicitly supplied.
