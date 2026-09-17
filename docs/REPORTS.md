# Excel report generation / Excel 报告生成

The report builders present existing analysis results. They do not reparse statements,
recalculate the upstream statistics, connect to a database or download market data.
They use `openpyxl` and (for optional PNGs) `matplotlib`, already in `requirements.txt`.
Report generation requires only the Python dependencies listed above.

两套报告分别用于交易风格分析和套利候选展示，包含工作表、图表、来源与数据局限说明。
日期和 Excel 图表数据范围随 CSV 更新；指标按名称查找，图表按列名查找。

## Quick synthetic demonstration

Run from the repository root after installing `requirements.txt`:

```powershell
python scripts/demo_reports.py --previews
```

This generates deterministic, invented executions and account balances in
`output/synthetic_reports/`, runs the actual matcher and analysis scripts, then creates:

- `output/synthetic_reports/reports/style_report.xlsx`
- `output/synthetic_reports/reports/arbitrage_report.xlsx`
- `output/synthetic_reports/previews/style/*.png`
- `output/synthetic_reports/previews/arbitrage/*.png`

The demo labels workbook titles and PNGs as synthetic. The data and results are
invented, not historical performance. The demo reads no
private statements. Its output directory can be changed with `--output-dir`.

## Generate reports from your own analysis

After parsing and matching statements as described in [DATA.md](DATA.md):

```powershell
python scripts/analyze_trading_style.py
python scripts/build_style_report.py
python scripts/identify_arbitrage.py
python scripts/build_arbitrage_report.py
```

Defaults respect `FUTURES_WORK_ROOT`: input is `<working root>/output`, and the Excel
files are `<input directory>/reports/style_report.xlsx` and `arbitrage_report.xlsx`.
To specify separate input, output and preview directories:

```powershell
python scripts/build_style_report.py --data-dir output --output output/reports/style_report.xlsx --preview-dir output/previews/style
python scripts/build_arbitrage_report.py --data-dir output --output output/reports/arbitrage_report.xlsx --preview-dir output/previews/arbitrage
```

Both builders accept `--help`. Existing output files with the same names are overwritten.
Missing files or required chart/metric columns cause a nonzero exit with an input error.
Use upstream CSVs from this repository, keeping each set from the same analysis run.

## Report contents and required input files

Trading style: the cover sheet contains six linked metrics and annual/product charts.
Monthly equity and product drawdown charts appear on their corresponding data sheets.

| CSV from `analyze_trading_style.py` | Excel worksheet |
| --- | --- |
| trading_style_summary.csv | 指标明细 |
| yearly_performance.csv | 年度表现 |
| monthly_performance.csv | 月度表现 |
| product_performance.csv | 品种分析 |
| product_risk_analysis.csv | 品种波动回撤 |
| direction_performance.csv | 方向分析 |
| holding_period_analysis.csv | 持仓分析 |
| weekday_performance.csv | 星期分析 |
| time_performance.csv | 时段分析 |
| equity_drawdown.csv | 权益回撤 |
| behavior_analysis.csv | 行为分析 |

Candidate report: the cover uses `arbitrage_metadata.json` for the actual period,
counts and score threshold. All files below come from `identify_arbitrage.py`.

| CSV | Excel worksheet |
| --- | --- |
| arbitrage_candidates.csv | 套利组合清单 |
| arbitrage_priority_review.csv | 优先核对 |
| arbitrage_pair_summary.csv | 品种配对汇总 |
| arbitrage_leg_episodes.csv | 逐腿区间 |
| arbitrage_checks.csv | 检查 |

All listed inputs must exist. Empty candidate results and header-only time-analysis
results are displayed as no data. The builders preserve input ordering; the upstream
producer sorts product profit and drawdown tables used by the leading-item charts.

## Interpretation and portability

- The CSV files remain the source of the analysis. Regenerate the workbook after
  changing CSV inputs; there is no live connection to the CSVs.
- Cover metrics are Excel formulas linked to imported data sheets. Excel or a compatible
  spreadsheet application recalculates them on opening. Readers that do not evaluate
  formulas may show empty cover metrics; raw imported values are still available.
- PNGs are independent chart summaries, not screenshots of every Excel worksheet.
  They show annual profit, monthly equity, product profit/drawdown or candidate counts.
  Chinese PNG labels need a local CJK font (for example Microsoft YaHei on Windows
  or Noto Sans CJK on Linux). Excel rendering and font substitution depend on the viewer.
- TXT timestamps, estimated daily equity and FIFO reconstruction retain their upstream
  limitations. The report does not establish that candidates were intentional arbitrage.
- Candidate legs can repeat across pairs. Do not sum all candidate-pair profits.
- Account identifiers and source information in local inputs can appear in report sheets.
  Generated workbooks, CSVs and PNGs stay local and are ignored by Git; do not manually
  upload private reports with the source code.

## Offline checks

```powershell
python -m unittest discover -s tests -v
python scripts/demo_reports.py
```

The tests generate synthetic inputs through the existing matcher/analysis stages,
then reopen generated Excel files to verify key values and chart references. They also
exercise empty results, alternate column order, missing files and text identifiers.
The same tests and synthetic report command run in the GitHub Actions workflow.
