from __future__ import annotations

from project_config import PROJECT_ROOT, RAW_STATEMENTS_DIR, MARKET_DATA_DIR

import itertools
import json
import math
from dataclasses import asdict, dataclass
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
    contract_year_month,
    generate_contract_pairs,
    load_history,
    training_month_pairs,
)


OUTPUT_DIR = ROOT / "output" / "P跨期套利" / "回测_v2"
V1_BASELINE_TRADES = ROOT / "output" / "P跨期套利" / "回测_v1" / "P跨期基准规则逐笔.csv"

# 第二版只优化入场，平仓参数保持第一版基准值，避免同时调太多参数。
TAKE_PROFIT_POINTS = 20.0
STOP_LOSS_POINTS = 20.0
MAX_HOLDING_DAYS = 10
EXIT_ABS_Z = 0.5

COOLDOWN_TRADING_DAYS = 5
ARM_EXPIRY_TRADING_DAYS = 5
NO_ENTRY_DAYS_BEFORE_DELIVERY_MONTH = 30


@dataclass(frozen=True)
class EntryParameters:
    arm_z: float
    confirmation: str
    min_edge_points: float

    @property
    def config_id(self) -> str:
        z = str(self.arm_z).replace(".", "p")
        edge = str(self.min_edge_points).replace(".", "p")
        return f"arm{z}_{self.confirmation}_edge{edge}"


def confirmation_label(value: str) -> str:
    return {
        "decline_1d": "首次回落",
        "pullback_0p3": "从峰值回落0.3Z",
        "pullback_0p5": "从峰值回落0.5Z",
    }[value]


def parameter_grid() -> list[EntryParameters]:
    return [
        EntryParameters(*values)
        for values in itertools.product(
            [1.5, 2.0],
            ["decline_1d", "pullback_0p3", "pullback_0p5"],
            [15.0, 20.0],
        )
    ]


def enhance_pair_series(pair: pd.DataFrame) -> pd.DataFrame:
    pair = pair.copy()
    ratio_mean = pair["spread_ratio"].rolling(60, min_periods=40).mean()
    pair["potential_reversion_points"] = (
        (pair["spread_ratio"] - ratio_mean) * pair["far_price"]
    )
    pair["near_ma20"] = pair["near_price"].rolling(20, min_periods=15).mean()
    pair["near_ma120"] = pair["near_price"].rolling(120, min_periods=80).mean()
    pair["near_ma20_change_5d"] = pair["near_ma20"].pct_change(5)
    bull = (
        pair["near_price"].gt(pair["near_ma120"])
        & pair["near_ma20_change_5d"].gt(0)
    )
    bear = (
        pair["near_price"].lt(pair["near_ma120"])
        & pair["near_ma20_change_5d"].lt(0)
    )
    pair["outright_trend"] = np.select(
        [bull, bear], ["近月牛市状态", "近月熊市状态"], default="趋势混合/不足"
    )
    pair["entry_cutoff"] = pair["near_delivery_start"] - pd.Timedelta(
        days=NO_ENTRY_DAYS_BEFORE_DELIVERY_MONTH
    )
    return pair


def arrays_for_pair(pair: pd.DataFrame) -> dict[str, np.ndarray]:
    return {
        "date": pair["date"].to_numpy(dtype="datetime64[ns]"),
        "near_price": pair["near_price"].to_numpy(dtype=float),
        "far_price": pair["far_price"].to_numpy(dtype=float),
        "spread": pair["spread"].to_numpy(dtype=float),
        "z60": pair["z60"].to_numpy(dtype=float),
        "edge": pair["potential_reversion_points"].to_numpy(dtype=float),
        "liquid": pair["liquid"].to_numpy(dtype=bool),
        "delivery_start": pair["near_delivery_start"].to_numpy(dtype="datetime64[ns]"),
        "entry_cutoff": pair["entry_cutoff"].to_numpy(dtype="datetime64[ns]"),
        "near_ma20": pair["near_ma20"].to_numpy(dtype=float),
        "near_ma120": pair["near_ma120"].to_numpy(dtype=float),
        "trend": pair["outright_trend"].astype(str).to_numpy(),
    }


def metric_values(trades: pd.DataFrame, prefix: str = "") -> dict[str, object]:
    if trades.empty:
        return {
            f"{prefix}交易数": 0,
            f"{prefix}胜率": np.nan,
            f"{prefix}净收益": 0.0,
            f"{prefix}平均每笔": np.nan,
            f"{prefix}利润因子": np.nan,
            f"{prefix}最大回撤": 0.0,
            f"{prefix}平均持仓交易日": np.nan,
            f"{prefix}收益标准差": np.nan,
            f"{prefix}统计分数": np.nan,
        }
    ordered = trades.sort_values(["平仓日期", "近月合约", "远月合约"])
    pnl = ordered["净收益(元)"].astype(float)
    equity = pnl.cumsum()
    drawdown = equity - equity.cummax().clip(lower=0)
    gains = pnl.loc[pnl > 0].sum()
    losses = -pnl.loc[pnl < 0].sum()
    std = pnl.std(ddof=1)
    score = pnl.mean() / std * math.sqrt(len(pnl)) if len(pnl) >= 2 and std > 0 else np.nan
    return {
        f"{prefix}交易数": len(pnl),
        f"{prefix}胜率": (pnl > 0).mean(),
        f"{prefix}净收益": pnl.sum(),
        f"{prefix}平均每笔": pnl.mean(),
        f"{prefix}利润因子": gains / losses if losses > 0 else np.nan,
        f"{prefix}最大回撤": drawdown.min(),
        f"{prefix}平均持仓交易日": ordered["持仓交易日"].mean(),
        f"{prefix}收益标准差": std,
        f"{prefix}统计分数": score,
    }


def make_trade_record(
    phase: str,
    params: EntryParameters,
    near: str,
    far: str,
    month_pair: str,
    position: dict[str, object],
    a: dict[str, np.ndarray],
    exit_index: int,
    reason: str,
) -> dict[str, object]:
    entry_spread = float(position["entry_spread"])
    exit_spread = float(a["spread"][exit_index])
    gross_points = entry_spread - exit_spread
    gross_pnl = gross_points * MULTIPLIER
    return {
        "阶段": phase,
        "参数编号": params.config_id,
        "观察阈值": params.arm_z,
        "确认方法": confirmation_label(params.confirmation),
        "最低回归空间": params.min_edge_points,
        "近月合约": near,
        "远月合约": far,
        "月份组合": month_pair,
        "进入观察日期": position["armed_date"],
        "确认日期": position["signal_date"],
        "开仓日期": position["entry_date"],
        "平仓日期": pd.Timestamp(a["date"][exit_index]),
        "观察时Z60": position["armed_z60"],
        "峰值Z60": position["peak_z60"],
        "确认时Z60": position["signal_z60"],
        "确认回落Z值": position["z_pullback"],
        "确认时潜在回归点": position["edge_points"],
        "近月MA20": position["near_ma20"],
        "近月MA120": position["near_ma120"],
        "单边趋势状态": position["trend"],
        "开仓近月价": position["entry_near_price"],
        "开仓远月价": position["entry_far_price"],
        "开仓价差": entry_spread,
        "平仓近月价": float(a["near_price"][exit_index]),
        "平仓远月价": float(a["far_price"][exit_index]),
        "平仓价差": exit_spread,
        "毛收益点数": gross_points,
        "毛收益(元)": gross_pnl,
        "手续费(元)": PAIR_ROUND_TRIP_FEE,
        "滑点成本(元)": SLIPPAGE_POINTS * MULTIPLIER,
        "净收益(元)": gross_pnl - PAIR_ROUND_TRIP_FEE - SLIPPAGE_POINTS * MULTIPLIER,
        "持仓交易日": max(0, exit_index - int(position["entry_index"])),
        "平仓原因": reason,
    }


def simulate_portfolio(
    pair_series: dict[tuple[str, str], pd.DataFrame],
    params: EntryParameters,
    phase: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, int]]:
    phase_start = np.datetime64(start.to_datetime64())
    phase_end = np.datetime64(end.to_datetime64())
    pair_arrays = {key: arrays_for_pair(frame) for key, frame in pair_series.items()}
    events: dict[pd.Timestamp, list[tuple[tuple[str, str], int]]] = {}
    states: dict[tuple[str, str], dict[str, object]] = {}
    for key, a in pair_arrays.items():
        states[key] = {
            "position": None,
            "armed": False,
            "armed_index": -1,
            "armed_date": None,
            "armed_z60": np.nan,
            "peak_z60": np.nan,
        }
        for index in range(1, len(a["date"])):
            if phase_start <= a["date"][index] <= phase_end:
                events.setdefault(pd.Timestamp(a["date"][index]), []).append((key, index))

    records: list[dict[str, object]] = []
    occupied_contracts: set[str] = set()
    occupied_month_pairs: set[str] = set()
    month_pair_cooldown_until: dict[str, int] = {}
    diagnostics = {
        "进入观察次数": 0,
        "确认信号次数": 0,
        "实际开仓次数": 0,
        "合约重叠拒绝次数": 0,
        "月份组合占用拒绝次数": 0,
        "冷静期拒绝次数": 0,
        "回归空间不足次数": 0,
        "交割过滤次数": 0,
    }

    sorted_dates = sorted(events)
    for global_step, execution_ts in enumerate(sorted_dates):
        day_events = events[execution_ts]

        # 先处理当日平仓，释放合约与月份组合，再处理新开仓。
        for key, index in day_events:
            state = states[key]
            position = state["position"]
            if position is None:
                continue
            a = pair_arrays[key]
            signal_index = index - 1
            entry_spread = float(position["entry_spread"])
            signal_pnl_points = entry_spread - float(a["spread"][signal_index])
            held = signal_index - int(position["entry_index"])
            reason = None
            if signal_pnl_points >= TAKE_PROFIT_POINTS:
                reason = "止盈"
            elif signal_pnl_points <= -STOP_LOSS_POINTS:
                reason = "止损"
            elif np.isfinite(a["z60"][signal_index]) and abs(a["z60"][signal_index]) <= EXIT_ABS_Z:
                reason = "Z60回归"
            elif held >= MAX_HOLDING_DAYS:
                reason = "最长持有"
            elif a["date"][index] >= a["delivery_start"][index]:
                reason = "交割月前退出"
            if reason is None:
                continue
            near, far = key
            near_month = contract_year_month(near)[1]
            far_month = contract_year_month(far)[1]
            month_pair = f"{near_month:02d}-{far_month:02d}"
            records.append(
                make_trade_record(phase, params, near, far, month_pair, position, a, index, reason)
            )
            state["position"] = None
            state["armed"] = False
            occupied_contracts.discard(near)
            occupied_contracts.discard(far)
            occupied_month_pairs.discard(month_pair)
            month_pair_cooldown_until[month_pair] = global_step + COOLDOWN_TRADING_DAYS

        candidates: list[dict[str, object]] = []
        for key, index in day_events:
            state = states[key]
            if state["position"] is not None:
                continue
            a = pair_arrays[key]
            signal_index = index - 1
            signal_date = a["date"][signal_index]
            execution_date = a["date"][index]
            z = float(a["z60"][signal_index])
            if not np.isfinite(z) or not (phase_start <= signal_date <= phase_end):
                continue
            if execution_date >= a["entry_cutoff"][index] or signal_date >= a["entry_cutoff"][signal_index]:
                diagnostics["交割过滤次数"] += 1
                state["armed"] = False
                continue
            if not (a["liquid"][signal_index] and a["liquid"][index]):
                continue

            if not state["armed"]:
                if z >= params.arm_z:
                    state.update(
                        {
                            "armed": True,
                            "armed_index": signal_index,
                            "armed_date": pd.Timestamp(signal_date),
                            "armed_z60": z,
                            "peak_z60": z,
                        }
                    )
                    diagnostics["进入观察次数"] += 1
                continue

            armed_age = signal_index - int(state["armed_index"])
            if armed_age > ARM_EXPIRY_TRADING_DAYS or z <= 0:
                state["armed"] = False
                if z >= params.arm_z:
                    state.update(
                        {
                            "armed": True,
                            "armed_index": signal_index,
                            "armed_date": pd.Timestamp(signal_date),
                            "armed_z60": z,
                            "peak_z60": z,
                        }
                    )
                    diagnostics["进入观察次数"] += 1
                continue

            state["peak_z60"] = max(float(state["peak_z60"]), z)
            previous_z = float(a["z60"][signal_index - 1]) if signal_index >= 1 else np.nan
            spread_declining = (
                signal_index >= 1
                and float(a["spread"][signal_index]) < float(a["spread"][signal_index - 1])
            )
            z_declining = np.isfinite(previous_z) and z < previous_z
            pullback = float(state["peak_z60"]) - z
            if params.confirmation == "decline_1d":
                confirmed = z_declining and spread_declining
            elif params.confirmation == "pullback_0p3":
                confirmed = z_declining and spread_declining and pullback >= 0.3
            else:
                confirmed = z_declining and spread_declining and pullback >= 0.5
            if not confirmed:
                continue
            diagnostics["确认信号次数"] += 1
            edge = float(a["edge"][signal_index])
            if not np.isfinite(edge) or edge < params.min_edge_points:
                diagnostics["回归空间不足次数"] += 1
                continue

            near, far = key
            near_month = contract_year_month(near)[1]
            far_month = contract_year_month(far)[1]
            month_pair = f"{near_month:02d}-{far_month:02d}"
            if global_step <= month_pair_cooldown_until.get(month_pair, -1):
                diagnostics["冷静期拒绝次数"] += 1
                continue
            candidates.append(
                {
                    "key": key,
                    "index": index,
                    "month_pair": month_pair,
                    "pullback": pullback,
                    "edge": edge,
                    "z": z,
                }
            )

        # 同日有冲突时，优先选择回落确认最强、潜在回归空间最大的组合。
        candidates.sort(key=lambda c: (-float(c["pullback"]), -float(c["edge"]), -float(c["z"])))
        for candidate in candidates:
            near, far = candidate["key"]
            month_pair = str(candidate["month_pair"])
            if near in occupied_contracts or far in occupied_contracts:
                diagnostics["合约重叠拒绝次数"] += 1
                continue
            if month_pair in occupied_month_pairs:
                diagnostics["月份组合占用拒绝次数"] += 1
                continue
            index = int(candidate["index"])
            signal_index = index - 1
            a = pair_arrays[(near, far)]
            state = states[(near, far)]
            state["position"] = {
                "armed_date": state["armed_date"],
                "armed_z60": float(state["armed_z60"]),
                "peak_z60": float(state["peak_z60"]),
                "signal_date": pd.Timestamp(a["date"][signal_index]),
                "entry_date": pd.Timestamp(a["date"][index]),
                "entry_index": index,
                "signal_z60": float(a["z60"][signal_index]),
                "z_pullback": float(candidate["pullback"]),
                "edge_points": float(candidate["edge"]),
                "near_ma20": float(a["near_ma20"][signal_index]),
                "near_ma120": float(a["near_ma120"][signal_index]),
                "trend": str(a["trend"][signal_index]),
                "entry_near_price": float(a["near_price"][index]),
                "entry_far_price": float(a["far_price"][index]),
                "entry_spread": float(a["spread"][index]),
            }
            state["armed"] = False
            occupied_contracts.update([near, far])
            occupied_month_pairs.add(month_pair)
            diagnostics["实际开仓次数"] += 1

    # 期末仍有持仓时，用该合约组合在阶段内最后一个共同交易日强平。
    for key, state in states.items():
        position = state["position"]
        if position is None:
            continue
        a = pair_arrays[key]
        eligible = np.flatnonzero((a["date"] >= phase_start) & (a["date"] <= phase_end))
        if not len(eligible):
            continue
        last_index = int(eligible[-1])
        near, far = key
        month_pair = f"{contract_year_month(near)[1]:02d}-{contract_year_month(far)[1]:02d}"
        records.append(
            make_trade_record(
                phase, params, near, far, month_pair, position, a, last_index, "数据结束强平"
            )
        )
    return pd.DataFrame(records), diagnostics


def grouped_summary(trades: pd.DataFrame, group_columns: list[str]) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    rows: list[dict[str, object]] = []
    grouper = group_columns[0] if len(group_columns) == 1 else group_columns
    for keys, group in trades.groupby(grouper, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        row = dict(zip(group_columns, keys))
        row.update(metric_values(group))
        rows.append(row)
    return pd.DataFrame(rows).sort_values(group_columns)


def years_profitable(trades: pd.DataFrame) -> int:
    if trades.empty:
        return 0
    temp = trades.copy()
    temp["年份"] = pd.to_datetime(temp["开仓日期"]).dt.year
    return int((temp.groupby("年份")["净收益(元)"].sum() > 0).sum())


def run_config(
    pair_series: dict[tuple[str, str], pd.DataFrame], params: EntryParameters
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int], dict[str, int]]:
    train, train_diag = simulate_portfolio(
        pair_series,
        params,
        "训练(2020-2024)",
        pd.Timestamp("2020-01-01"),
        pd.Timestamp("2024-12-31"),
    )
    test, test_diag = simulate_portfolio(
        pair_series,
        params,
        "测试(2025)",
        pd.Timestamp("2025-01-01"),
        pd.Timestamp("2025-12-31"),
    )
    return train, test, train_diag, test_diag


def main() -> None:
    history = load_history()
    month_pairs = training_month_pairs()
    pairs = generate_contract_pairs(history, set(month_pairs))
    by_contract = {
        contract: frame.copy() for contract, frame in history.groupby("contract", sort=False)
    }
    pair_series: dict[tuple[str, str], pd.DataFrame] = {}
    coverage_rows = []
    for near, far in pairs:
        series = enhance_pair_series(build_pair_series(by_contract, near, far))
        valid = series.loc[
            series["z60"].notna()
            & series["potential_reversion_points"].notna()
            & series["liquid"]
            & series["date"].between("2020-01-01", "2025-12-31")
            & (series["date"] < series["entry_cutoff"])
        ]
        coverage_rows.append(
            {
                "近月合约": near,
                "远月合约": far,
                "月份组合": f"{contract_year_month(near)[1]:02d}-{contract_year_month(far)[1]:02d}",
                "共同交易日": len(series),
                "满足Z值流动性及交割过滤交易日": len(valid),
                "最早日期": series["date"].min() if len(series) else pd.NaT,
                "最晚日期": series["date"].max() if len(series) else pd.NaT,
            }
        )
        if len(valid):
            pair_series[(near, far)] = series

    result_cache: dict[str, tuple[pd.DataFrame, pd.DataFrame, dict[str, int], dict[str, int]]] = {}
    grid_rows = []
    for params in parameter_grid():
        train, test, train_diag, test_diag = run_config(pair_series, params)
        result_cache[params.config_id] = (train, test, train_diag, test_diag)
        row: dict[str, object] = {
            "参数编号": params.config_id,
            "观察阈值Z": params.arm_z,
            "确认方法": confirmation_label(params.confirmation),
            "最低回归空间点": params.min_edge_points,
            "冷静期交易日": COOLDOWN_TRADING_DAYS,
            "交割前停止开仓日": NO_ENTRY_DAYS_BEFORE_DELIVERY_MONTH,
        }
        row.update(metric_values(train, "训练"))
        row["训练盈利年份数"] = years_profitable(train)
        row.update({f"训练诊断_{k}": v for k, v in train_diag.items()})
        row.update(metric_values(test, "测试"))
        row["测试盈利年份数"] = years_profitable(test)
        row.update({f"测试诊断_{k}": v for k, v in test_diag.items()})
        grid_rows.append(row)

    grid = pd.DataFrame(grid_rows)
    eligible = grid.loc[grid["训练交易数"].ge(15)].sort_values(
        ["训练统计分数", "训练净收益", "训练最大回撤"],
        ascending=[False, False, False],
    )
    best_id = str(eligible.iloc[0]["参数编号"] if len(eligible) else grid.iloc[0]["参数编号"])
    robust_eligible = grid.loc[
        grid["训练交易数"].ge(50)
        & grid["训练净收益"].gt(0)
        & grid["训练利润因子"].ge(1.10)
        & grid["训练盈利年份数"].ge(3)
    ].sort_values(
        ["训练盈利年份数", "训练最大回撤", "训练统计分数"],
        ascending=[False, False, False],
    )
    robust_id = str(
        robust_eligible.iloc[0]["参数编号"] if len(robust_eligible) else best_id
    )
    recommended = EntryParameters(1.5, "pullback_0p3", 20.0)
    rec_train, rec_test, rec_train_diag, rec_test_diag = result_cache[recommended.config_id]
    best_train, best_test, best_train_diag, best_test_diag = result_cache[best_id]
    robust_train, robust_test, robust_train_diag, robust_test_diag = result_cache[robust_id]
    recommended_trades = pd.concat([rec_train, rec_test], ignore_index=True)
    best_trades = pd.concat([best_train, best_test], ignore_index=True)
    robust_trades = pd.concat([robust_train, robust_test], ignore_index=True)

    all_for_summaries = recommended_trades.copy()
    if len(all_for_summaries):
        all_for_summaries["年份"] = pd.to_datetime(all_for_summaries["开仓日期"]).dt.year

    v1 = pd.read_csv(V1_BASELINE_TRADES, encoding="utf-8-sig", low_memory=False)
    v1_train = v1.loc[v1["阶段"].eq("训练(2020-2024)")].copy()
    v1_test = v1.loc[v1["阶段"].eq("测试(2025)")].copy()
    comparison_rows = []
    for name, train, test in [
        ("第一版基准", v1_train, v1_test),
        ("第二版预设参考规则", rec_train, rec_test),
        ("第二版训练最优", best_train, best_test),
        ("第二版训练稳健候选", robust_train, robust_test),
    ]:
        row = {"规则": name}
        row.update(metric_values(train, "训练"))
        row["训练盈利年份数"] = years_profitable(train)
        row.update(metric_values(test, "测试"))
        comparison_rows.append(row)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    grid.sort_values(["训练统计分数", "训练净收益"], ascending=[False, False]).to_csv(
        OUTPUT_DIR / "P跨期v2参数网格.csv", index=False, encoding="utf-8-sig"
    )
    recommended_trades.to_csv(
        OUTPUT_DIR / "P跨期v2推荐规则逐笔.csv",
        index=False,
        encoding="utf-8-sig",
        date_format="%Y-%m-%d",
    )
    best_trades.to_csv(
        OUTPUT_DIR / "P跨期v2训练最优逐笔.csv",
        index=False,
        encoding="utf-8-sig",
        date_format="%Y-%m-%d",
    )
    robust_trades.to_csv(
        OUTPUT_DIR / "P跨期v2训练稳健候选逐笔.csv",
        index=False,
        encoding="utf-8-sig",
        date_format="%Y-%m-%d",
    )
    pd.DataFrame(comparison_rows).to_csv(
        OUTPUT_DIR / "P跨期v2与第一版对比.csv", index=False, encoding="utf-8-sig"
    )
    grouped_summary(all_for_summaries, ["阶段", "年份"]).to_csv(
        OUTPUT_DIR / "P跨期v2推荐规则年度汇总.csv", index=False, encoding="utf-8-sig"
    )
    grouped_summary(all_for_summaries, ["阶段", "月份组合"]).to_csv(
        OUTPUT_DIR / "P跨期v2推荐规则月份汇总.csv", index=False, encoding="utf-8-sig"
    )
    grouped_summary(all_for_summaries, ["阶段", "单边趋势状态"]).to_csv(
        OUTPUT_DIR / "P跨期v2推荐规则趋势分组.csv", index=False, encoding="utf-8-sig"
    )
    grouped_summary(all_for_summaries, ["阶段", "平仓原因"]).to_csv(
        OUTPUT_DIR / "P跨期v2推荐规则平仓原因.csv", index=False, encoding="utf-8-sig"
    )
    robust_for_summaries = robust_trades.copy()
    if len(robust_for_summaries):
        robust_for_summaries["年份"] = pd.to_datetime(
            robust_for_summaries["开仓日期"]
        ).dt.year
    grouped_summary(robust_for_summaries, ["阶段", "年份"]).to_csv(
        OUTPUT_DIR / "P跨期v2稳健候选年度汇总.csv", index=False, encoding="utf-8-sig"
    )
    grouped_summary(robust_for_summaries, ["阶段", "月份组合"]).to_csv(
        OUTPUT_DIR / "P跨期v2稳健候选月份汇总.csv", index=False, encoding="utf-8-sig"
    )
    grouped_summary(robust_for_summaries, ["阶段", "单边趋势状态"]).to_csv(
        OUTPUT_DIR / "P跨期v2稳健候选趋势分组.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(coverage_rows).to_csv(
        OUTPUT_DIR / "P跨期v2数据覆盖.csv",
        index=False,
        encoding="utf-8-sig",
        date_format="%Y-%m-%d",
    )

    best_row = grid.loc[grid["参数编号"].eq(best_id)].iloc[0]
    robust_row = grid.loc[grid["参数编号"].eq(robust_id)].iloc[0]
    summary = {
        "method": "空近月/多远月",
        "version": "v2_entry_optimization",
        "entry_design": {
            "stage_1": "Z60达到观察阈值，只进入观察状态，不立即开仓",
            "stage_2": "Z60和绝对价差均回落，并满足确认强度后，下一共同交易日成交",
            "arm_expiry_trading_days": ARM_EXPIRY_TRADING_DAYS,
            "cooldown_trading_days_by_month_pair": COOLDOWN_TRADING_DAYS,
            "minimum_reversion_space_points_tested": [15.0, 20.0],
            "no_entry_days_before_delivery_month": NO_ENTRY_DAYS_BEFORE_DELIVERY_MONTH,
            "overlap_limits": ["同一合约不能同时参与多组价差", "同一月份组合最多一笔持仓"],
            "outright_trend": "仅记录并分组观察，不参与开仓",
        },
        "fixed_exit_design": {
            "take_profit_points": TAKE_PROFIT_POINTS,
            "stop_loss_points": STOP_LOSS_POINTS,
            "max_holding_days": MAX_HOLDING_DAYS,
            "exit_abs_z": EXIT_ABS_Z,
        },
        "costs": {
            "multiplier": MULTIPLIER,
            "pair_round_trip_fee_rmb": PAIR_ROUND_TRIP_FEE,
            "slippage_points": SLIPPAGE_POINTS,
        },
        "recommended_parameters": asdict(recommended),
        "recommended_metrics": {
            **metric_values(rec_train, "训练"),
            "训练盈利年份数": years_profitable(rec_train),
            **metric_values(rec_test, "测试"),
        },
        "recommended_diagnostics": {
            "训练": rec_train_diag,
            "测试": rec_test_diag,
        },
        "training_best_config_id": best_id,
        "training_best_parameters": {
            "arm_z": float(best_row["观察阈值Z"]),
            "confirmation": str(best_row["确认方法"]),
            "min_edge_points": float(best_row["最低回归空间点"]),
        },
        "training_best_metrics": {
            **metric_values(best_train, "训练"),
            "训练盈利年份数": years_profitable(best_train),
            **metric_values(best_test, "测试"),
        },
        "training_best_diagnostics": {
            "训练": best_train_diag,
            "测试": best_test_diag,
        },
        "training_robust_candidate_selection": {
            "criteria": [
                "训练交易数>=50",
                "训练净收益>0",
                "训练利润因子>=1.10",
                "2020-2024至少3个盈利年份",
            ],
            "config_id": robust_id,
            "parameters": {
                "arm_z": float(robust_row["观察阈值Z"]),
                "confirmation": str(robust_row["确认方法"]),
                "min_edge_points": float(robust_row["最低回归空间点"]),
            },
            "metrics": {
                **metric_values(robust_train, "训练"),
                "训练盈利年份数": years_profitable(robust_train),
                **metric_values(robust_test, "测试"),
            },
            "diagnostics": {
                "训练": robust_train_diag,
                "测试": robust_test_diag,
            },
        },
        "warning": "2025只用于样本外检验；单边趋势仅作结果分组，不参与参数选择。",
    }
    with (OUTPUT_DIR / "P跨期v2回测摘要.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, default=str)

    print("第二版参数组合：", len(grid))
    print("有效合约对：", len(pair_series))
    print("推荐规则：", asdict(recommended))
    print("推荐规则训练：", metric_values(rec_train, "训练"))
    print("推荐规则测试：", metric_values(rec_test, "测试"))
    print("训练最优参数编号：", best_id)
    print("训练最优训练：", metric_values(best_train, "训练"))
    print("训练最优测试：", metric_values(best_test, "测试"))
    print("训练稳健候选参数编号：", robust_id)
    print("训练稳健候选训练：", metric_values(robust_train, "训练"))
    print("训练稳健候选测试：", metric_values(robust_test, "测试"))
    print("输出目录：", OUTPUT_DIR)


if __name__ == "__main__":
    main()
