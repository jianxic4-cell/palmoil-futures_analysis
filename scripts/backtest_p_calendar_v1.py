from __future__ import annotations

from project_config import PROJECT_ROOT, RAW_STATEMENTS_DIR, MARKET_DATA_DIR

import itertools
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = PROJECT_ROOT
HISTORY_CSV = MARKET_DATA_DIR / "yp_lseg_full_contract_history.csv"
EVENTS_CSV = ROOT / "output" / "P跨期套利" / "开平仓分析" / "P跨期逐笔开平仓特征.csv"
OUTPUT_DIR = ROOT / "output" / "P跨期套利" / "回测_v1"

MULTIPLIER = 10.0
PAIR_ROUND_TRIP_FEE = 10.08
SLIPPAGE_POINTS = 4.0
MIN_VOLUME_EACH_LEG = 100.0
MIN_OPEN_INTEREST_EACH_LEG = 500.0


@dataclass(frozen=True)
class Parameters:
    entry_z: float
    take_profit_points: float
    stop_loss_points: float
    max_holding_days: int
    exit_abs_z: float | None

    @property
    def config_id(self) -> str:
        exit_label = "off" if self.exit_abs_z is None else str(self.exit_abs_z).replace(".", "p")
        return (
            f"z{str(self.entry_z).replace('.', 'p')}_"
            f"tp{int(self.take_profit_points)}_sl{int(self.stop_loss_points)}_"
            f"h{self.max_holding_days}_xz{exit_label}"
        )


def contract_expiry_ordinal(contract: str) -> int | None:
    match = re.search(r"(\d{4})$", str(contract))
    if not match:
        return None
    code = match.group(1)
    month = int(code[-2:])
    year = 2000 + int(code[:2])
    if not 1 <= month <= 12:
        return None
    return year * 12 + month


def contract_year_month(contract: str) -> tuple[int, int] | None:
    match = re.search(r"(\d{4})$", str(contract))
    if not match:
        return None
    code = match.group(1)
    year, month = 2000 + int(code[:2]), int(code[-2:])
    return (year, month) if 1 <= month <= 12 else None


def load_history() -> pd.DataFrame:
    history = pd.read_csv(HISTORY_CSV, low_memory=False)
    history = history.loc[history["product"].eq("P")].copy()
    history["date"] = pd.to_datetime(history["date"], errors="coerce")
    for column in ["settle", "last", "volume", "open_interest"]:
        history[column] = pd.to_numeric(history[column], errors="coerce")
    history["price"] = history["settle"].combine_first(history["last"])
    return (
        history.dropna(subset=["contract", "date", "price"])
        .drop_duplicates(["contract", "date"], keep="last")
        .sort_values(["contract", "date"])
    )


def training_month_pairs() -> list[str]:
    events = pd.read_csv(EVENTS_CSV, encoding="utf-8-sig", low_memory=False)
    mask = (
        events["样本阶段"].eq("研究样本(2020-2024)")
        & events["交易结构"].eq("空近月/多远月")
    )
    return sorted(events.loc[mask, "月份组合"].dropna().astype(str).unique().tolist())


def generate_contract_pairs(history: pd.DataFrame, month_pairs: set[str]) -> list[tuple[str, str]]:
    contracts = []
    for contract in sorted(history["contract"].astype(str).unique()):
        ordinal = contract_expiry_ordinal(contract)
        year_month = contract_year_month(contract)
        if ordinal is not None and year_month is not None:
            contracts.append((contract, ordinal, year_month[1]))

    pairs = []
    for near, far in itertools.combinations(contracts, 2):
        if near[1] > far[1]:
            near, far = far, near
        gap = far[1] - near[1]
        month_pair = f"{near[2]:02d}-{far[2]:02d}"
        if 0 < gap <= 12 and month_pair in month_pairs:
            pairs.append((near[0], far[0]))
    return sorted(set(pairs))


def build_pair_series(
    history_by_contract: dict[str, pd.DataFrame], near_contract: str, far_contract: str
) -> pd.DataFrame:
    near = history_by_contract[near_contract][
        ["date", "price", "volume", "open_interest"]
    ].rename(
        columns={
            "price": "near_price",
            "volume": "near_volume",
            "open_interest": "near_oi",
        }
    )
    far = history_by_contract[far_contract][
        ["date", "price", "volume", "open_interest"]
    ].rename(
        columns={
            "price": "far_price",
            "volume": "far_volume",
            "open_interest": "far_oi",
        }
    )
    pair = near.merge(far, on="date", how="inner").sort_values("date")
    pair["spread"] = pair["near_price"] - pair["far_price"]
    pair["spread_ratio"] = pair["near_price"] / pair["far_price"] - 1
    rolling_mean = pair["spread_ratio"].rolling(60, min_periods=40).mean()
    rolling_std = pair["spread_ratio"].rolling(60, min_periods=40).std(ddof=1)
    pair["z60"] = (pair["spread_ratio"] - rolling_mean) / rolling_std.replace(0, np.nan)
    pair["liquid"] = (
        pair["near_volume"].ge(MIN_VOLUME_EACH_LEG)
        & pair["far_volume"].ge(MIN_VOLUME_EACH_LEG)
        & pair["near_oi"].ge(MIN_OPEN_INTEREST_EACH_LEG)
        & pair["far_oi"].ge(MIN_OPEN_INTEREST_EACH_LEG)
    )
    near_year, near_month = contract_year_month(near_contract) or (1900, 1)
    pair["near_delivery_start"] = pd.Timestamp(near_year, near_month, 1)
    return pair.reset_index(drop=True)


def simulate_pair(
    pair: pd.DataFrame,
    near_contract: str,
    far_contract: str,
    params: Parameters,
    phase: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> list[dict[str, object]]:
    if len(pair) < 3:
        return []
    dates = pair["date"].to_numpy(dtype="datetime64[ns]")
    near_prices = pair["near_price"].to_numpy(dtype=float)
    far_prices = pair["far_price"].to_numpy(dtype=float)
    spreads = pair["spread"].to_numpy(dtype=float)
    z60_values = pair["z60"].to_numpy(dtype=float)
    liquid = pair["liquid"].to_numpy(dtype=bool)
    delivery_starts = pair["near_delivery_start"].to_numpy(dtype="datetime64[ns]")
    phase_start = np.datetime64(start.to_datetime64())
    phase_end = np.datetime64(end.to_datetime64())
    near_month = contract_year_month(near_contract)[1]
    far_month = contract_year_month(far_contract)[1]
    month_pair = f"{near_month:02d}-{far_month:02d}"
    records: list[dict[str, object]] = []
    position: dict[str, object] | None = None

    for index in range(1, len(pair)):
        execution_date = dates[index]
        signal_index = index - 1
        signal_date = dates[signal_index]
        if execution_date < phase_start or execution_date > phase_end:
            continue

        if position is not None:
            entry_spread = float(position["entry_spread"])
            signal_pnl_points = entry_spread - spreads[signal_index]
            trading_days_held = signal_index - int(position["entry_index"])
            reason = None
            if signal_pnl_points >= params.take_profit_points:
                reason = "止盈"
            elif signal_pnl_points <= -params.stop_loss_points:
                reason = "止损"
            elif params.exit_abs_z is not None and np.isfinite(z60_values[signal_index]) and abs(z60_values[signal_index]) <= params.exit_abs_z:
                reason = "Z60回归"
            elif trading_days_held >= params.max_holding_days:
                reason = "最长持有"
            elif execution_date >= delivery_starts[index]:
                reason = "交割月前退出"

            if reason is not None:
                exit_spread = spreads[index]
                gross_points = entry_spread - exit_spread
                gross_pnl = gross_points * MULTIPLIER
                net_pnl = gross_pnl - PAIR_ROUND_TRIP_FEE - SLIPPAGE_POINTS * MULTIPLIER
                entry_date = pd.Timestamp(position["entry_date"])
                records.append(
                    {
                        "阶段": phase,
                        "参数编号": params.config_id,
                        "近月合约": near_contract,
                        "远月合约": far_contract,
                        "月份组合": month_pair,
                        "信号日期": position["signal_date"],
                        "开仓日期": entry_date,
                        "平仓日期": pd.Timestamp(execution_date),
                        "开仓Z60": position["entry_signal_z60"],
                        "开仓近月价": position["entry_near_price"],
                        "开仓远月价": position["entry_far_price"],
                        "开仓价差": entry_spread,
                        "平仓近月价": near_prices[index],
                        "平仓远月价": far_prices[index],
                        "平仓价差": exit_spread,
                        "毛收益点数": gross_points,
                        "毛收益(元)": gross_pnl,
                        "手续费(元)": PAIR_ROUND_TRIP_FEE,
                        "滑点成本(元)": SLIPPAGE_POINTS * MULTIPLIER,
                        "净收益(元)": net_pnl,
                        "持仓交易日": index - int(position["entry_index"]),
                        "平仓原因": reason,
                    }
                )
                position = None
                continue

        if position is None:
            valid_signal = (
                phase_start <= signal_date <= phase_end
                and signal_date < delivery_starts[signal_index]
                and execution_date < delivery_starts[index]
                and liquid[signal_index]
                and np.isfinite(z60_values[signal_index])
                and z60_values[signal_index] >= params.entry_z
            )
            if valid_signal:
                position = {
                    "signal_date": pd.Timestamp(signal_date),
                    "entry_date": pd.Timestamp(execution_date),
                    "entry_index": index,
                    "entry_signal_z60": z60_values[signal_index],
                    "entry_near_price": near_prices[index],
                    "entry_far_price": far_prices[index],
                    "entry_spread": spreads[index],
                }

    if position is not None:
        eligible_indices = np.flatnonzero((dates >= phase_start) & (dates <= phase_end))
        if len(eligible_indices):
            last_index = int(eligible_indices[-1])
            exit_spread = spreads[last_index]
            gross_points = float(position["entry_spread"]) - exit_spread
            records.append(
                {
                    "阶段": phase,
                    "参数编号": params.config_id,
                    "近月合约": near_contract,
                    "远月合约": far_contract,
                    "月份组合": month_pair,
                    "信号日期": position["signal_date"],
                    "开仓日期": position["entry_date"],
                    "平仓日期": pd.Timestamp(dates[last_index]),
                    "开仓Z60": position["entry_signal_z60"],
                    "开仓近月价": position["entry_near_price"],
                    "开仓远月价": position["entry_far_price"],
                    "开仓价差": position["entry_spread"],
                    "平仓近月价": near_prices[last_index],
                    "平仓远月价": far_prices[last_index],
                    "平仓价差": exit_spread,
                    "毛收益点数": gross_points,
                    "毛收益(元)": gross_points * MULTIPLIER,
                    "手续费(元)": PAIR_ROUND_TRIP_FEE,
                    "滑点成本(元)": SLIPPAGE_POINTS * MULTIPLIER,
                    "净收益(元)": gross_points * MULTIPLIER - PAIR_ROUND_TRIP_FEE - SLIPPAGE_POINTS * MULTIPLIER,
                    "持仓交易日": max(0, last_index - int(position["entry_index"])),
                    "平仓原因": "数据结束强平",
                }
            )
    return records


def metrics(trades: pd.DataFrame, prefix: str) -> dict[str, object]:
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


def month_summary(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    rows = []
    for (phase, month_pair), group in trades.groupby(["阶段", "月份组合"]):
        row = {"阶段": phase, "月份组合": month_pair}
        row.update(metrics(group, ""))
        rows.append(row)
    result = pd.DataFrame(rows)
    result.columns = [column[0].lower() + column[1:] if column.startswith("交") else column for column in result.columns]
    return result.sort_values(["阶段", "净收益"], ascending=[True, False])


def run_config(
    pair_series: dict[tuple[str, str], pd.DataFrame], params: Parameters
) -> tuple[pd.DataFrame, pd.DataFrame]:
    train_records = []
    test_records = []
    for (near, far), series in pair_series.items():
        train_records.extend(
            simulate_pair(
                series,
                near,
                far,
                params,
                "训练(2020-2024)",
                pd.Timestamp("2020-01-01"),
                pd.Timestamp("2024-12-31"),
            )
        )
        test_records.extend(
            simulate_pair(
                series,
                near,
                far,
                params,
                "测试(2025)",
                pd.Timestamp("2025-01-01"),
                pd.Timestamp("2025-12-31"),
            )
        )
    return pd.DataFrame(train_records), pd.DataFrame(test_records)


def parameter_grid() -> list[Parameters]:
    return [
        Parameters(*values)
        for values in itertools.product(
            [1.0, 1.5, 2.0],
            [10.0, 20.0, 30.0],
            [20.0, 30.0, 40.0],
            [5, 10, 20],
            [None, 0.5],
        )
    ]


def main() -> None:
    history = load_history()
    month_pairs = training_month_pairs()
    pairs = generate_contract_pairs(history, set(month_pairs))
    by_contract = {
        contract: frame.copy() for contract, frame in history.groupby("contract", sort=False)
    }
    pair_series = {}
    coverage_rows = []
    for near, far in pairs:
        series = build_pair_series(by_contract, near, far)
        valid = series.loc[
            series["z60"].notna()
            & series["liquid"]
            & series["date"].between("2020-01-01", "2025-12-31")
            & (series["date"] < series["near_delivery_start"])
        ]
        coverage_rows.append(
            {
                "近月合约": near,
                "远月合约": far,
                "月份组合": f"{contract_year_month(near)[1]:02d}-{contract_year_month(far)[1]:02d}",
                "共同交易日": len(series),
                "满足窗口和流动性交易日": len(valid),
                "最早日期": series["date"].min() if len(series) else pd.NaT,
                "最晚日期": series["date"].max() if len(series) else pd.NaT,
            }
        )
        if len(valid) > 0:
            pair_series[(near, far)] = series

    grid_rows = []
    for params in parameter_grid():
        train, test = run_config(pair_series, params)
        row = {"参数编号": params.config_id, **asdict(params)}
        row.update(metrics(train, "训练"))
        row.update(metrics(test, "测试"))
        grid_rows.append(row)
    grid = pd.DataFrame(grid_rows)
    eligible = grid.loc[grid["训练交易数"] >= 15].copy()
    eligible = eligible.sort_values(
        ["训练统计分数", "训练净收益", "训练最大回撤"],
        ascending=[False, False, False],
    )
    best_id = eligible.iloc[0]["参数编号"] if not eligible.empty else grid.iloc[0]["参数编号"]
    best_row = grid.loc[grid["参数编号"].eq(best_id)].iloc[0]
    best_params = Parameters(
        float(best_row["entry_z"]),
        float(best_row["take_profit_points"]),
        float(best_row["stop_loss_points"]),
        int(best_row["max_holding_days"]),
        None if pd.isna(best_row["exit_abs_z"]) else float(best_row["exit_abs_z"]),
    )
    baseline_params = Parameters(1.0, 20.0, 20.0, 10, 0.5)
    baseline_train, baseline_test = run_config(pair_series, baseline_params)
    best_train, best_test = run_config(pair_series, best_params)
    baseline_trades = pd.concat([baseline_train, baseline_test], ignore_index=True)
    best_trades = pd.concat([best_train, best_test], ignore_index=True)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    grid.sort_values(["训练统计分数", "训练净收益"], ascending=[False, False]).to_csv(
        OUTPUT_DIR / "P跨期参数网格.csv", index=False, encoding="utf-8-sig"
    )
    eligible.head(30).to_csv(
        OUTPUT_DIR / "P跨期训练前30参数.csv", index=False, encoding="utf-8-sig"
    )
    baseline_trades.to_csv(
        OUTPUT_DIR / "P跨期基准规则逐笔.csv", index=False, encoding="utf-8-sig", date_format="%Y-%m-%d"
    )
    best_trades.to_csv(
        OUTPUT_DIR / "P跨期训练最优逐笔.csv", index=False, encoding="utf-8-sig", date_format="%Y-%m-%d"
    )
    month_summary(baseline_trades).to_csv(
        OUTPUT_DIR / "P跨期基准规则月份汇总.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(coverage_rows).to_csv(
        OUTPUT_DIR / "P跨期回测数据覆盖.csv", index=False, encoding="utf-8-sig", date_format="%Y-%m-%d"
    )

    baseline_metrics = {
        **metrics(baseline_train, "训练"),
        **metrics(baseline_test, "测试"),
    }
    best_metrics = {**metrics(best_train, "训练"), **metrics(best_test, "测试")}
    summary = {
        "method": "空近月/多远月",
        "month_pairs_selected_from_2020_2024_activity": month_pairs,
        "contract_pairs_with_valid_data": len(pair_series),
        "assumptions": {
            "signal_execution": "当日结算产生信号，下一共同交易日结算成交",
            "multiplier": MULTIPLIER,
            "pair_round_trip_fee_rmb": PAIR_ROUND_TRIP_FEE,
            "slippage_points": SLIPPAGE_POINTS,
            "minimum_volume_each_leg": MIN_VOLUME_EACH_LEG,
            "minimum_open_interest_each_leg": MIN_OPEN_INTEREST_EACH_LEG,
            "delivery_rule": "进入近月交割月前退出",
        },
        "baseline_parameters": asdict(baseline_params),
        "baseline_metrics": baseline_metrics,
        "training_best_parameters": asdict(best_params),
        "training_best_metrics": best_metrics,
        "warning": "训练最优仅按2020-2024选择，2025仅用于检验，不应用测试结果反向挑参数。",
    }
    with (OUTPUT_DIR / "P跨期回测摘要.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, default=str)

    print("训练期活动月份组合：", month_pairs)
    print("有效合约对：", len(pair_series))
    print("参数组合：", len(grid))
    print("基准参数：", asdict(baseline_params))
    print("基准训练：", baseline_metrics)
    print("训练最优参数：", asdict(best_params))
    print("训练最优结果：", best_metrics)
    print("输出目录：", OUTPUT_DIR)


if __name__ == "__main__":
    main()
