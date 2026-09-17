
from __future__ import annotations

from project_config import PROJECT_ROOT, RAW_STATEMENTS_DIR, MARKET_DATA_DIR

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev


def number(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def read_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def safe_div(numerator, denominator):
    return numerator / denominator if denominator else ""


def corr(xs, ys):
    if len(xs) < 2 or len(xs) != len(ys):
        return ""
    avg_x, avg_y = mean(xs), mean(ys)
    dx = [x - avg_x for x in xs]
    dy = [y - avg_y for y in ys]
    denominator = math.sqrt(sum(x * x for x in dx) * sum(y * y for y in dy))
    return sum(x * y for x, y in zip(dx, dy)) / denominator if denominator else ""


def summarize_trades(rows):
    count = len(rows)
    wins = [r for r in rows if r["net_pnl"] > 0]
    losses = [r for r in rows if r["net_pnl"] < 0]
    gross_profit = sum(r["net_pnl"] for r in wins)
    gross_loss = -sum(r["net_pnl"] for r in losses)
    avg_win = safe_div(gross_profit, len(wins))
    avg_loss = safe_div(gross_loss, len(losses))
    total_qty = sum(r["quantity"] for r in rows)
    return {
        "平仓次数": count,
        "手数": total_qty,
        "盈利次数": len(wins),
        "亏损次数": len(losses),
        "胜率": safe_div(len(wins), count),
        "毛盈利": gross_profit,
        "毛亏损": gross_loss,
        "净收益": sum(r["net_pnl"] for r in rows),
        "平均每笔": safe_div(sum(r["net_pnl"] for r in rows), count),
        "平均盈利": avg_win,
        "平均亏损": avg_loss,
        "盈亏比": safe_div(avg_win, avg_loss) if avg_win != "" and avg_loss != "" else "",
        "利润因子": safe_div(gross_profit, gross_loss),
        "平均持仓天数": safe_div(sum(r["holding_days"] * r["quantity"] for r in rows), total_qty),
    }


def aggregate_trade_groups(completed):
    groups = {}
    for row in completed:
        key = (
            row.get("exit_source_file", ""), row.get("exit_trade_id", ""),
            row.get("contract", ""), row.get("direction", ""), row.get("exit_datetime", ""),
        )
        item = groups.setdefault(key, {
            "contract": row.get("contract", ""), "product": row.get("product", ""),
            "direction": row.get("direction", ""), "exit_datetime": row.get("exit_datetime", ""),
            "exit_date": row.get("exit_trading_date", ""), "quantity": 0.0,
            "gross_pnl": 0.0, "fees": 0.0, "net_pnl": 0.0,
            "holding_weighted": 0.0, "all_exact": True,
        })
        qty = number(row.get("quantity"))
        item["quantity"] += qty
        item["gross_pnl"] += number(row.get("broker_realized_pnl"))
        item["fees"] += number(row.get("total_fee"))
        item["net_pnl"] += number(row.get("net_pnl_after_fees"))
        item["holding_weighted"] += number(row.get("holding_days_approx")) * qty
        item["all_exact"] = item["all_exact"] and row.get("holding_time_quality") == "EXACT"
    result = []
    for item in groups.values():
        item["holding_days"] = safe_div(item["holding_weighted"], item["quantity"]) or 0.0
        item["exit_dt"] = parse_dt(item["exit_datetime"])
        result.append(item)
    return sorted(result, key=lambda r: (r["exit_datetime"], r["contract"], r["direction"]))


def grouped_stats(trades, field):
    groups = defaultdict(list)
    for trade in trades:
        groups[trade[field]].append(trade)
    return groups


def stats_row(label, rows, label_name):
    return {label_name: label, **summarize_trades(rows)}


def build_product_risk_rows(trades):
    """按平仓日已实现净收益，计算每个品种的波动和回撤。"""
    result = []
    for product, rows in grouped_stats(trades, "product").items():
        daily_pnl = defaultdict(float)
        for trade in rows:
            exit_date = trade.get("exit_date", "")
            if not exit_date and trade.get("exit_dt"):
                exit_date = trade["exit_dt"].date().isoformat()
            if exit_date:
                daily_pnl[exit_date] += trade["net_pnl"]

        dates = sorted(daily_pnl)
        daily_values = [daily_pnl[date] for date in dates]
        trade_values = [trade["net_pnl"] for trade in rows]

        cumulative = 0.0
        peak = 0.0
        peak_date = dates[0] if dates else ""
        max_drawdown = 0.0
        max_drawdown_start = ""
        max_drawdown_trough = ""
        max_drawdown_peak_value = 0.0
        underwater_start = None
        longest_underwater_days = 0
        curve = []

        for date in dates:
            cumulative += daily_pnl[date]
            curve.append((date, cumulative))
            if cumulative >= peak:
                if underwater_start:
                    duration = (datetime.fromisoformat(date) - datetime.fromisoformat(underwater_start)).days
                    longest_underwater_days = max(longest_underwater_days, duration)
                peak = cumulative
                peak_date = date
                underwater_start = None
                continue

            if underwater_start is None:
                underwater_start = peak_date or date
            drawdown = peak - cumulative
            if drawdown > max_drawdown:
                max_drawdown = drawdown
                max_drawdown_start = peak_date or date
                max_drawdown_trough = date
                max_drawdown_peak_value = peak

        if underwater_start and dates:
            duration = (datetime.fromisoformat(dates[-1]) - datetime.fromisoformat(underwater_start)).days
            longest_underwater_days = max(longest_underwater_days, duration)

        recovery_date = ""
        if max_drawdown > 0 and max_drawdown_trough:
            for date, value in curve:
                if date > max_drawdown_trough and value >= max_drawdown_peak_value:
                    recovery_date = date
                    break

        net_profit = sum(trade_values)
        result.append({
            "品种": product,
            "平仓次数": len(rows),
            "活跃平仓日": len(dates),
            "净收益": net_profit,
            "平仓日收益波动": pstdev(daily_values) if len(daily_values) > 1 else 0.0,
            "单笔收益波动": pstdev(trade_values) if len(trade_values) > 1 else 0.0,
            "最大回撤金额": max_drawdown,
            "回撤开始": max_drawdown_start,
            "回撤低点": max_drawdown_trough,
            "回撤恢复": recovery_date,
            "最长水下天数": longest_underwater_days,
            "收益/最大回撤": safe_div(net_profit, max_drawdown),
            "数据口径": "按平仓日累计已实现净收益；不含持仓期间浮盈亏",
        })

    return sorted(result, key=lambda row: row["最大回撤金额"], reverse=True)


def main():
    parser = argparse.ArgumentParser(description="分析期货交易风格并生成 CSV 统计表")
    parser.add_argument("--input-dir", type=Path, default=PROJECT_ROOT / "output")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "output")
    args = parser.parse_args()

    completed = read_csv(args.input_dir / "completed_trades.csv")
    transactions = read_csv(args.input_dir / "transactions.csv")
    daily = read_csv(args.input_dir / "daily_accounts.csv")
    if not completed or not transactions or not daily:
        raise SystemExit("第一阶段输出为空，无法分析。")

    # One account per working root. Restore the balance before the first day's
    # cashflows, realized PnL and fees instead of using its end-of-day balance.
    daily.sort(key=lambda row: row["date"])
    first_day = daily[0]
    beginning_equity = (
        number(first_day.get("estimated_equity"))
        - number(first_day.get("realized_pnl"))
        + number(first_day.get("total_fee"))
        - number(first_day.get("net_cash_flow"))
    )

    trades = aggregate_trade_groups(completed)
    overall = summarize_trades(trades)

    # 年度表现：以账户日报口径计算，手续费和年末浮盈亏均纳入。
    annual_groups = defaultdict(list)
    for row in daily:
        annual_groups[row["date"][:4]].append(row)
    yearly_rows = []
    prior_equity = beginning_equity
    for year in sorted(annual_groups):
        rows = sorted(annual_groups[year], key=lambda r: r["date"])
        gross = sum(number(r["realized_pnl"]) for r in rows)
        fees = sum(number(r["total_fee"]) for r in rows)
        deposits = sum(number(r["deposit"]) for r in rows)
        withdrawals = sum(number(r["withdrawal"]) for r in rows)
        reported = [r for r in rows if r.get("reported_month_end_equity") not in (None, "")]
        last = reported[-1]
        end_equity = number(last["reported_month_end_equity"])
        account_profit = end_equity - prior_equity - deposits + withdrawals
        yearly_rows.append({
            "年份": year, "数据起始": rows[0]["date"], "数据截止": rows[-1]["date"],
            "期初权益": prior_equity, "入金": deposits, "出金": withdrawals,
            "平仓盈亏": gross, "总费用": fees, "已实现净收益": gross - fees,
            "年末浮盈亏": number(last.get("month_end_floating_pnl")),
            "账户实际收益": account_profit, "期末权益": end_equity,
            "资金时间质量": "精确到日" if all(r.get("cash_flow_quality") == "REPORTED_DAILY" for r in rows) else "TXT月度汇总",
        })
        prior_equity = end_equity

    # 月度表现和账户活动强度。
    month_groups = defaultdict(list)
    for row in daily:
        month_groups[row["statement_month"]].append(row)
    tx_months = defaultdict(list)
    for row in transactions:
        tx_months[row["statement_month"]].append(row)
    monthly_rows, previous_equity = [], beginning_equity
    for month in sorted(month_groups):
        rows = sorted(month_groups[month], key=lambda r: r["date"])
        txs = tx_months.get(month, [])
        reported = [r for r in rows if r.get("reported_month_end_equity") not in (None, "")]
        last = reported[-1]
        end_equity = number(last["reported_month_end_equity"])
        deposits = sum(number(r["deposit"]) for r in rows)
        withdrawals = sum(number(r["withdrawal"]) for r in rows)
        gross = sum(number(r["realized_pnl"]) for r in rows)
        fees = sum(number(r["total_fee"]) for r in rows)
        turnover = sum(number(r["turnover"]) for r in txs)
        monthly_rows.append({
            "月份": month, "平仓盈亏": gross, "总费用": fees, "已实现净收益": gross - fees,
            "入金": deposits, "出金": withdrawals,
            "账户实际收益": end_equity - previous_equity - deposits + withdrawals,
            "期末权益": end_equity, "月末浮盈亏": number(last.get("month_end_floating_pnl")),
            "成交记录数": len(txs), "成交手数": sum(number(r["quantity"]) for r in txs),
            "成交额": turnover, "成交额/期末权益": safe_div(turnover, end_equity),
            "来源": "XLSX" if all(r.get("source_format") == "XLSX" for r in rows) else "TXT",
        })
        previous_equity = end_equity

    # 品种、方向、持仓周期、星期、精确成交时段。
    product_rows = []
    for product, rows in grouped_stats(trades, "product").items():
        item = stats_row(product, rows, "品种")
        item["多头净收益"] = sum(r["net_pnl"] for r in rows if r["direction"] == "LONG")
        item["空头净收益"] = sum(r["net_pnl"] for r in rows if r["direction"] == "SHORT")
        product_rows.append(item)
    product_rows.sort(key=lambda r: r["净收益"], reverse=True)
    product_risk_rows = build_product_risk_rows(trades)

    direction_rows = [stats_row(label, rows, "方向") for label, rows in grouped_stats(trades, "direction").items()]
    direction_rows.sort(key=lambda r: r["方向"])

    buckets = [
        ("日内", lambda d: d == 0), ("1–2天", lambda d: 0 < d <= 2),
        ("3–5天", lambda d: 2 < d <= 5), ("6–10天", lambda d: 5 < d <= 10),
        ("11–20天", lambda d: 10 < d <= 20), ("21天以上", lambda d: d > 20),
    ]
    holding_rows = [stats_row(label, [t for t in trades if test(t["holding_days"])], "持仓区间") for label, test in buckets]

    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekday_groups = defaultdict(list)
    for trade in trades:
        dt = trade["exit_dt"]
        if dt:
            weekday_groups[dt.weekday()].append(trade)
    weekday_rows = [stats_row(weekday_names[i], weekday_groups.get(i, []), "平仓星期") for i in range(5)]

    exact_trades = [t for t in trades if t["all_exact"] and t["exit_dt"]]
    time_groups = defaultdict(list)
    for trade in exact_trades:
        hour = trade["exit_dt"].hour
        time_groups[f"{hour:02d}:00–{hour:02d}:59"].append(trade)
    time_rows = [stats_row(label, rows, "平仓时段") for label, rows in sorted(time_groups.items())]

    # 估算权益曲线只反映已实现损益；月末点由券商实报权益替换。
    equity_rows, peak = [], None
    max_drawdown = 0.0
    for row in sorted(daily, key=lambda r: r["date"]):
        equity = number(row["estimated_equity"])
        peak = equity if peak is None else max(peak, equity)
        drawdown = equity - peak
        drawdown_pct = safe_div(drawdown, peak) if peak and peak > 0 else ""
        max_drawdown = min(max_drawdown, drawdown)
        equity_rows.append({
            "日期": row["date"], "月份": row["statement_month"], "估算权益": equity,
            "历史峰值": peak, "回撤金额": drawdown, "回撤比例": drawdown_pct,
            "权益质量": row["equity_quality"], "数据来源": row["source_format"],
        })

    # 行为特征：精确时间部分才判断亏损后放大仓位和快速反手。
    exact_sorted = sorted(exact_trades, key=lambda r: r["exit_dt"])
    after_loss = increased_after_loss = reversals = 0
    reversal_pnl = 0.0
    longest_loss_streak = current_streak = 0
    for trade in trades:
        if trade["net_pnl"] < 0:
            current_streak += 1
            longest_loss_streak = max(longest_loss_streak, current_streak)
        else:
            current_streak = 0
    for previous, current in zip(exact_sorted, exact_sorted[1:]):
        minutes = (current["exit_dt"] - previous["exit_dt"]).total_seconds() / 60
        same_day = current["exit_dt"].date() == previous["exit_dt"].date()
        if previous["net_pnl"] < 0 and same_day:
            after_loss += 1
            if current["quantity"] > previous["quantity"] * 1.5:
                increased_after_loss += 1
        if same_day and 0 <= minutes <= 30 and current["contract"] == previous["contract"] and current["direction"] != previous["direction"]:
            reversals += 1
            reversal_pnl += current["net_pnl"]

    positive = sorted((t for t in trades if t["net_pnl"] > 0), key=lambda t: t["net_pnl"], reverse=True)
    top10_profit = sum(t["net_pnl"] for t in positive[:10])
    gross_positive = sum(t["net_pnl"] for t in positive)
    top_product = product_rows[0] if product_rows else {}
    intraday = next((r for r in holding_rows if r["持仓区间"] == "日内"), {})
    behavior_rows = [
        {"指标": "最长连续亏损", "数值": longest_loss_streak, "单位": "笔", "质量": "按平仓顺序", "说明": "TXT同日内部顺序为推断"},
        {"指标": "前10笔盈利占全部毛盈利", "数值": safe_div(top10_profit, gross_positive), "单位": "%", "质量": "可靠", "说明": "衡量收益是否依赖少数大盈利"},
        {"指标": "扣除前10笔盈利后的净收益", "数值": overall["净收益"] - top10_profit, "单位": "元", "质量": "可靠", "说明": "仅用于集中度压力测试"},
        {"指标": "精确时间平仓次数", "数值": len(exact_trades), "单位": "笔", "质量": "XLSX", "说明": "仅这些交易用于盘中行为判断"},
        {"指标": "亏损后同日继续交易", "数值": after_loss, "单位": "次", "质量": "XLSX", "说明": "按下一笔精确时间交易判断"},
        {"指标": "亏损后仓位放大超过50%", "数值": increased_after_loss, "单位": "次", "质量": "XLSX", "说明": "不等同于主观报复性交易"},
        {"指标": "30分钟内同合约反手", "数值": reversals, "单位": "次", "质量": "XLSX", "说明": "仅识别客观反向交易"},
        {"指标": "快速反手后净收益", "数值": reversal_pnl, "单位": "元", "质量": "XLSX", "说明": "上述快速反手交易的合计净收益"},
    ]

    monthly_activity = [r for r in monthly_rows if r["期末权益"] > 0]
    activity_corr = corr([r["期末权益"] for r in monthly_activity], [r["成交额/期末权益"] for r in monthly_activity])
    cumulative_realized = sum(number(r["realized_pnl"]) - number(r["total_fee"]) for r in daily)
    total_flow = sum(number(r["net_cash_flow"]) for r in daily)
    last_reported = [r for r in daily if r.get("reported_month_end_equity") not in (None, "")][-1]
    ending_equity = number(last_reported["reported_month_end_equity"])
    economic_profit = ending_equity - beginning_equity - total_flow

    summary_rows = [
        {"指标": "累计已实现净收益", "数值": cumulative_realized, "单位": "元", "质量": "可靠", "说明": "平仓盈亏减全部费用"},
        {"指标": "累计账户实际收益", "数值": economic_profit, "单位": "元", "质量": "可靠", "说明": "期末权益减期初权益及净入金，包含当前浮盈亏"},
        {"指标": "累计总费用", "数值": sum(number(r["total_fee"]) for r in daily), "单位": "元", "质量": "可靠", "说明": "交易手续费及其他费用"},
        {"指标": "完整平仓次数", "数值": overall["平仓次数"], "单位": "笔", "质量": "可靠", "说明": "按券商平仓成交号聚合，避免FIFO拆分重复计数"},
        {"指标": "胜率", "数值": overall["胜率"], "单位": "%", "质量": "可靠", "说明": "按完整平仓事件计算"},
        {"指标": "盈亏比", "数值": overall["盈亏比"], "单位": "倍", "质量": "可靠", "说明": "平均盈利/平均亏损绝对值"},
        {"指标": "利润因子", "数值": overall["利润因子"], "单位": "倍", "质量": "可靠", "说明": "毛盈利/毛亏损绝对值"},
        {"指标": "日内交易占比", "数值": safe_div(intraday.get("平仓次数", 0), overall["平仓次数"]), "单位": "%", "质量": "按交易日可靠", "说明": "同一交易日开平"},
        {"指标": "平均持仓天数", "数值": overall["平均持仓天数"], "单位": "天", "质量": "TXT按日期近似", "说明": "按成交手数加权"},
        {"指标": "估算最大回撤", "数值": max_drawdown, "单位": "元", "质量": "非月末不含浮盈亏", "说明": "不能替代逐日盯市权益最大回撤"},
        {"指标": "最长连续亏损", "数值": longest_loss_streak, "单位": "笔", "质量": "同日TXT顺序推断", "说明": "用于行为风险提示"},
        {"指标": "前10笔盈利集中度", "数值": safe_div(top10_profit, gross_positive), "单位": "%", "质量": "可靠", "说明": "前10笔盈利占全部毛盈利"},
        {"指标": "期末权益", "数值": ending_equity, "单位": "元", "质量": "券商月末实报", "说明": last_reported["date"]},
        {"指标": "精确时间平仓次数", "数值": len(exact_trades), "单位": "笔", "质量": "XLSX", "说明": "盘中时段、快速反手分析的有效样本"},
        {"指标": "权益与交易活跃度相关性", "数值": activity_corr, "单位": "相关系数", "质量": "月度近似", "说明": "活跃度=成交额/期末权益，不代表保证金仓位"},
        {"指标": "最赚钱品种", "数值": top_product.get("品种", ""), "单位": "", "质量": "可靠", "说明": f"净收益 {top_product.get('净收益', 0):.2f} 元"},
        {"指标": "未归因费用及在途开仓费", "数值": cumulative_realized - overall["净收益"], "单位": "元", "质量": "可靠", "说明": "账户净收益与已完成交易归因之差，主要为未平仓开仓费及其他费用"},
    ]

    common_fields = ["平仓次数", "手数", "盈利次数", "亏损次数", "胜率", "毛盈利", "毛亏损", "净收益", "平均每笔", "平均盈利", "平均亏损", "盈亏比", "利润因子", "平均持仓天数"]
    write_csv(args.output_dir / "trading_style_summary.csv", ["指标", "数值", "单位", "质量", "说明"], summary_rows)
    write_csv(args.output_dir / "yearly_performance.csv", list(yearly_rows[0]), yearly_rows)
    write_csv(args.output_dir / "monthly_performance.csv", list(monthly_rows[0]), monthly_rows)
    write_csv(args.output_dir / "product_performance.csv", ["品种", *common_fields, "多头净收益", "空头净收益"], product_rows)
    write_csv(
        args.output_dir / "product_risk_analysis.csv",
        ["品种", "平仓次数", "活跃平仓日", "净收益", "平仓日收益波动", "单笔收益波动", "最大回撤金额",
         "回撤开始", "回撤低点", "回撤恢复", "最长水下天数", "收益/最大回撤", "数据口径"],
        product_risk_rows,
    )
    write_csv(args.output_dir / "direction_performance.csv", ["方向", *common_fields], direction_rows)
    write_csv(args.output_dir / "holding_period_analysis.csv", ["持仓区间", *common_fields], holding_rows)
    write_csv(args.output_dir / "weekday_performance.csv", ["平仓星期", *common_fields], weekday_rows)
    write_csv(args.output_dir / "time_performance.csv", ["平仓时段", *common_fields], time_rows)
    write_csv(args.output_dir / "equity_drawdown.csv", list(equity_rows[0]), equity_rows)
    write_csv(args.output_dir / "behavior_analysis.csv", ["指标", "数值", "单位", "质量", "说明"], behavior_rows)

    print(json.dumps({
        "completed_trade_groups": len(trades), "exact_time_trade_groups": len(exact_trades),
        "products": len(product_rows), "product_risk_rows": len(product_risk_rows),
        "months": len(monthly_rows), "years": len(yearly_rows),
        "realized_net_profit": round(cumulative_realized, 2), "economic_profit": round(economic_profit, 2),
        "output_dir": str(args.output_dir.resolve()),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
