# Validation record

## Import identifier regression check — 2026-09-17

- Eleven new offline tests cover leading-zero English/Chinese identifiers,
  long numeric identifiers, literal NA/NULL values, missing identifiers and
  continued numeric inference for quantities/amounts.
- Mocked CLI tests verify exact values and explicit SQL TEXT types are passed
  to the writer, no connection is requested without `--write`, and incompatible
  append targets stop before either table is written.
- Existing TEXT/VARCHAR columns and new-table targets are accepted in schema
  inspection tests. Database inspection and writes are mocked throughout.
- All 46 offline tests passed in the existing local Python environment.
- No live PostgreSQL connection, schema migration or repair of previously
  imported values was performed. The separate report-number issue is unchanged.

## Opening-balance regression check — 2026-09-17

- Nine new account-profit tests cover first-day deposits, withdrawals, realized
  gains/losses, fees, combined movements, reversed daily rows and a year boundary,
  as well as the unchanged baseline.
- In the synthetic first-day deposit case, a 1,000 deposit leaves the first month's
  profit at 192, rather than -808; two-month and annual profit both equal 384.
- Monthly totals, annual totals and cumulative account profit are checked against
  independently constructed synthetic ledger movements.
- All 35 offline tests passed in the existing local Python environment.
- No real account outputs or database records were changed. Report-number
  conversion and database-identifier issues remain outside this fix.

## Input safety regression check — 2026-09-17

- All 26 offline tests passed in the existing local Python environment.
- Ten input-safety tests cover missing/non-directory sources, empty directories,
  wrong-year selection, all statements failing, duplicate TXT statements,
  duplicate XLSX metadata and mixed TXT/XLSX duplicates.
- Failure cases check a nonzero exit status and byte-for-byte preservation of all
  three existing output files. Duplicate XLSX discovery uses a stubbed reader;
  the earlier report integration tests exercise actual synthetic XLSX layouts.
- Valid zero-trade statements and different-month statements remain accepted.
- No database access or private statements were used. These checks do not fix
  unrelated monthly-profit, report-number-conversion or database-identifier issues.

## Clean-environment check — 2026-09-14

Platform: Windows, 64-bit CPython 3.10.0. Dependencies were resolved from
`requirements.txt` into a new virtual environment without system site packages.

- Dependency installation completed and `pip check` reported no broken requirements.
- All 16 offline tests passed.
- Synthetic XLSX and GB18030 TXT broker-layout cases passed the full sequence:
  parsing, FIFO matching, trading-style statistics, candidate identification and both
  Excel report builders. Each case preserved the synthetic account identifier,
  produced four executions and reconciled without parsing/matching errors.
- The market demo produced 344 synthetic price rows, three monthly selections,
  two CSV outputs and one chart.
- The report demo processed eight synthetic executions into four completed trades,
  two months and two candidate pairs. Both Excel reports and five PNG summaries were produced.
- CSV-mode P/P filtering exported two synthetic candidates to CSV and Excel.
- Report tests cover saved values, formula/chart references, changed dates/column
  order, empty results, missing inputs, identifier preservation and literal CSV text.

## Dependency versions in the clean environment

| Package | Version |
| --- | --- |
| numpy | 2.2.6 |
| pandas | 2.3.3 |
| matplotlib | 3.10.9 |
| SQLAlchemy | 2.0.52 |
| psycopg / psycopg-binary | 3.3.5 |
| openpyxl | 3.1.5 |

These are observed test versions. `requirements.txt` specifies compatible ranges,
not a fully pinned environment, so another installation may resolve different versions.

## Coverage boundaries

All validation inputs were synthetic. No private statements, database connections,
market-data downloads or order execution were involved. Synthetic layout tests do not
establish compatibility with every broker's exports or verify every parser branch.
The full historical research/backtest pipeline was not rerun with real market data.

Workbook values and chart references were checked by reopening the exported files.
PNG summaries were generated; Excel desktop rendering was not automated. Formula-only
previewers can display blank cover metrics until a spreadsheet application recalculates them.

GitHub Actions is configured for Ubuntu with Python 3.10 and 3.12, including both
demonstrations. This local record does not claim a completed remote workflow run.
Local macOS/Linux execution was not part of this check.
