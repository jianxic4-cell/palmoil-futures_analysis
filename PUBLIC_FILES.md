# Public release file inventory

The release consists of the source, tests, configuration template and documentation
listed below. Data, generated reports, virtual environments and Python caches are
excluded. No real datasets are bundled.

## Root and automation

- README.md
- README.zh-CN.md
- PUBLIC_FILES.md
- requirements.txt
- .gitignore
- .env.example
- .github/workflows/tests.yml

## Documentation

- docs/DATA.md
- docs/METHODOLOGY.md
- docs/PUBLISHING.md
- docs/VALIDATION.md
- docs/REPORTS.md

## Main research code

- scripts/project_config.py
- scripts/database.py
- scripts/parse_statements.py
- scripts/xlsx_reader.py
- scripts/match_trades.py
- scripts/anonymize_statements.py
- scripts/identify_arbitrage.py
- scripts/analyze_trading_style.py
- scripts/import_to_pgsql.py
- scripts/filter_p_calendar_spreads_pg.py
- scripts/analyze_p_calendar_entry_exit.py
- scripts/backtest_p_calendar_v1.py
- scripts/backtest_p_calendar_v2.py
- scripts/analyze_p_main_liquid_far.py
- scripts/build_p_daily_research_panel.py
- scripts/demo.py
- scripts/report_workbook.py
- scripts/build_style_report.py
- scripts/build_arbitrage_report.py
- scripts/demo_reports.py

## Offline tests

- tests/test_research.py
- tests/test_account_profit.py
- tests/test_database_import.py
- tests/test_statement_safety.py
- tests/test_reports.py
