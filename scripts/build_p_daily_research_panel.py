from __future__ import annotations

from project_config import PROJECT_ROOT, RAW_STATEMENTS_DIR, MARKET_DATA_DIR

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd



CODE_DIR = Path(__file__).resolve().parent
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from analyze_p_calendar_entry_exit import (  # noqa: E402
    load_continuous_history,
    load_contract_history,
    rolling_percentile,
)
from analyze_p_main_liquid_far import choose_monthly_contracts  # noqa: E402
from backtest_p_calendar_v1 import (  # noqa: E402
    MIN_OPEN_INTEREST_EACH_LEG,
    MIN_VOLUME_EACH_LEG,
    contract_year_month,
)


EVENTS_CSV = (
    PROJECT_ROOT
    / "output"
    / "P跨期套利"
    / "开平仓分析"
    / "P跨期逐笔开平仓特征.csv"
)
OUTPUT_DIR = PROJECT_ROOT / "output" / "P跨期套利" / "每日研究"
DAILY_CSV = OUTPUT_DIR / "P主力远月_每日研究表.csv"
EVENT_OUTPUT_CSV = OUTPUT_DIR / "P主力远月_历史交易开仓事件.csv"
SUMMARY_JSON = OUTPUT_DIR / "P主力远月_每日研究摘要.json"

NO_ENTRY_DAYS_BEFORE_DELIVERY_MONTH = 30


def phase_label(date: pd.Timestamp) -> str:
    if date.year <= 2019:
        return "历史预热(2018-2019)"
    if date.year <= 2024:
        return "研究样本(2020-2024)"
    if date.year == 2025:
        return "样本外测试(2025)"
    return "观察样本(2026)"


def contract_delivery_start(contract: str) -> pd.Timestamp:
    year_month = contract_year_month(contract)
    if year_month is None:
        return pd.NaT
    return pd.Timestamp(year_month[0], year_month[1], 1)


def build_pair_features(
    history_by_contract: dict[str, pd.DataFrame],
    continuous: pd.DataFrame,
    main_contract: str,
    far_contract: str,
) -> pd.DataFrame:
    main = history_by_contract.get(main_contract)
    far = history_by_contract.get(far_contract)
    if main is None or far is None:
        return pd.DataFrame()

    main_columns = {
        "price": "主力结算价",
        "volume": "主力成交量",
        "open_interest": "主力持仓量",
    }
    far_columns = {
        "price": "远月结算价",
        "volume": "远月成交量",
        "open_interest": "远月持仓量",
    }
    pair = main[["date", *main_columns]].rename(columns=main_columns).merge(
        far[["date", *far_columns]].rename(columns=far_columns),
        on="date",
        how="inner",
    )
    pair = pair.sort_values("date").drop_duplicates("date", keep="last")
    if pair.empty:
        return pair

    pair["价差"] = pair["主力结算价"] - pair["远月结算价"]
    pair["价差百分比"] = pair["主力结算价"] / pair["远月结算价"] - 1
    pair["对数价差"] = np.log(pair["主力结算价"] / pair["远月结算价"])

    for window, minimum in ((20, 15), (60, 40)):
        mean = pair["价差百分比"].rolling(window, min_periods=minimum).mean()
        std = pair["价差百分比"].rolling(window, min_periods=minimum).std(ddof=1)
        pair[f"价差{window}日均值"] = mean
        pair[f"Z{window}"] = (pair["价差百分比"] - mean) / std.replace(0, np.nan)

    pair["价差60日分位"] = rolling_percentile(pair["价差百分比"])
    pair["价差1日变化"] = pair["价差百分比"].diff(1)
    pair["价差5日变化"] = pair["价差百分比"] - pair["价差百分比"].shift(5)
    pair["价差20日变化"] = pair["价差百分比"] - pair["价差百分比"].shift(20)
    pair["价差20日年化波动率"] = (
        pair["价差1日变化"].rolling(20, min_periods=15).std(ddof=1)
        * math.sqrt(252)
    )

    pair["远月成交量占主力"] = pair["远月成交量"] / pair["主力成交量"].replace(0, np.nan)
    pair["远月持仓量占主力"] = pair["远月持仓量"] / pair["主力持仓量"].replace(0, np.nan)
    pair["两腿当日流动性合格"] = (
        pair["主力成交量"].ge(MIN_VOLUME_EACH_LEG)
        & pair["远月成交量"].ge(MIN_VOLUME_EACH_LEG)
        & pair["主力持仓量"].ge(MIN_OPEN_INTEREST_EACH_LEG)
        & pair["远月持仓量"].ge(MIN_OPEN_INTEREST_EACH_LEG)
    )

    pair = pair.merge(continuous, on="date", how="left")
    bull = pair["主连20日收益"].gt(0) & pair["主连均线趋势"].gt(0)
    bear = pair["主连20日收益"].lt(0) & pair["主连均线趋势"].lt(0)
    pair["主力趋势状态"] = np.select(
        [bull, bear], ["牛市状态", "熊市状态"], default="趋势混合/不足"
    )

    delivery_start = contract_delivery_start(main_contract)
    pair["主力交割月首日"] = delivery_start
    pair["距主力交割月日历天数"] = (delivery_start - pair["date"]).dt.days
    pair["停止新开仓日"] = delivery_start - pd.Timedelta(
        days=NO_ENTRY_DAYS_BEFORE_DELIVERY_MONTH
    )
    pair["允许新开仓"] = (
        pair["两腿当日流动性合格"] & pair["date"].lt(pair["停止新开仓日"])
    )
    return pair.reset_index(drop=True)


def build_daily_panel(
    selections: pd.DataFrame,
    history: pd.DataFrame,
    continuous: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[tuple[str, str], pd.DataFrame]]:
    history_by_contract = {
        contract: frame.copy()
        for contract, frame in history.groupby("contract", sort=False)
    }
    pair_cache: dict[tuple[str, str], pd.DataFrame] = {}
    parts: list[pd.DataFrame] = []

    selected = selections.dropna(subset=["主力合约", "远月合约"])
    for _, selection in selected.iterrows():
        main_contract = str(selection["主力合约"])
        far_contract = str(selection["远月合约"])
        key = (main_contract, far_contract)
        if key not in pair_cache:
            pair_cache[key] = build_pair_features(
                history_by_contract, continuous, main_contract, far_contract
            )
        pair = pair_cache[key]
        period = pd.Timestamp(selection["月份"]).to_period("M")
        part = pair.loc[pair["date"].dt.to_period("M").eq(period)].copy()
        if part.empty:
            continue
        part.insert(0, "选择月份", period.to_timestamp())
        part.insert(2, "主力合约", main_contract)
        part.insert(3, "远月合约", far_contract)
        part.insert(4, "期限差月", int(selection["期限差月"]))
        part["选择参考截止日"] = selection["选择参考截止日"]
        part["主力选择时5日均成交量"] = selection["主力5日均成交量"]
        part["主力选择时5日均持仓量"] = selection["主力5日均持仓量"]
        part["远月选择时5日均成交量"] = selection["远月5日均成交量"]
        part["远月选择时5日均持仓量"] = selection["远月5日均持仓量"]
        part["选择时远月成交量占主力"] = selection["远月成交量占主力"]
        parts.append(part)

    if not parts:
        return pd.DataFrame(), pair_cache

    daily = pd.concat(parts, ignore_index=True)
    daily = daily.sort_values(["date", "主力合约", "远月合约"])
    daily = daily.drop_duplicates("date", keep="first").reset_index(drop=True)
    daily["样本阶段"] = daily["date"].map(phase_label)
    daily["日历月"] = daily["date"].dt.month
    daily["季度"] = "Q" + daily["date"].dt.quarter.astype(str)
    return daily, pair_cache


def join_text(values: pd.Series) -> str:
    items = [str(v) for v in values.dropna().astype(str) if str(v).strip()]
    return "、".join(dict.fromkeys(items))


def label_historical_entries(
    daily: pd.DataFrame,
    selections: pd.DataFrame,
    pair_cache: dict[tuple[str, str], pd.DataFrame],
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    events = pd.read_csv(EVENTS_CSV, encoding="utf-8-sig", low_memory=False)
    for column in ["组合开始", "组合结束", "开仓信号日"]:
        events[column] = pd.to_datetime(events[column], errors="coerce")
    events["月份键"] = events["组合开始"].dt.to_period("M")

    selection_map = selections.dropna(subset=["主力合约", "远月合约"]).copy()
    selection_map["月份键"] = pd.to_datetime(selection_map["月份"]).dt.to_period("M")
    events = events.merge(
        selection_map[["月份键", "主力合约", "远月合约"]].rename(
            columns={"远月合约": "筛选远月合约"}
        ),
        on="月份键",
        how="left",
    )
    events["匹配当月主力组合"] = (
        events["近月合约"].eq(events["主力合约"])
        & events["远月合约"].eq(events["筛选远月合约"])
    )
    matched = events.loc[events["匹配当月主力组合"]].copy()

    actual_map: dict[pd.Timestamp, list[pd.Series]] = {}
    signal_map: dict[pd.Timestamp, list[pd.Series]] = {}
    event_records: list[dict[str, object]] = []

    for _, event in matched.iterrows():
        actual_date = pd.Timestamp(event["组合开始"])
        key = (str(event["近月合约"]), str(event["远月合约"]))
        pair = pair_cache.get(key, pd.DataFrame())
        prior = pair.loc[pair["date"].lt(actual_date)].tail(1) if not pair.empty else pd.DataFrame()
        signal_date = pd.NaT if prior.empty else pd.Timestamp(prior.iloc[0]["date"])
        signal_row = None if prior.empty else prior.iloc[0]

        actual_map.setdefault(actual_date, []).append(event)
        if pd.notna(signal_date):
            signal_map.setdefault(signal_date, []).append(event)

        record = {
            "候选编号": event["候选编号"],
            "组合开始": event["组合开始"],
            "组合结束": event["组合结束"],
            "交易结构": event["交易结构"],
            "主力合约": event["主力合约"],
            "远月合约": event["筛选远月合约"],
            "期限差月": event["近远月间隔(月)"],
            "保守独立配对": event["保守独立配对"],
            "存在共享持仓腿": event["存在共享持仓腿"],
            "组合净收益(候选口径)": event["组合净收益(候选口径)"],
            "事前特征日": signal_date,
            "事前特征日位于每日研究表": False,
        }
        if signal_row is not None:
            for column in [
                "主力结算价", "远月结算价", "价差", "价差百分比", "Z20", "Z60",
                "价差60日分位", "价差5日变化", "价差20日变化", "价差20日年化波动率",
                "远月成交量占主力", "远月持仓量占主力", "主连5日收益", "主连20日收益",
                "主连60日收益", "主连均线趋势", "主力趋势状态", "距主力交割月日历天数",
                "两腿当日流动性合格", "允许新开仓",
            ]:
                record[f"事前_{column}"] = signal_row.get(column, np.nan)
            in_daily = daily.loc[
                daily["date"].eq(signal_date)
                & daily["主力合约"].eq(key[0])
                & daily["远月合约"].eq(key[1])
            ]
            record["事前特征日位于每日研究表"] = not in_daily.empty
        event_records.append(record)

    daily = daily.copy()
    daily["历史交易当日组合开始笔数"] = 0
    daily["历史交易当日候选编号"] = ""
    daily["历史交易当日开仓方向"] = ""
    daily["历史交易当日独立配对笔数"] = 0
    daily["历史交易当日共享腿笔数"] = 0
    daily["下一交易日历史交易开仓笔数"] = 0
    daily["事前标签候选编号"] = ""
    daily["事前标签开仓方向"] = ""
    daily["事前标签独立配对笔数"] = 0

    for index, row in daily.iterrows():
        date = pd.Timestamp(row["date"])
        key = (str(row["主力合约"]), str(row["远月合约"]))
        actual = [
            e for e in actual_map.get(date, [])
            if (str(e["近月合约"]), str(e["远月合约"])) == key
        ]
        signal = [
            e for e in signal_map.get(date, [])
            if (str(e["近月合约"]), str(e["远月合约"])) == key
        ]
        if actual:
            frame = pd.DataFrame(actual)
            daily.at[index, "历史交易当日组合开始笔数"] = len(frame)
            daily.at[index, "历史交易当日候选编号"] = join_text(frame["候选编号"])
            daily.at[index, "历史交易当日开仓方向"] = join_text(frame["交易结构"])
            daily.at[index, "历史交易当日独立配对笔数"] = int(frame["保守独立配对"].sum())
            daily.at[index, "历史交易当日共享腿笔数"] = int(frame["存在共享持仓腿"].sum())
        if signal:
            frame = pd.DataFrame(signal)
            daily.at[index, "下一交易日历史交易开仓笔数"] = len(frame)
            daily.at[index, "事前标签候选编号"] = join_text(frame["候选编号"])
            daily.at[index, "事前标签开仓方向"] = join_text(frame["交易结构"])
            daily.at[index, "事前标签独立配对笔数"] = int(frame["保守独立配对"].sum())

    event_table = pd.DataFrame(event_records).sort_values(["组合开始", "候选编号"])
    stats = {
        "全部P跨期候选": int(len(events)),
        "匹配当月主力组合候选": int(len(matched)),
        "匹配且保守独立候选": int(matched["保守独立配对"].sum()),
        "每日研究表已标记实际开仓候选": int(daily["历史交易当日组合开始笔数"].sum()),
        "每日研究表已标记事前标签候选": int(daily["下一交易日历史交易开仓笔数"].sum()),
        "事件表有事前特征候选": int(event_table["事前特征日"].notna().sum()),
        "事前特征日位于每日研究表候选": int(event_table["事前特征日位于每日研究表"].sum()),
    }
    return daily, event_table, stats


def reorder_daily_columns(daily: pd.DataFrame) -> pd.DataFrame:
    ordered = [
        "date", "选择月份", "样本阶段", "日历月", "季度", "主力合约", "远月合约", "期限差月",
        "主力结算价", "远月结算价", "价差", "价差百分比", "对数价差", "Z20", "Z60",
        "价差60日分位", "价差1日变化", "价差5日变化", "价差20日变化", "价差20日年化波动率",
        "主力成交量", "远月成交量", "远月成交量占主力", "主力持仓量", "远月持仓量", "远月持仓量占主力",
        "两腿当日流动性合格", "允许新开仓", "主连价格", "主连5日收益", "主连20日收益", "主连60日收益",
        "主连均线趋势", "主力趋势状态", "主力交割月首日", "距主力交割月日历天数", "停止新开仓日",
        "选择参考截止日", "主力选择时5日均成交量", "主力选择时5日均持仓量", "远月选择时5日均成交量",
        "远月选择时5日均持仓量", "选择时远月成交量占主力", "历史交易当日组合开始笔数", "历史交易当日候选编号",
        "历史交易当日开仓方向", "历史交易当日独立配对笔数", "历史交易当日共享腿笔数", "下一交易日历史交易开仓笔数",
        "事前标签候选编号", "事前标签开仓方向", "事前标签独立配对笔数",
    ]
    return daily[[column for column in ordered if column in daily.columns]]


def main() -> None:
    history = load_contract_history()
    continuous = load_continuous_history()
    selections = choose_monthly_contracts(history)
    daily, pair_cache = build_daily_panel(selections, history, continuous)
    daily, events, label_stats = label_historical_entries(daily, selections, pair_cache)
    daily = reorder_daily_columns(daily)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    daily.to_csv(DAILY_CSV, index=False, encoding="utf-8-sig", date_format="%Y-%m-%d")
    events.to_csv(
        EVENT_OUTPUT_CSV, index=False, encoding="utf-8-sig", date_format="%Y-%m-%d"
    )

    summary = {
        "data_range": {
            "start": str(daily["date"].min().date()),
            "end": str(daily["date"].max().date()),
        },
        "daily_rows": int(len(daily)),
        "selected_months": int(daily["选择月份"].nunique()),
        "pair_count": int(daily[["主力合约", "远月合约"]].drop_duplicates().shape[0]),
        "entry_allowed_days": int(daily["允许新开仓"].sum()),
        "z60_available_days": int(daily["Z60"].notna().sum()),
        "label_stats": label_stats,
        "definitions": {
            "spread": "主力结算价-远月结算价",
            "spread_ratio": "主力结算价/远月结算价-1",
            "advance_label": "当日收盘后已知特征，对应下一交易日是否出现历史交易开仓，避免使用开仓日收盘价预测当日开仓",
            "pair_selection": "每月固定；使用月初以前5个交易日选择主力，以及主力后3至5个月中流动性最高的远月",
        },
        "sources": [
            "yp_lseg_full_contract_history.csv",
            "yp_lseg_continuous_history.csv",
            "P跨期逐笔开平仓特征.csv",
        ],
    }
    SUMMARY_JSON.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"每日研究表: {DAILY_CSV}")
    print(f"历史交易开仓事件: {EVENT_OUTPUT_CSV}")


if __name__ == "__main__":
    main()
