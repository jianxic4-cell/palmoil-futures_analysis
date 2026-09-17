from __future__ import annotations

from project_config import PROJECT_ROOT, RAW_STATEMENTS_DIR, MARKET_DATA_DIR

import json
import math
import re
from pathlib import Path

import numpy as np
import pandas as pd



TRADES_CSV = PROJECT_ROOT / "output" / "P跨期套利" / "P跨期套利全部组合.csv"
CONTRACT_HISTORY_CSV = (
    MARKET_DATA_DIR / "yp_lseg_full_contract_history.csv"
)
CONTINUOUS_HISTORY_CSV = (
    MARKET_DATA_DIR / "yp_lseg_continuous_history.csv"
)
OUTPUT_DIR = PROJECT_ROOT / "output" / "P跨期套利" / "开平仓分析"


def expiry_ordinal(contract: object, reference_year: int) -> int | None:
    match = re.search(r"(\d{3,4})$", str(contract).strip().upper())
    if not match:
        return None
    code = match.group(1)
    month = int(code[-2:])
    if not 1 <= month <= 12:
        return None
    if len(code) == 4:
        year = 2000 + int(code[:2])
    else:
        digit = int(code[0])
        decade = reference_year - reference_year % 10
        year = min(
            (decade - 10 + digit, decade + digit, decade + 10 + digit),
            key=lambda value: abs(value - reference_year),
        )
    return year * 12 + month


def parse_contract_month(contract: object) -> int | None:
    match = re.search(r"(\d{2})$", str(contract).strip().upper())
    if not match:
        return None
    month = int(match.group(1))
    return month if 1 <= month <= 12 else None


def normalize_trade(row: pd.Series) -> dict[str, object]:
    start = pd.to_datetime(row["组合开始"], errors="coerce")
    reference_year = int(start.year) if pd.notna(start) else 2020
    a_expiry = expiry_ordinal(row["A合约"], reference_year)
    b_expiry = expiry_ordinal(row["B合约"], reference_year)
    if a_expiry is None or b_expiry is None:
        near_prefix, far_prefix = "A", "B"
        month_gap = np.nan
        expiry_status = "无法判断"
    elif a_expiry < b_expiry:
        near_prefix, far_prefix = "A", "B"
        month_gap = b_expiry - a_expiry
        expiry_status = "正常"
    else:
        near_prefix, far_prefix = "B", "A"
        month_gap = a_expiry - b_expiry
        expiry_status = "正常"

    near_direction = str(row[f"{near_prefix}方向"]).upper()
    far_direction = str(row[f"{far_prefix}方向"]).upper()
    if near_direction == "LONG" and far_direction == "SHORT":
        structure = "多近月/空远月"
        position_sign = 1
    elif near_direction == "SHORT" and far_direction == "LONG":
        structure = "空近月/多远月"
        position_sign = -1
    else:
        structure = "方向异常"
        position_sign = 0

    near_contract = str(row[f"{near_prefix}合约"])
    far_contract = str(row[f"{far_prefix}合约"])
    near_month = parse_contract_month(near_contract)
    far_month = parse_contract_month(far_contract)
    month_pair = (
        f"{near_month:02d}-{far_month:02d}"
        if near_month is not None and far_month is not None
        else "无法判断"
    )
    end = pd.to_datetime(row["组合结束"], errors="coerce")

    return {
        "候选编号": row["候选编号"],
        "置信度": row["置信度"],
        "识别得分": pd.to_numeric(row["识别得分"], errors="coerce"),
        "交易结构": structure,
        "持仓方向系数": position_sign,
        "月份组合": month_pair,
        "近远月间隔(月)": month_gap,
        "期限判断": expiry_status,
        "组合开始": start,
        "组合结束": end,
        "持仓天数": (end - start).days + 1 if pd.notna(start) and pd.notna(end) else np.nan,
        "近月合约": near_contract,
        "近月方向": near_direction,
        "近月区间ID": row[f"{near_prefix}区间ID"],
        "近月实际开仓均价": pd.to_numeric(row[f"{near_prefix}开仓均价"], errors="coerce"),
        "近月手数": pd.to_numeric(row[f"{near_prefix}配对手数"], errors="coerce"),
        "近月净收益": pd.to_numeric(row[f"{near_prefix}净收益"], errors="coerce"),
        "远月合约": far_contract,
        "远月方向": far_direction,
        "远月区间ID": row[f"{far_prefix}区间ID"],
        "远月实际开仓均价": pd.to_numeric(row[f"{far_prefix}开仓均价"], errors="coerce"),
        "远月手数": pd.to_numeric(row[f"{far_prefix}配对手数"], errors="coerce"),
        "远月净收益": pd.to_numeric(row[f"{far_prefix}净收益"], errors="coerce"),
        "组合净收益(候选口径)": pd.to_numeric(row["组合净收益(可能重复)"], errors="coerce"),
        "名义金额平衡度": pd.to_numeric(row["名义金额平衡度"], errors="coerce"),
        "共同持仓天数": pd.to_numeric(row["重叠天数"], errors="coerce"),
        "数据质量": row["数据质量"],
    }


def load_trades() -> pd.DataFrame:
    raw = pd.read_csv(TRADES_CSV, encoding="utf-8-sig", low_memory=False)
    normalized = pd.DataFrame(normalize_trade(row) for _, row in raw.iterrows())
    normalized["样本阶段"] = np.where(
        normalized["组合开始"].dt.year <= 2024,
        "研究样本(2020-2024)",
        "样本外记录(2025)",
    )
    leg_ids = pd.concat(
        [normalized["近月区间ID"], normalized["远月区间ID"]], ignore_index=True
    ).astype(str)
    usage = leg_ids.value_counts()
    normalized["近月腿候选使用次数"] = normalized["近月区间ID"].astype(str).map(usage)
    normalized["远月腿候选使用次数"] = normalized["远月区间ID"].astype(str).map(usage)
    normalized["存在共享持仓腿"] = (
        normalized[["近月腿候选使用次数", "远月腿候选使用次数"]].max(axis=1) > 1
    )
    ranked = normalized.sort_values(
        ["识别得分", "名义金额平衡度", "共同持仓天数", "候选编号"],
        ascending=[False, False, False, True],
    )
    used_legs: set[str] = set()
    independent_indexes: list[int] = []
    for index, row in ranked.iterrows():
        near_id = str(row["近月区间ID"])
        far_id = str(row["远月区间ID"])
        if near_id in used_legs or far_id in used_legs:
            continue
        independent_indexes.append(index)
        used_legs.update((near_id, far_id))
    normalized["保守独立配对"] = normalized.index.isin(independent_indexes)
    return normalized


def load_contract_history() -> pd.DataFrame:
    history = pd.read_csv(CONTRACT_HISTORY_CSV, low_memory=False)
    history = history.loc[history["product"].eq("P")].copy()
    history["date"] = pd.to_datetime(history["date"], errors="coerce")
    for column in ["open", "high", "low", "settle", "last", "volume", "open_interest"]:
        history[column] = pd.to_numeric(history[column], errors="coerce")
    history["price"] = history["settle"].combine_first(history["last"])
    return (
        history.dropna(subset=["contract", "date", "price"])
        .drop_duplicates(["contract", "date"], keep="last")
        .sort_values(["contract", "date"])
    )


def load_continuous_history() -> pd.DataFrame:
    continuous = pd.read_csv(CONTINUOUS_HISTORY_CSV, low_memory=False)
    continuous = continuous.loc[continuous["品种"].eq("P")].copy()
    continuous["日期"] = pd.to_datetime(continuous["日期"], errors="coerce")
    for column in ["SETTLE", "TRDPRC_1"]:
        continuous[column] = pd.to_numeric(continuous[column], errors="coerce")
    continuous["主连价格"] = continuous["SETTLE"].combine_first(continuous["TRDPRC_1"])
    continuous = (
        continuous.dropna(subset=["日期", "主连价格"])
        .drop_duplicates("日期", keep="last")
        .sort_values("日期")
    )
    continuous["主连5日收益"] = continuous["主连价格"].pct_change(5)
    continuous["主连20日收益"] = continuous["主连价格"].pct_change(20)
    continuous["主连60日收益"] = continuous["主连价格"].pct_change(60)
    continuous["主连20日均线"] = continuous["主连价格"].rolling(20, min_periods=15).mean()
    continuous["主连60日均线"] = continuous["主连价格"].rolling(60, min_periods=40).mean()
    continuous["主连均线趋势"] = continuous["主连20日均线"] / continuous["主连60日均线"] - 1
    return continuous[
        [
            "日期",
            "主连价格",
            "主连5日收益",
            "主连20日收益",
            "主连60日收益",
            "主连均线趋势",
        ]
    ].rename(columns={"日期": "date"})


def rolling_percentile(values: pd.Series, window: int = 60, minimum: int = 40) -> pd.Series:
    return values.rolling(window, min_periods=minimum).apply(
        lambda sample: pd.Series(sample).rank(pct=True).iloc[-1], raw=False
    )


def build_pair_series(
    history_by_contract: dict[str, pd.DataFrame],
    continuous: pd.DataFrame,
    near_contract: str,
    far_contract: str,
) -> pd.DataFrame:
    near = history_by_contract.get(near_contract)
    far = history_by_contract.get(far_contract)
    if near is None or far is None:
        return pd.DataFrame()

    near_columns = {
        "price": "近月结算价",
        "volume": "近月成交量",
        "open_interest": "近月持仓量",
    }
    far_columns = {
        "price": "远月结算价",
        "volume": "远月成交量",
        "open_interest": "远月持仓量",
    }
    pair = near[["date", *near_columns]].rename(columns=near_columns).merge(
        far[["date", *far_columns]].rename(columns=far_columns), on="date", how="inner"
    )
    pair = pair.sort_values("date").drop_duplicates("date", keep="last")
    if pair.empty:
        return pair

    pair["价差"] = pair["近月结算价"] - pair["远月结算价"]
    pair["价差比例"] = pair["近月结算价"] / pair["远月结算价"] - 1
    pair["对数价差"] = np.log(pair["近月结算价"] / pair["远月结算价"])
    for window, minimum in [(20, 15), (60, 40)]:
        mean = pair["价差比例"].rolling(window, min_periods=minimum).mean()
        std = pair["价差比例"].rolling(window, min_periods=minimum).std(ddof=1)
        pair[f"Z{window}"] = (pair["价差比例"] - mean) / std.replace(0, np.nan)
    pair["价差60日分位"] = rolling_percentile(pair["价差比例"])
    pair["价差5日变化"] = pair["价差比例"] - pair["价差比例"].shift(5)
    pair["价差20日变化"] = pair["价差比例"] - pair["价差比例"].shift(20)
    total_volume = pair["近月成交量"] + pair["远月成交量"]
    total_oi = pair["近月持仓量"] + pair["远月持仓量"]
    pair["近月成交量占比"] = pair["近月成交量"] / total_volume.replace(0, np.nan)
    pair["近月持仓量占比"] = pair["近月持仓量"] / total_oi.replace(0, np.nan)
    pair = pair.merge(continuous, on="date", how="left")
    return pair


def prior_row(series: pd.DataFrame, event_date: pd.Timestamp) -> pd.Series | None:
    available = series.loc[series["date"] < event_date]
    if available.empty:
        return None
    return available.iloc[-1]


def safe_value(row: pd.Series | None, column: str) -> object:
    if row is None or column not in row:
        return np.nan
    return row[column]


def enrich_events(
    trades: pd.DataFrame,
    history: pd.DataFrame,
    continuous: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[tuple[str, str], pd.DataFrame]]:
    history_by_contract = {
        contract: frame.copy()
        for contract, frame in history.groupby("contract", sort=False)
    }
    pair_cache: dict[tuple[str, str], pd.DataFrame] = {}
    records: list[dict[str, object]] = []

    feature_columns = [
        "date",
        "近月结算价",
        "远月结算价",
        "价差",
        "价差比例",
        "Z20",
        "Z60",
        "价差60日分位",
        "价差5日变化",
        "价差20日变化",
        "近月成交量占比",
        "近月持仓量占比",
        "主连价格",
        "主连5日收益",
        "主连20日收益",
        "主连60日收益",
        "主连均线趋势",
    ]

    for _, trade in trades.iterrows():
        key = (trade["近月合约"], trade["远月合约"])
        if key not in pair_cache:
            pair_cache[key] = build_pair_series(
                history_by_contract, continuous, key[0], key[1]
            )
        series = pair_cache[key]
        entry = prior_row(series, trade["组合开始"]) if not series.empty else None
        exit_row = prior_row(series, trade["组合结束"]) if not series.empty else None
        record = trade.to_dict()

        for column in feature_columns:
            entry_name = "开仓信号日" if column == "date" else f"开仓前_{column}"
            exit_name = "平仓信号日" if column == "date" else f"平仓前_{column}"
            record[entry_name] = safe_value(entry, column)
            record[exit_name] = safe_value(exit_row, column)

        sign = trade["持仓方向系数"]
        entry_z60 = record["开仓前_Z60"]
        exit_z60 = record["平仓前_Z60"]
        entry_percentile = record["开仓前_价差60日分位"]
        entry_spread = record["开仓前_价差比例"]
        exit_spread = record["平仓前_价差比例"]
        entry_spread_points = record["开仓前_价差"]
        exit_spread_points = record["平仓前_价差"]
        record["开仓方向化Z60"] = sign * entry_z60 if pd.notna(entry_z60) else np.nan
        record["平仓方向化Z60"] = sign * exit_z60 if pd.notna(exit_z60) else np.nan
        record["开仓逆向极端分数"] = (
            1 - entry_percentile
            if sign == 1 and pd.notna(entry_percentile)
            else entry_percentile
            if sign == -1 and pd.notna(entry_percentile)
            else np.nan
        )
        record["持仓期方向化价差变化"] = (
            sign * (exit_spread - entry_spread)
            if pd.notna(entry_spread) and pd.notna(exit_spread)
            else np.nan
        )
        record["持仓期方向化价差点数变化"] = (
            sign * (exit_spread_points - entry_spread_points)
            if pd.notna(entry_spread_points) and pd.notna(exit_spread_points)
            else np.nan
        )
        record["Z60向中枢收敛幅度"] = (
            abs(entry_z60) - abs(exit_z60)
            if pd.notna(entry_z60) and pd.notna(exit_z60)
            else np.nan
        )
        record["开仓符合均值回归极端"] = bool(
            pd.notna(record["开仓方向化Z60"]) and record["开仓方向化Z60"] <= -1
        )
        record["平仓回到Z60中枢附近"] = bool(
            pd.notna(exit_z60) and abs(exit_z60) <= 0.5
        )
        if pd.isna(entry_z60) or pd.isna(exit_z60):
            exit_proxy = "行情特征不足"
        elif abs(entry_z60) >= 0.8 and abs(exit_z60) <= 0.5:
            exit_proxy = "可能均值回归至中枢"
        elif record["持仓期方向化价差变化"] > 0:
            exit_proxy = "可能有利变动后止盈"
        elif abs(exit_z60) > abs(entry_z60):
            exit_proxy = "可能不利扩大后退出"
        else:
            exit_proxy = "时间或其他条件"
        record["平仓原因代理分类"] = exit_proxy
        records.append(record)

    enriched = pd.DataFrame(records)
    return enriched, pair_cache


def weighted_baseline(
    events: pd.DataFrame, pair_cache: dict[tuple[str, str], pd.DataFrame]
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    unique_methods = events[["近月合约", "远月合约", "交易结构", "持仓方向系数"]].drop_duplicates()
    for _, method in unique_methods.iterrows():
        key = (method["近月合约"], method["远月合约"])
        series = pair_cache.get(key)
        if series is None or series.empty:
            continue
        sample = series.dropna(subset=["Z60", "价差60日分位"]).copy()
        if sample.empty:
            continue
        sign = method["持仓方向系数"]
        sample["交易结构"] = method["交易结构"]
        sample["近月合约"] = method["近月合约"]
        sample["远月合约"] = method["远月合约"]
        sample["方向化Z60"] = sign * sample["Z60"]
        sample["方向化5日变化"] = sign * sample["价差5日变化"]
        sample["方向化20日变化"] = sign * sample["价差20日变化"]
        sample["逆向极端分数"] = np.where(
            sign == 1,
            1 - sample["价差60日分位"],
            sample["价差60日分位"],
        )
        sample["样本阶段"] = np.where(
            sample["date"].dt.year <= 2024,
            "研究样本(2020-2024)",
            "样本外记录(2025)",
        )
        frames.append(sample)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def summarize_events(events: pd.DataFrame) -> pd.DataFrame:
    rows = []
    scopes = [("全部候选", events), ("保守独立配对", events.loc[events["保守独立配对"]])]
    for scope_name, scope in scopes:
        for keys, group in scope.groupby(["样本阶段", "交易结构"], dropna=False):
            valid = group.dropna(subset=["开仓前_Z60"])
            rows.append({
                "统计口径": scope_name,
                "样本阶段": keys[0],
                "交易结构": keys[1],
                "候选记录数": len(group),
                "有60日特征记录数": len(valid),
                "特征覆盖率": len(valid) / len(group) if len(group) else np.nan,
                "开仓Z60中位数": valid["开仓前_Z60"].median(),
                "开仓方向化Z60中位数": valid["开仓方向化Z60"].median(),
                "开仓逆向极端分数中位数": valid["开仓逆向极端分数"].median(),
                "符合方向化Z60<=-1比例": valid["开仓符合均值回归极端"].mean(),
                "开仓前5日方向化变化中位数": (
                    valid["持仓方向系数"] * valid["开仓前_价差5日变化"]
                ).median(),
                "持仓天数中位数": group["持仓天数"].median(),
                "方向化价差点数变化中位数": group["持仓期方向化价差点数变化"].median(),
                "平仓Z60中位数": group["平仓前_Z60"].median(),
                "平仓位于|Z60|<=0.5比例": group["平仓回到Z60中枢附近"].mean(),
                "Z60向中枢收敛比例": (group["Z60向中枢收敛幅度"] > 0).mean(),
                "候选口径胜率": (group["组合净收益(候选口径)"] > 0).mean(),
                "候选口径净收益合计": group["组合净收益(候选口径)"].sum(),
                "共享持仓腿记录比例": group["存在共享持仓腿"].mean(),
            })
    return pd.DataFrame(rows)


def threshold_grid(events: pd.DataFrame, baseline: pd.DataFrame) -> pd.DataFrame:
    thresholds = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
    rows = []
    scopes = [("全部候选", events), ("保守独立配对", events.loc[events["保守独立配对"]])]
    for scope_name, scope in scopes:
      for phase, phase_events in scope.groupby("样本阶段"):
        for structure, group in phase_events.groupby("交易结构"):
            event_values = group["开仓方向化Z60"].dropna()
            base_values = baseline.loc[
                baseline["交易结构"].eq(structure)
                & baseline["样本阶段"].eq(phase),
                "方向化Z60",
            ].dropna()
            for threshold in thresholds:
                event_rate = (event_values <= -threshold).mean() if len(event_values) else np.nan
                base_rate = (base_values <= -threshold).mean() if len(base_values) else np.nan
                matched = group.loc[group["开仓方向化Z60"] <= -threshold]
                rows.append(
                    {
                        "统计口径": scope_name,
                        "样本阶段": phase,
                        "交易结构": structure,
                        "方向化Z60阈值": -threshold,
                        "符合记录数": len(matched),
                        "交易记录命中率": event_rate,
                        "普通交易日出现率": base_rate,
                        "相对富集倍数": event_rate / base_rate if base_rate and not math.isnan(base_rate) else np.nan,
                        "命中记录候选口径胜率": (
                            (matched["组合净收益(候选口径)"] > 0).mean()
                            if len(matched)
                            else np.nan
                        ),
                        "命中记录候选口径净收益": matched["组合净收益(候选口径)"].sum(),
                    }
                )
    return pd.DataFrame(rows)


def feature_comparison(events: pd.DataFrame, baseline: pd.DataFrame) -> pd.DataFrame:
    rows = []
    scopes = [("全部候选", events), ("保守独立配对", events.loc[events["保守独立配对"]])]
    for scope_name, scope in scopes:
      for phase, phase_events in scope.groupby("样本阶段"):
        for structure, group in phase_events.groupby("交易结构"):
            base = baseline.loc[
                baseline["样本阶段"].eq(phase)
                & baseline["交易结构"].eq(structure)
            ]
            valid = group.dropna(subset=["开仓前_Z60"])
            event_dir5 = valid["持仓方向系数"] * valid["开仓前_价差5日变化"]
            event_dir20 = valid["持仓方向系数"] * valid["开仓前_价差20日变化"]
            rows.append(
                {
                    "统计口径": scope_name,
                    "样本阶段": phase,
                    "交易结构": structure,
                    "有效开仓记录数": len(valid),
                    "普通交易日样本数": len(base),
                    "开仓方向化Z60中位数": valid["开仓方向化Z60"].median(),
                    "普通日方向化Z60中位数": base["方向化Z60"].median(),
                    "开仓方向化Z60<=-1比例": (valid["开仓方向化Z60"] <= -1).mean(),
                    "普通日方向化Z60<=-1比例": (base["方向化Z60"] <= -1).mean(),
                    "Z60极端富集倍数": (
                        (valid["开仓方向化Z60"] <= -1).mean()
                        / (base["方向化Z60"] <= -1).mean()
                        if len(base) and (base["方向化Z60"] <= -1).mean() > 0
                        else np.nan
                    ),
                    "开仓逆向极端分数中位数": valid["开仓逆向极端分数"].median(),
                    "普通日逆向极端分数中位数": base["逆向极端分数"].median(),
                    "开仓方向化5日变化中位数": event_dir5.median(),
                    "普通日方向化5日变化中位数": base["方向化5日变化"].median(),
                    "开仓方向化20日变化中位数": event_dir20.median(),
                    "普通日方向化20日变化中位数": base["方向化20日变化"].median(),
                    "开仓主连20日收益中位数": valid["开仓前_主连20日收益"].median(),
                    "普通日主连20日收益中位数": base["主连20日收益"].median(),
                    "开仓主连均线趋势中位数": valid["开仓前_主连均线趋势"].median(),
                    "普通日主连均线趋势中位数": base["主连均线趋势"].median(),
                    "开仓近月持仓量占比中位数": valid["开仓前_近月持仓量占比"].median(),
                    "普通日近月持仓量占比中位数": base["近月持仓量占比"].median(),
                }
            )
    return pd.DataFrame(rows)


def month_pair_summary(events: pd.DataFrame) -> pd.DataFrame:
    rows = []
    scopes = [("全部候选", events), ("保守独立配对", events.loc[events["保守独立配对"]])]
    for scope_name, scope in scopes:
      for keys, group in scope.groupby(
          ["样本阶段", "交易结构", "月份组合"], dropna=False
      ):
        valid = group.dropna(subset=["开仓前_Z60"])
        rows.append({
                "统计口径": scope_name,
                "样本阶段": keys[0],
                "交易结构": keys[1],
                "月份组合": keys[2],
                "候选记录数": len(group),
                "有效开仓记录数": len(valid),
                "开仓Z60中位数": valid["开仓前_Z60"].median(),
                "开仓方向化Z60中位数": valid["开仓方向化Z60"].median(),
                "符合方向化Z60<=-1比例": (valid["开仓方向化Z60"] <= -1).mean(),
                "持仓天数中位数": group["持仓天数"].median(),
                "候选口径胜率": (group["组合净收益(候选口径)"] > 0).mean(),
                "共享持仓腿比例": group["存在共享持仓腿"].mean(),
            })
    result = pd.DataFrame(rows)
    return result.sort_values(
        ["候选记录数", "样本阶段", "交易结构"], ascending=[False, True, True]
    )


def exit_summary(events: pd.DataFrame) -> pd.DataFrame:
    frames = []
    scopes = [("全部候选", events), ("保守独立配对", events.loc[events["保守独立配对"]])]
    for scope_name, scope in scopes:
      summary = (
        scope.groupby(["样本阶段", "交易结构", "平仓原因代理分类"], dropna=False)
        .agg(
            记录数=("候选编号", "count"),
            持仓天数中位数=("持仓天数", "median"),
            开仓Z60中位数=("开仓前_Z60", "median"),
            平仓Z60中位数=("平仓前_Z60", "median"),
            候选口径胜率=("组合净收益(候选口径)", lambda x: (x > 0).mean()),
            候选口径净收益=("组合净收益(候选口径)", "sum"),
        )
        .reset_index()
      )
      summary.insert(0, "统计口径", scope_name)
      totals = summary.groupby(["样本阶段", "交易结构"])["记录数"].transform("sum")
      summary.insert(5, "组内占比", summary["记录数"] / totals)
      frames.append(summary)
    result = pd.concat(frames, ignore_index=True)
    return result.sort_values(
        ["统计口径", "样本阶段", "交易结构", "记录数"],
        ascending=[True, True, True, False],
    )


def data_quality_summary(events: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"检查项": "P跨期候选记录", "结果": len(events), "说明": "来自PostgreSQL筛选结果"},
            {
                "检查项": "涉及合约数量",
                "结果": len(set(events["近月合约"]) | set(events["远月合约"])),
                "说明": "全部合约均在LSEG历史文件中存在",
            },
            {
                "检查项": "开仓Z60有效记录",
                "结果": int(events["开仓前_Z60"].notna().sum()),
                "说明": "需要至少40个共同交易日形成60日窗口",
            },
            {
                "检查项": "共享持仓腿候选",
                "结果": int(events["存在共享持仓腿"].sum()),
                "说明": "候选组合之间可能重复使用同一腿，收益不可直接当作独立交易累计",
            },
            {
                "检查项": "保守独立配对",
                "结果": int(events["保守独立配对"].sum()),
                "说明": "按识别得分、资金平衡度和共同持仓天数选择，同一腿只使用一次且未使用收益筛选",
            },
            {
                "检查项": "信号日期口径",
                "结果": "开平仓前一共同交易日",
                "说明": "避免把当日结算价当作事前信号",
            },
        ]
    )


def json_safe(value: object) -> object:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) else float(value)
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    return value


def main() -> None:
    trades = load_trades()
    history = load_contract_history()
    continuous = load_continuous_history()
    events, pair_cache = enrich_events(trades, history, continuous)
    baseline = weighted_baseline(events, pair_cache)
    event_summary = summarize_events(events)
    thresholds = threshold_grid(events, baseline)
    feature_compare = feature_comparison(events, baseline)
    month_pairs = month_pair_summary(events)
    exits = exit_summary(events)
    quality = data_quality_summary(events)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    outputs = {
        "features": OUTPUT_DIR / "P跨期逐笔开平仓特征.csv",
        "event_summary": OUTPUT_DIR / "P跨期开平仓汇总.csv",
        "thresholds": OUTPUT_DIR / "P跨期开仓阈值比较.csv",
        "feature_compare": OUTPUT_DIR / "P跨期开仓与普通日比较.csv",
        "month_pairs": OUTPUT_DIR / "P跨期月份组合特征.csv",
        "exits": OUTPUT_DIR / "P跨期平仓原因代理.csv",
        "quality": OUTPUT_DIR / "P跨期数据检查.csv",
        "summary_json": OUTPUT_DIR / "P跨期分析结果.json",
    }
    events.to_csv(outputs["features"], index=False, encoding="utf-8-sig", date_format="%Y-%m-%d")
    event_summary.to_csv(outputs["event_summary"], index=False, encoding="utf-8-sig")
    thresholds.to_csv(outputs["thresholds"], index=False, encoding="utf-8-sig")
    feature_compare.to_csv(outputs["feature_compare"], index=False, encoding="utf-8-sig")
    month_pairs.to_csv(outputs["month_pairs"], index=False, encoding="utf-8-sig")
    exits.to_csv(outputs["exits"], index=False, encoding="utf-8-sig")
    quality.to_csv(outputs["quality"], index=False, encoding="utf-8-sig")

    summary_payload = {
        "source": {
            "trades": str(TRADES_CSV),
            "contract_history": str(CONTRACT_HISTORY_CSV),
            "continuous_history": str(CONTINUOUS_HISTORY_CSV),
        },
        "coverage": {
            "events": len(events),
            "valid_entry_z60": int(events["开仓前_Z60"].notna().sum()),
            "shared_leg_events": int(events["存在共享持仓腿"].sum()),
            "history_start": history["date"].min(),
            "history_end": history["date"].max(),
        },
        "event_summary": event_summary.to_dict("records"),
        "exit_summary": exits.to_dict("records"),
    }
    with outputs["summary_json"].open("w", encoding="utf-8") as handle:
        json.dump(summary_payload, handle, ensure_ascii=False, indent=2, default=json_safe)

    print(f"逐笔候选：{len(events)}")
    print(f"开仓Z60有效：{events['开仓前_Z60'].notna().sum()}")
    print(f"共享持仓腿候选：{events['存在共享持仓腿'].sum()}")
    print(event_summary.to_string(index=False))
    print(f"输出目录：{OUTPUT_DIR}")


if __name__ == "__main__":
    main()
