# Calibration run sheet (RPA4 4.06)

Each folder in `for-rpa4-machine/` holds one case: `candidate.cfg` + `manifest.json`.

## Procedure (per case)
1. Open `candidate.cfg` in RPA4. Run it. Do not change anything and do not re-save the CFG.
2. Confirm the case name shown in RPA4 equals the folder name (e.g. `cal-R00`).
3. Export the PDF and save it **into the same case folder** as `candidate.pdf`.
4. If RPA4 shows any error, warning, or silently changes a value, write it in the Notes column. A failed load is a result, not a mistake.

## Order
Priority 1 first (R00–R05). They settle the open validation items. Priority 2 are parser/scaling fixtures. Priority 3 test whether RPA4 accepts what the generator writes.

| Run | Case name | Change | Pri | What to look at |
|---|---|---|---|---|
| R00 | `cal-R00` | baseline, name only changed | 1 | Table 2 'Nozzle inlet' A/At: is it still 6.0? Do all other values match the original PDF? |
| R01 | `cal-R01` | nozzle inlet areaRatio 2 | 1 | Does ANY number in the report change versus R00? |
| R02 | `cal-R02` | nozzle inlet areaRatio 8 | 1 | Same as R01 |
| R03 | `cal-R03` | nozzle inlet areaRatio 15 | 1 | Same as R01; top of the planned sweep range. Any error or warning? |
| R04 | `cal-R04` | equilibrium shifting true | 1 | Do Isp, c*, and Table 3 composition change versus R00? Which of R00/R04 matches the ORIGINAL PDF? |
| R05 | `cal-R05` | ambient numberOfPoints 10 | 1 | Table 6 rows: 10 or 11? (points vs intervals) |
| R06 | `cal-R06` | O/F 2.3 | 2 | Parser fixture with different values; Table 3 condensed-carbon behavior |
| R07 | `cal-R07` | exit areaRatio 10 | 2 | Fixture for the separation flag; check the optimum-expansion altitude row still prints two numbers |
| R08 | `cal-R08` | chamber length 1200 (no unit key) | 2 | Does Lc become 1200.00 mm? (confirms the unit is mm) |
| R09 | `cal-R09` | throat diameter 4 in | 2 | Scaling fixture: thrust and mass flow should scale with throat area |
| R10 | `cal-R10` | chamber length 1020 WITH units key | 3 | Does RPA4 accept the extra 'units' key the generator adds? Any load error? |
| R11 | `cal-R11` | baseline in generator format | 3 | Does RPA4 open a pretty-printed (indent=2, trailing newline) CFG exactly like R00? Emulates generate_candidate_cfgs output. |
| R12 | `cal-R12` | multiphase false | 3 | Does the 'Condensed fraction' row disappear from Table 3? (tests the validator's multiphase check) |

## Checklist
- [ ] R00  `cal-R00`   Notes: ______
- [ ] R01  `cal-R01`   Notes: ______
- [ ] R02  `cal-R02`   Notes: ______
- [ ] R03  `cal-R03`   Notes: ______
- [ ] R04  `cal-R04`   Notes: ______
- [ ] R05  `cal-R05`   Notes: ______
- [ ] R06  `cal-R06`   Notes: ______
- [ ] R07  `cal-R07`   Notes: ______
- [ ] R08  `cal-R08`   Notes: ______
- [ ] R09  `cal-R09`   Notes: ______
- [ ] R10  `cal-R10`   Notes: ______
- [ ] R11  `cal-R11`   Notes: ______
- [ ] R12  `cal-R12`   Notes: ______

## Return
Zip the case folders (PDFs inside them) as `results-cal-<date>.zip` and move them by USB, OneDrive, or email.
