# Palm Oil Futures Data Processing and Analysis

Tools for parsing futures statements, reconstructing trades, analysing palm-oil (P)
calendar spreads and generating Excel reports.

[中文说明](README.zh-CN.md) · [Data requirements](docs/DATA.md) ·
[Report guide](docs/REPORTS.md) · [Methodology](docs/METHODOLOGY.md) ·
[Validation](docs/VALIDATION.md)

## Requirements

- Python 3.10 or later. Local validation uses 64-bit Python 3.10 on Windows.
- Dependencies in `requirements.txt`: NumPy, pandas, matplotlib, SQLAlchemy,
  psycopg and openpyxl.
- Excel or a compatible viewer to view workbooks. Excel is not needed to generate them.
  Chinese PNG labels need a local CJK font.
- Obtaining dependencies requires internet access; demonstrations and CSV analysis
  can run offline afterwards.

| Use case | Additional requirements |
| --- | --- |
| Synthetic demonstrations and tests | None; no account, API key or database needed |
| Statement analysis and reports | Supported broker TXT/XLSX statements |
| Market features and research backtests | Separate contract and continuation price histories |
| Optional database import/filtering | PostgreSQL, a configured database and the `strategy` schema |

No private statements, credentials, licensed market history or real performance
results are included. The project provides research tools, not an order execution system.

## Try the demonstrations

With the required dependencies available, run from the repository root:

```text
python -m unittest discover -s tests -v
python scripts/demo.py
python scripts/demo_reports.py --previews
```

| Command | Output |
| --- | --- |
| `demo.py` | Two CSVs and one spread chart in `output/synthetic_demo/` |
| `demo_reports.py` | Two Excel files in `output/synthetic_reports/reports/` |
| `demo_reports.py --previews` | Also writes PNG charts in `output/synthetic_reports/previews/` |

The market demo exercises monthly selection and spread features. The report demo
uses invented executions and balances with the actual matcher and analysis scripts.
Both are synthetic and do not reproduce historical performance.

## Analyse your own statements

Use one account per working directory for account statistics and candidate analysis.
The parser supports specific export layouts, not arbitrary broker spreadsheets.
See [supported layouts](docs/DATA.md#broker-statement-layouts).

1. Put supported statements in `data/raw/<year>/`.
2. Run the parser and matcher below, choosing the years present in the data.
3. Inspect `output/parsing_errors.csv`, including missing-history and matching warnings,
   before interpreting downstream results.

```text
python scripts/parse_statements.py --start-year 2024 --end-year 2026
python scripts/match_trades.py
```

The parser writes `transactions.csv`, `daily_accounts.csv` and `parsing_errors.csv`.
The matcher writes `completed_trades.csv` and updates the error file.

Trading-style report:

```text
python scripts/analyze_trading_style.py
python scripts/build_style_report.py
```

Arbitrage-candidate report:

```text
python scripts/identify_arbitrage.py
python scripts/build_arbitrage_report.py
```

Default workbooks are `output/reports/style_report.xlsx` and
`output/reports/arbitrage_report.xlsx`. They contain formatted sheets and explanatory
notes; the style report also contains linked metrics and charts.
See [REPORTS.md](docs/REPORTS.md) for custom paths, PNGs and all input files.

## Palm-oil market research

After candidate identification, supply the two market-history CSVs specified in
[DATA.md](docs/DATA.md), then run these stages in order:

```text
python scripts/filter_p_calendar_spreads_pg.py
python scripts/analyze_p_calendar_entry_exit.py
python scripts/backtest_p_calendar_v1.py
python scripts/backtest_p_calendar_v2.py
python scripts/analyze_p_main_liquid_far.py
python scripts/build_p_daily_research_panel.py
```

This workflow filters P/P candidates, aligns pre-event features, runs research
backtests and builds daily panels. Mixed-product statements remain mixed at the
parsing stage; account totals must not be interpreted as P-only performance.
Parameters and date splits are defined in the scripts and explained in
[methodology](docs/METHODOLOGY.md). A full historical run needs adequate market
coverage and earlier-stage outputs, including the baseline backtest files.

## Paths and database configuration

| Setting | Default / meaning |
| --- | --- |
| `FUTURES_WORK_ROOT` | Repository root; contains `data/` and `output/` |
| `FUTURES_STATEMENTS_DIR` | `<working root>/data/raw` |
| `FUTURES_MARKET_DIR` | `<working root>/data/market` |
| `PGHOST`, `PGPORT` | `localhost`, `5432` |
| `PGDATABASE`, `PGUSER` | `quant`, `postgres` |
| `PGPASSWORD` | Prompted interactively when absent |

Set variables in the process running the scripts. `.env.example` is a reference
template and is **not loaded automatically**. Explicit CLI paths override defaults
where available. `demo.py` always writes under the repository root;
`demo_reports.py` follows the working root and accepts `--output-dir`.

Database access is optional. `filter_p_calendar_spreads_pg.py --from-db` reads
`strategy.arbitrage_candidates` in a read-only transaction.
`import_to_pgsql.py --write` imports completed trades and candidates. The database
and schema must already exist, with appropriate access permissions.
The importer fails on existing tables by default; repeated `--if-exists append`
runs can duplicate records. Importing Python modules does not connect.

## Common problems

| Symptom | Check |
| --- | --- |
| `ModuleNotFoundError` | The interpreter must have the dependencies in `requirements.txt` |
| Missing CSV / `FileNotFoundError` | Run preceding stages; check working root and `--data-dir` |
| No statements successfully parsed | Read the terminal error first; check the source directory, years and supported layout. Output CSVs are not updated, so existing files may be from a previous run |
| Some statements parsed, but others have missing worksheets/headers or other parsing errors | Inspect the newly generated `parsing_errors.csv` and check the export layout. A completed run does not mean every statement parsed successfully |
| Missing opening history | Use `match_trades.py --prior-statement PATH` for an earlier position snapshot |
| Missing month-end data in style analysis | Supply complete monthly statements with reported month-end equity |
| Chinese PNG labels are squares | Use a local CJK font such as Microsoft YaHei or Noto Sans CJK |
| Empty cover metrics in a previewer | Open in a spreadsheet application that recalculates formulas |
| Permission error saving a workbook | Close it in Excel and check folder write access |
| Database schema/table missing | Use the default CSV path or provide the documented database objects |

## Repository layout

| Location | Purpose |
| --- | --- |
| `scripts/parse_statements.py`, `xlsx_reader.py` | Statement parsing |
| `scripts/match_trades.py` | FIFO open/close reconstruction |
| `scripts/identify_arbitrage.py` | Candidate scoring |
| `scripts/analyze_trading_style.py` | Account/trade summaries |
| `scripts/build_style_report.py`, `build_arbitrage_report.py` | Excel report entry points |
| `scripts/report_workbook.py` | Shared report formatting and PNG charts |
| `scripts/filter_p_calendar_spreads_pg.py` | P/P filtering from CSV or PostgreSQL |
| `scripts/analyze_p_calendar_entry_exit.py` | Pre-event market features |
| `scripts/backtest_p_calendar_v1.py`, `backtest_p_calendar_v2.py` | Research backtests |
| `scripts/analyze_p_main_liquid_far.py` | Monthly liquidity-based selection |
| `scripts/build_p_daily_research_panel.py` | Daily feature/event panel |
| `scripts/anonymize_statements.py` | Local statement anonymization helper |
| `scripts/import_to_pgsql.py`, `database.py` | Optional database import/configuration |
| `scripts/demo.py`, `demo_reports.py` | Synthetic demonstrations |
| `tests/`, `docs/` | Offline tests and reference documentation |

## Limits and publication

FIFO is a reconstruction assumption. Candidate scores do not establish trading intent,
and repeated legs mean candidate profits cannot simply be summed. TXT timestamps and
estimated daily equity have precision limitations. Backtests use research assumptions
and do not demonstrate live profitability. See [methodology](docs/METHODOLOGY.md).

Generated files are local outputs excluded by `.gitignore`; this does not protect
manual uploads or already tracked files. Review [PUBLIC_FILES.md](PUBLIC_FILES.md)
and the [publishing checklist](docs/PUBLISHING.md) before publishing.

No `LICENSE` file is currently included.
