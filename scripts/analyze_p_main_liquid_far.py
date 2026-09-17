from __future__ import annotations

from project_config import PROJECT_ROOT, RAW_STATEMENTS_DIR, MARKET_DATA_DIR

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from backtest_p_calendar_v1 import (
    MIN_OPEN_INTEREST_EACH_LEG,
    MIN_VOLUME_EACH_LEG,
    MULTIPLIER,
    PAIR_ROUND_TRIP_FEE,
    ROOT,
    SLIPPAGE_POINTS,
    build_pair_series,
    contract_expiry_ordinal,
    contract_year_month,
    load_history,
)
from backtest_p_calendar_v2 import enhance_pair_series


OUTPUT_DIR = ROOT / "output" / "P跨期套利" / "主力远月筛选"
V1_BASELINE_TRADES = (
    ROOT / "output" / "P跨期套利" / "回测_v1" / "P跨期基准规则逐笔.csv"
)

LOOKBACK_TRADING_DAYS = 5
MAIN_MAX_MONTHS_AHEAD = 8
FAR_GAPS = (3, 4, 5)
MIN_OBSERVATIONS = 3

# 重新回测时沿用第二版的稳健候选入场规则，只改变“允许交易的合约集合”。
ARM_Z = 2.0
PULLBACK_Z = 0.5
MIN_EDGE_POINTS = 15.0
ARM_EXPIRY_TRADING_DAYS = 5
COOLDOWN_TRADING_DAYS = 5
NO_ENTRY_DAYS_BEFORE_DELIVERY_MONTH = 30
TAKE_PROFIT_POINTS = 20.0
STOP_LOSS_POINTS = 20.0
MAX_HOLDING_DAYS = 10
EXIT_ABS_Z = 0.5


def month_ordinal(value: pd.Timestamp | pd.Period) -> int:
    return int(value.year) * 12 + int(value.month)


def valid_liquidity(row: pd.Series) -> bool:
    return bool(
        row["observations"] >= MIN_OBSERVATIONS
        and row["avg_volume_5d"] >= MIN_VOLUME_EACH_LEG
        and row["avg_oi_5d"] >= MIN_OPEN_INTEREST_EACH_LEG
    )


def contract_stats(history: pd.DataFrame, reference_dates: list[pd.Timestamp]) -> pd.DataFrame:
    reference = history.loc[history["date"].isin(reference_dates)].copy()
    if reference.empty:
        return pd.DataFrame()
    result = (
        reference.groupby("contract", as_index=False)
        .agg(
            observations=("date", "nunique"),
            avg_volume_5d=("volume", "mean"),
            avg_oi_5d=("open_interest", "mean"),
            last_reference_date=("date", "max"),
            reference_price=("price", "last"),
        )
    )
    result["expiry_ordinal"] = result["contract"].map(contract_expiry_ordinal)
    return result.dropna(subset=["expiry_ordinal"]).copy()


def choose_monthly_contracts(history: pd.DataFrame) -> pd.DataFrame:
    all_dates = sorted(pd.Timestamp(x) for x in history["date"].dropna().unique())
    first_month = history["date"].min().to_period("M") + 1
    last_month = history["date"].max().to_period("M")
    rows: list[dict[str, object]] = []

    for period in pd.period_range(first_month, last_month, freq="M"):
        month_start = period.to_timestamp()
        prior_dates = [d for d in all_dates if d < month_start][-LOOKBACK_TRADING_DAYS:]
        base: dict[str, object] = {
            "月份": month_start,
            "选择参考截止日": max(prior_dates) if prior_dates else pd.NaT,
            "参考交易日数": len(prior_dates),
            "主力合约": None,
            "远月合约": None,
            "期限差月": np.nan,
            "选择状态": "",
        }
        if len(prior_dates) < MIN_OBSERVATIONS:
            base["选择状态"] = "参考数据不足"
            rows.append(base)
            continue

        stats = contract_stats(history, prior_dates)
        current_ordinal = month_ordinal(period)
        stats["距当前月份"] = stats["expiry_ordinal"] - current_ordinal
        main_pool = stats.loc[
            stats["距当前月份"].between(1, MAIN_MAX_MONTHS_AHEAD)
            & stats.apply(valid_liquidity, axis=1)
        ].sort_values(["avg_volume_5d", "avg_oi_5d"], ascending=[False, False])
        if main_pool.empty:
            base["选择状态"] = "没有合格主力合约"
            rows.append(base)
            continue

        main = main_pool.iloc[0]
        main_contract = str(main["contract"])
        main_ordinal = int(main["expiry_ordinal"])
        base.update(
            {
                "主力合约": main_contract,
                "主力距当前月": int(main["距当前月份"]),
                "主力5日均成交量": float(main["avg_volume_5d"]),
                "主力5日均持仓量": float(main["avg_oi_5d"]),
                "主力参考价": float(main["reference_price"]),
            }
        )

        far_candidates: list[pd.Series] = []
        for gap in FAR_GAPS:
            candidate = stats.loc[stats["expiry_ordinal"].eq(main_ordinal + gap)]
            prefix = f"加{gap}月"
            if candidate.empty:
                base[f"{prefix}合约"] = None
                base[f"{prefix}5日均成交量"] = np.nan
                base[f"{prefix}5日均持仓量"] = np.nan
                base[f"{prefix}是否合格"] = False
                continue
            row = candidate.sort_values(
                ["avg_volume_5d", "avg_oi_5d"], ascending=[False, False]
            ).iloc[0]
            base[f"{prefix}合约"] = str(row["contract"])
            base[f"{prefix}5日均成交量"] = float(row["avg_volume_5d"])
            base[f"{prefix}5日均持仓量"] = float(row["avg_oi_5d"])
            base[f"{prefix}是否合格"] = valid_liquidity(row)
            if valid_liquidity(row):
                row = row.copy()
                row["gap"] = gap
                far_candidates.append(row)

        if not far_candidates:
            base["选择状态"] = "加3至5月没有合格流动性合约"
            rows.append(base)
            continue
        far = sorted(
            far_candidates,
            key=lambda r: (float(r["avg_volume_5d"]), float(r["avg_oi_5d"])),
            reverse=True,
        )[0]
        gap = int(far["gap"])
        base.update(
            {
                "远月合约": str(far["contract"]),
                "期限差月": gap,
                "远月5日均成交量": float(far["avg_volume_5d"]),
                "远月5日均持仓量": float(far["avg_oi_5d"]),
                "远月参考价": float(far["reference_price"]),
                "远月成交量占主力": float(far["avg_volume_5d"] / main["avg_volume_5d"]),
                "选择状态": f"选择加{gap}月中流动性最高合约",
            }
        )
        rows.append(base)
    return pd.DataFrame(rows)


def build_selected_daily(
    selections: pd.DataFrame, history: pd.DataFrame
) -> tuple[pd.DataFrame, dict[tuple[str, str], pd.DataFrame]]:
    by_contract = {
        contract: frame.copy() for contract, frame in history.groupby("contract", sort=False)
    }
    pair_cache: dict[tuple[str, str], pd.DataFrame] = {}
    parts: list[pd.DataFrame] = []
    for _, selection in selections.dropna(subset=["主力合约", "远月合约"]).iterrows():
        main_contract = str(selection["主力合约"])
        far_contract = str(selection["远月合约"])
        key = (main_contract, far_contract)
        if key not in pair_cache:
            pair_cache[key] = enhance_pair_series(
                build_pair_series(by_contract, main_contract, far_contract)
            )
        pair = pair_cache[key]
        period = pd.Timestamp(selection["月份"]).to_period("M")
        part = pair.loc[pair["date"].dt.to_period("M").eq(period)].copy()
        if part.empty:
            continue
        part.insert(0, "选择月份", period.to_timestamp())
        part.insert(1, "主力合约", main_contract)
        part.insert(2, "远月合约", far_contract)
        part.insert(3, "期限差月", int(selection["期限差月"]))
        part["价差百分比"] = part["spread_ratio"]
        parts.append(
            part[
                [
                    "选择月份",
                    "date",
                    "主力合约",
                    "远月合约",
                    "期限差月",
                    "near_price",
                    "far_price",
                    "spread",
                    "价差百分比",
                    "z60",
                    "potential_reversion_points",
                    "near_volume",
                    "far_volume",
                    "near_oi",
                    "far_oi",
                    "liquid",
                    "entry_cutoff",
                ]
            ]
        )
    if not parts:
        return pd.DataFrame(), pair_cache
    daily = pd.concat(parts, ignore_index=True).sort_values(["date", "主力合约"])
    daily = daily.drop_duplicates(["date"], keep="first").reset_index(drop=True)
    return daily, pair_cache


def add_month_spread_summary(selections: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    result = selections.copy()
    if daily.empty:
        return result
    summary = (
        daily.groupby("选择月份", as_index=False)
        .agg(
            月内共同交易日=("date", "nunique"),
            月初价差百分比=("价差百分比", "first"),
            月末价差百分比=("价差百分比", "last"),
            月均价差百分比=("价差百分比", "mean"),
            月内最低价差百分比=("价差百分比", "min"),
            月内最高价差百分比=("价差百分比", "max"),
            月均绝对价差百分比=("价差百分比", lambda x: x.abs().mean()),
        )
        .rename(columns={"选择月份": "月份"})
    )
    return result.merge(summary, on="月份", how="left")


def close_trade(
    position: dict[str, object], row: pd.Series, reason: str, phase: str
) -> dict[str, object]:
    gross_points = float(position["entry_spread"]) - float(row["spread"])
    gross_pnl = gross_points * MULTIPLIER
    return {
        "阶段": phase,
        "选择月份": position["selection_month"],
        "主力合约": position["main_contract"],
        "远月合约": position["far_contract"],
        "期限差月": position["gap_months"],
        "进入观察日期": position["armed_date"],
        "确认日期": position["signal_date"],
        "开仓日期": position["entry_date"],
        "平仓日期": pd.Timestamp(row["date"]),
        "观察时Z60": position["armed_z60"],
        "峰值Z60": position["peak_z60"],
        "确认时Z60": position["signal_z60"],
        "确认回落Z值": position["z_pullback"],
        "确认时潜在回归点": position["edge_points"],
        "开仓主力价": position["entry_main_price"],
        "开仓远月价": position["entry_far_price"],
        "开仓价差": position["entry_spread"],
        "开仓价差百分比": position["entry_spread_pct"],
        "平仓主力价": float(row["near_price"]),
        "平仓远月价": float(row["far_price"]),
        "平仓价差": float(row["spread"]),
        "平仓价差百分比": float(row["价差百分比"]),
        "毛收益点数": gross_points,
        "毛收益(元)": gross_pnl,
        "手续费(元)": PAIR_ROUND_TRIP_FEE,
        "滑点成本(元)": SLIPPAGE_POINTS * MULTIPLIER,
        "净收益(元)": gross_pnl - PAIR_ROUND_TRIP_FEE - SLIPPAGE_POINTS * MULTIPLIER,
        "持仓交易日": int(position["held_days"]),
        "平仓原因": reason,
    }


def phase_label(date: pd.Timestamp) -> str:
    if pd.Timestamp("2020-01-01") <= date <= pd.Timestamp("2024-12-31"):
        return "训练(2020-2024)"
    if pd.Timestamp("2025-01-01") <= date <= pd.Timestamp("2025-12-31"):
        return "测试(2025)"
    if date.year == 2026:
        return "观察(2026截至数据末日)"
    return "历史观察"


def backtest_selected_pairs(daily: pd.DataFrame) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame()
    records: list[dict[str, object]] = []
    all_dates = sorted(pd.Timestamp(x) for x in daily["date"].unique())
    global_step = {date: index for index, date in enumerate(all_dates)}
    cooldown_until_step = -1

    for selection_month, segment in daily.groupby("选择月份", sort=True):
        segment = segment.sort_values("date").reset_index(drop=True)
        if len(segment) < 2:
            continue
        armed = False
        armed_index = -1
        armed_date = None
        armed_z60 = np.nan
        peak_z60 = np.nan
        position: dict[str, object] | None = None

        for index in range(1, len(segment)):
            signal = segment.iloc[index - 1]
            execution = segment.iloc[index]
            execution_date = pd.Timestamp(execution["date"])
            step = global_step[execution_date]

            if position is not None:
                position["held_days"] = int(position["held_days"]) + 1
                signal_pnl_points = float(position["entry_spread"]) - float(signal["spread"])
                reason = None
                if signal_pnl_points >= TAKE_PROFIT_POINTS:
                    reason = "止盈"
                elif signal_pnl_points <= -STOP_LOSS_POINTS:
                    reason = "止损"
                elif np.isfinite(signal["z60"]) and abs(float(signal["z60"])) <= EXIT_ABS_Z:
                    reason = "Z60回归"
                elif int(position["held_days"]) >= MAX_HOLDING_DAYS:
                    reason = "最长持有"
                if reason is not None:
                    records.append(close_trade(position, execution, reason, phase_label(pd.Timestamp(position["entry_date"]))))
                    position = None
                    cooldown_until_step = step + COOLDOWN_TRADING_DAYS
                    armed = False
                    continue

            if position is not None:
                continue
            z = float(signal["z60"]) if pd.notna(signal["z60"]) else np.nan
            if not np.isfinite(z) or not bool(signal["liquid"]) or not bool(execution["liquid"]):
                continue
            if pd.Timestamp(signal["date"]) >= pd.Timestamp(signal["entry_cutoff"]):
                armed = False
                continue
            if not armed:
                if z >= ARM_Z:
                    armed = True
                    armed_index = index - 1
                    armed_date = pd.Timestamp(signal["date"])
                    armed_z60 = z
                    peak_z60 = z
                continue
            if (index - 1) - armed_index > ARM_EXPIRY_TRADING_DAYS or z <= 0:
                armed = False
                continue
            peak_z60 = max(float(peak_z60), z)
            previous = segment.iloc[index - 2] if index >= 2 else None
            confirmed = (
                previous is not None
                and pd.notna(previous["z60"])
                and z < float(previous["z60"])
                and float(signal["spread"]) < float(previous["spread"])
                and float(peak_z60) - z >= PULLBACK_Z
                and pd.notna(signal["potential_reversion_points"])
                and float(signal["potential_reversion_points"]) >= MIN_EDGE_POINTS
            )
            if not confirmed or step <= cooldown_until_step:
                continue
            position = {
                "selection_month": pd.Timestamp(selection_month),
                "main_contract": str(signal["主力合约"]),
                "far_contract": str(signal["远月合约"]),
                "gap_months": int(signal["期限差月"]),
                "armed_date": armed_date,
                "armed_z60": armed_z60,
                "peak_z60": peak_z60,
                "signal_date": pd.Timestamp(signal["date"]),
                "entry_date": execution_date,
                "signal_z60": z,
                "z_pullback": float(peak_z60) - z,
                "edge_points": float(signal["potential_reversion_points"]),
                "entry_main_price": float(execution["near_price"]),
                "entry_far_price": float(execution["far_price"]),
                "entry_spread": float(execution["spread"]),
                "entry_spread_pct": float(execution["价差百分比"]),
                "held_days": 0,
            }
            armed = False

        if position is not None:
            last = segment.iloc[-1]
            records.append(
                close_trade(position, last, "月末换组", phase_label(pd.Timestamp(position["entry_date"])))
            )
            cooldown_until_step = global_step[pd.Timestamp(last["date"])] + COOLDOWN_TRADING_DAYS
    return pd.DataFrame(records)


def metrics(trades: pd.DataFrame) -> dict[str, object]:
    if trades.empty:
        return {
            "交易数": 0,
            "胜率": np.nan,
            "净收益": 0.0,
            "平均每笔": np.nan,
            "利润因子": np.nan,
            "最大回撤": 0.0,
            "平均持仓交易日": np.nan,
        }
    ordered = trades.sort_values(["平仓日期", "主力合约", "远月合约"])
    pnl = ordered["净收益(元)"].astype(float)
    equity = pnl.cumsum()
    drawdown = equity - equity.cummax().clip(lower=0)
    gains = pnl.loc[pnl > 0].sum()
    losses = -pnl.loc[pnl < 0].sum()
    return {
        "交易数": len(pnl),
        "胜率": (pnl > 0).mean(),
        "净收益": pnl.sum(),
        "平均每笔": pnl.mean(),
        "利润因子": gains / losses if losses > 0 else np.nan,
        "最大回撤": drawdown.min(),
        "平均持仓交易日": ordered["持仓交易日"].mean(),
    }


def grouped_metrics(trades: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    rows = []
    grouper = columns[0] if len(columns) == 1 else columns
    for keys, group in trades.groupby(grouper, dropna=False):
        keys = keys if isinstance(keys, tuple) else (keys,)
        row = dict(zip(columns, keys))
        row.update(metrics(group))
        rows.append(row)
    return pd.DataFrame(rows).sort_values(columns)


def main() -> None:
    history = load_history()
    history = history.loc[history["contract"].map(contract_year_month).notna()].copy()
    selections = choose_monthly_contracts(history)
    daily, _ = build_selected_daily(selections, history)
    selections = add_month_spread_summary(selections, daily)
    trades = backtest_selected_pairs(daily)
    if len(trades):
        trades["年份"] = pd.to_datetime(trades["开仓日期"]).dt.year

    selected = selections.dropna(subset=["远月合约"]).copy()
    gap_summary = (
        selected.groupby("期限差月", as_index=False)
        .agg(
            选择月份数=("月份", "count"),
            平均远月成交量占主力=("远月成交量占主力", "mean"),
            平均月初价差百分比=("月初价差百分比", "mean"),
            平均月末价差百分比=("月末价差百分比", "mean"),
            平均绝对价差百分比=("月均绝对价差百分比", "mean"),
            价差百分比标准差=("月均价差百分比", "std"),
        )
        .sort_values("期限差月")
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    selections.to_csv(
        OUTPUT_DIR / "P主力与3至5月远月_月度选择.csv",
        index=False,
        encoding="utf-8-sig",
        date_format="%Y-%m-%d",
    )
    daily.to_csv(
        OUTPUT_DIR / "P主力与远月_每日百分比价差.csv",
        index=False,
        encoding="utf-8-sig",
        date_format="%Y-%m-%d",
    )
    gap_summary.to_csv(
        OUTPUT_DIR / "P主力远月_期限差统计.csv", index=False, encoding="utf-8-sig"
    )
    trades.to_csv(
        OUTPUT_DIR / "P主力远月_回测逐笔.csv",
        index=False,
        encoding="utf-8-sig",
        date_format="%Y-%m-%d",
    )
    grouped_metrics(trades, ["阶段", "年份"]).to_csv(
        OUTPUT_DIR / "P主力远月_回测年度汇总.csv", index=False, encoding="utf-8-sig"
    )
    grouped_metrics(trades, ["阶段", "期限差月"]).to_csv(
        OUTPUT_DIR / "P主力远月_回测期限差汇总.csv", index=False, encoding="utf-8-sig"
    )
    v1 = pd.read_csv(V1_BASELINE_TRADES, encoding="utf-8-sig", low_memory=False)
    v1 = v1.rename(columns={"近月合约": "主力合约"})
    v1["期限差月"] = [
        contract_expiry_ordinal(far) - contract_expiry_ordinal(near)
        for near, far in zip(v1["主力合约"], v1["远月合约"])
    ]
    v1_gap_summary = grouped_metrics(v1, ["阶段", "期限差月"])
    v1_gap_summary.to_csv(
        OUTPUT_DIR / "P第一版回测_期限差汇总.csv", index=False, encoding="utf-8-sig"
    )

    current = selected.sort_values("月份").iloc[-1] if len(selected) else None
    phase_results = {
        phase: metrics(group) for phase, group in trades.groupby("阶段")
    } if len(trades) else {}
    summary = {
        "selection_method": {
            "frequency": "每月一次并在当月固定",
            "lookback": f"月初之前{LOOKBACK_TRADING_DAYS}个交易日",
            "main_contract": "未来1至8个月合约中5日平均成交量最高，持仓量作为次排序",
            "far_contract": "主力到期月之后3、4、5个月合约中5日平均成交量最高者",
            "liquidity_minimum_each_leg": {
                "volume": MIN_VOLUME_EACH_LEG,
                "open_interest": MIN_OPEN_INTEREST_EACH_LEG,
                "minimum_observations": MIN_OBSERVATIONS,
            },
            "no_month_lookahead": True,
        },
        "spread_definition": "(主力价格-远月价格)/远月价格",
        "data_range": {
            "start": str(history["date"].min().date()),
            "end": str(history["date"].max().date()),
        },
        "months_total": int(len(selections)),
        "months_selected": int(len(selected)),
        "gap_counts": {
            str(int(k)): int(v)
            for k, v in selected["期限差月"].value_counts().sort_index().items()
        },
        "current_selection": None if current is None else {
            "month": str(pd.Timestamp(current["月份"]).date()),
            "main_contract": str(current["主力合约"]),
            "far_contract": str(current["远月合约"]),
            "gap_months": int(current["期限差月"]),
            "main_avg_volume_5d": float(current["主力5日均成交量"]),
            "far_avg_volume_5d": float(current["远月5日均成交量"]),
            "far_volume_share": float(current["远月成交量占主力"]),
            "month_start_spread_pct": float(current["月初价差百分比"]),
            "latest_spread_pct": float(current["月末价差百分比"]),
        },
        "backtest_rule": {
            "direction": "空主力/多远月",
            "arm_z": ARM_Z,
            "pullback_z": PULLBACK_Z,
            "minimum_reversion_space_points": MIN_EDGE_POINTS,
            "cooldown_trading_days": COOLDOWN_TRADING_DAYS,
            "take_profit_points": TAKE_PROFIT_POINTS,
            "stop_loss_points": STOP_LOSS_POINTS,
            "max_holding_days": MAX_HOLDING_DAYS,
            "exit_abs_z": EXIT_ABS_Z,
            "month_end_rule": "当月最后一个共同交易日退出，下一月重新选合约",
        },
        "backtest_results": phase_results,
        "v1_baseline_gap_results": v1_gap_summary.to_dict(orient="records"),
        "warning": "动态选择解决合约范围与期限混杂问题，但仍须以样本外交易数和年份稳定性判断，不能只看总收益。",
    }
    with (OUTPUT_DIR / "P主力远月_研究摘要.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, default=str)

    print("月度范围：", selections["月份"].min(), "至", selections["月份"].max())
    print("成功选择月份：", len(selected), "/", len(selections))
    print("期限差分布：", summary["gap_counts"])
    print("当前选择：", summary["current_selection"])
    print("回测结果：", phase_results)
    print("输出目录：", OUTPUT_DIR)


if __name__ == "__main__":
    main()
