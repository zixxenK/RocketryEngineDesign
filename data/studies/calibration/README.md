# RPA4 calibration study

This is the supplied v2 calibration pack, staged for the local importer.

- Study: `../cal-study.json`
- Imported candidate manifests and CFGs: `cal/candidates/`
- Files to carry to the RPA4 computer: `rpa4-machine/`
- Procedure and notes: `RUN-SHEET.md`

The 13 runs are manual RPA4 experiments. The inlet-area-ratio, multiphase,
and CFG serialization validation rules remain on hold until their listed runs
are returned. `cal-study.json` is for importing and ranking the pre-staged
cases; its 20-candidate limit intentionally prevents generating its 256-point
grid.

Returned PDFs can be imported from a zip, a folder, or an individual PDF:

```powershell
rocket-study import data/studies/cal-study.json --source path\to\results-cal.zip
rocket-study rank data/studies/cal-study.json
```

The importer copies PDFs into the study's `imports/` folder and records their
hashes. It does not run RPA4 or infer the outcome of an experiment that has not
been returned.
