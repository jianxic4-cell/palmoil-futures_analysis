# 棕榈油期货数据处理与分析

用于期货账单解析、开平仓匹配、P 棕榈油跨期价差研究，以及 Excel 分析报告生成。

[English](README.md) · [数据要求](docs/DATA.md) · [报告说明](docs/REPORTS.md) ·
[研究方法与局限](docs/METHODOLOGY.md) · [验证记录](docs/VALIDATION.md)

## 使用要求

- Python 3.10 或以上；本地验证环境为 Windows、64 位 Python 3.10。
- `requirements.txt` 中的依赖：NumPy、pandas、matplotlib、SQLAlchemy、psycopg、openpyxl。
- 查看工作簿需要 Excel 或兼容的表格软件；生成报告不需要安装 Excel。
- PNG 中文图表需要本机中文字体，例如微软雅黑或 Noto Sans CJK。
- 获取依赖时需要联网；之后的演示和 CSV 数据处理可以离线运行。

| 使用目的 | 额外需要准备什么 |
| --- | --- |
| 合成演示和测试 | 无须账户、API key 或数据库 |
| 账单分析与报告 | 符合支持格式的 TXT/XLSX 期货结算单 |
| 行情特征分析和回测 | 单合约与连续合约两份历史行情 CSV |
| 可选的数据库导入/筛选 | PostgreSQL、已配置的数据库与 `strategy` schema |

仓库不提供真实账单、账户信息、密码、授权行情或真实收益结果。项目用于研究与数据分析，不执行实盘下单。

## 先运行演示

在依赖已具备的环境中，从仓库根目录执行：

```text
python -m unittest discover -s tests -v
python scripts/demo.py
python scripts/demo_reports.py --previews
```

| 命令 | 生成的内容 |
| --- | --- |
| `demo.py` | `output/synthetic_demo/` 中的两份 CSV 和一张价差图 |
| `demo_reports.py` | `output/synthetic_reports/reports/` 中的两份 Excel |
| `demo_reports.py --previews` | 另外在 `output/synthetic_reports/previews/` 生成 PNG 图表 |

行情演示调用月度合约选择和价差特征函数。报告演示使用虚构成交与余额，
调用实际的 FIFO 匹配、统计与候选识别程序。演示均标记为合成数据，不代表历史收益。

## 使用自己的结算单

账户统计和候选分析按**每个工作目录一个账户**使用。解析器适配特定券商导出布局，
并非任意 Excel 都能直接识别；其他格式可能需要修改解析器。
详细字段见[账单格式要求](docs/DATA.md#broker-statement-layouts)。

1. 将结算单放入 `data/raw/年份/`。
2. 按实际数据年份运行解析与匹配，下面的年份仅为示例。
3. 检查 `output/parsing_errors.csv`，处理解析失败、缺少历史开仓和匹配歧义等记录，再解读统计结果。

```text
python scripts/parse_statements.py --start-year 2024 --end-year 2026
python scripts/match_trades.py
```

解析阶段生成 `transactions.csv`、`daily_accounts.csv`、`parsing_errors.csv`。
匹配阶段生成 `completed_trades.csv` 并更新错误记录。

生成交易风格报告：

```text
python scripts/analyze_trading_style.py
python scripts/build_style_report.py
```

生成套利候选报告：

```text
python scripts/identify_arbitrage.py
python scripts/build_arbitrage_report.py
```

两份报告默认保存在 `output/reports/style_report.xlsx` 和
`output/reports/arbitrage_report.xlsx`。报告包含格式化明细和数据说明；
交易风格报告还包含关联指标与图表。自定义目录、PNG 预览及全部前置文件见[报告说明](docs/REPORTS.md)。

## 棕榈油行情研究

完成候选识别后，另行准备[数据要求](docs/DATA.md)中的两份行情 CSV，再依次运行：

```text
python scripts/filter_p_calendar_spreads_pg.py
python scripts/analyze_p_calendar_entry_exit.py
python scripts/backtest_p_calendar_v1.py
python scripts/backtest_p_calendar_v2.py
python scripts/analyze_p_main_liquid_far.py
python scripts/build_p_daily_research_panel.py
```

这条流程完成 P/P 候选筛选、事件前行情特征、研究回测、月度流动性选约和每日面板。
混合品种账单在解析阶段保留所有品种，账户总权益不能当作 P 品种的单独权益。
完整历史流程需要足够的行情覆盖和前序输出，包括基准回测文件。
研究参数和年份划分位于相应脚本，口径见[方法说明](docs/METHODOLOGY.md)。

## 路径与数据库配置

| 环境变量 | 默认值或用途 |
| --- | --- |
| `FUTURES_WORK_ROOT` | 仓库根目录，包含 `data/` 与 `output/` |
| `FUTURES_STATEMENTS_DIR` | `工作根目录/data/raw` |
| `FUTURES_MARKET_DIR` | `工作根目录/data/market` |
| `PGHOST`、`PGPORT` | `localhost`、`5432` |
| `PGDATABASE`、`PGUSER` | `quant`、`postgres` |
| `PGPASSWORD` | 未设置时交互询问密码 |

环境变量须设置在运行脚本的进程中。`.env.example` 只是配置参考，**程序不会自动读取它**。
支持路径参数的命令可用参数覆盖默认值。`demo.py` 固定输出到仓库内；
`demo_reports.py` 使用工作根目录，也支持 `--output-dir`。

数据库是可选功能。`filter_p_calendar_spreads_pg.py --from-db` 在只读事务中读取
`strategy.arbitrage_candidates`。`import_to_pgsql.py --write` 会导入成交与候选数据，
需要预先创建数据库、`strategy` schema，并授予相应权限。默认遇到已有表会失败；
`--if-exists append` 重复执行可能重复入库。仅导入 Python 模块不会连接数据库。

## 常见问题

| 现象 | 检查方式 |
| --- | --- |
| `ModuleNotFoundError` | 确认实际运行命令的 Python 环境具备 `requirements.txt` 中的依赖 |
| 缺少 CSV / `FileNotFoundError` | 先执行前序步骤，检查工作根目录与 `--data-dir` |
| 未成功解析任何账单 | 先查看终端报错，核对源目录、年份和导出布局；本次不会更新输出 CSV，已有文件可能是上次运行结果 |
| 部分账单解析成功，但有缺工作表、表头或其他解析错误 | 查看本次生成的 `parsing_errors.csv`，核对导出布局；运行完成不代表所有账单均解析成功 |
| 缺少历史开仓 | 用 `match_trades.py --prior-statement PATH` 指定较早的持仓快照 |
| 交易风格统计因月末数据缺失而失败 | 使用完整月度账单，包含券商报告的月末权益 |
| PNG 中文显示为方框 | 检查本机是否有可用的中文字体 |
| 预览器中的总览指标为空 | 用能够重新计算公式的表格软件打开工作簿 |
| 保存 Excel 时权限错误 | 关闭正在打开的同名工作簿，并检查目录写入权限 |
| 数据库缺少 schema 或表 | 使用默认 CSV 流程，或先准备要求的数据库对象 |

## 目录说明

| 位置 | 用途 |
| --- | --- |
| `scripts/parse_statements.py`、`xlsx_reader.py` | 账单解析 |
| `scripts/match_trades.py` | FIFO 开平仓匹配 |
| `scripts/identify_arbitrage.py` | 对冲套利候选评分 |
| `scripts/analyze_trading_style.py` | 账户与交易统计 |
| `scripts/build_style_report.py`、`build_arbitrage_report.py` | Excel 报告入口 |
| `scripts/report_workbook.py` | 报告排版与 PNG 图表 |
| `scripts/filter_p_calendar_spreads_pg.py` | CSV / 数据库 P/P 筛选 |
| `scripts/analyze_p_calendar_entry_exit.py` | 开平仓事件前行情特征 |
| `scripts/backtest_p_calendar_v1.py`、`backtest_p_calendar_v2.py` | 研究回测 |
| `scripts/analyze_p_main_liquid_far.py` | 月度流动性选约 |
| `scripts/build_p_daily_research_panel.py` | 每日特征与事件面板 |
| `scripts/anonymize_statements.py` | 本地账单匿名化辅助 |
| `scripts/import_to_pgsql.py`、`database.py` | 可选数据库导入与配置 |
| `scripts/demo.py`、`demo_reports.py` | 合成数据演示 |
| `tests/`、`docs/` | 离线测试与详细文档 |

## 结果局限与发布

FIFO 是账务重建假设，候选评分不能证明交易意图。同一持仓腿可能出现在多个候选中，
因此不能直接汇总全部候选收益。TXT 时间和日度权益估算有精度限制。
回测采用研究假设，不能据此认定实盘盈利能力。详见[研究局限](docs/METHODOLOGY.md)。

生成的数据与报告属于本地输出，已由 `.gitignore` 排除；网页手动上传和已经跟踪的文件
不受此规则保护。发布前核对 [PUBLIC_FILES.md](PUBLIC_FILES.md) 和[发布清单](docs/PUBLISHING.md)。

当前仓库未包含 `LICENSE` 文件。
