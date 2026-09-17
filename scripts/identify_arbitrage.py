#!/usr/bin/env python3
"""从已完成交易中识别跨期、跨品种及跨日期的对冲套利候选组合。"""

from __future__ import annotations

from project_config import PROJECT_ROOT, RAW_STATEMENTS_DIR, MARKET_DATA_DIR

import argparse
import csv
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


RELATED_PAIRS = {
    tuple(sorted(pair))
    for pair in [
        ("Y", "P"), ("Y", "OI"), ("P", "OI"), ("A", "M"), ("A", "Y"),
        ("M", "RM"), ("C", "CS"), ("C", "JD"), ("CS", "JD"),
        ("RB", "HC"), ("I", "RB"), ("I", "HC"), ("J", "JM"),
        ("JM", "I"), ("J", "I"), ("MA", "PP"), ("MA", "TA"),
        ("PP", "L"), ("L", "V"), ("PP", "V"), ("TA", "EG"),
        ("TA", "PF"), ("EG", "PF"), ("SA", "FG"), ("UR", "SA"),
        ("FU", "BU"), ("FU", "LU"), ("BU", "LU"), ("SC", "FU"),
        ("SC", "BU"), ("PG", "L"), ("PG", "PP"), ("CU", "AL"),
        ("CU", "ZN"), ("AL", "ZN"), ("NI", "SS"), ("AU", "AG"),
        ("IF", "IH"), ("IF", "IC"), ("IH", "IC"), ("IC", "IM"),
    ]
}


def number(value, default=0.0):
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def parse_dt(value):
    return datetime.fromisoformat(value)


def read_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_episodes(rows):
    grouped = defaultdict(list)
    for row in rows:
        if not row.get("entry_datetime") or not row.get("exit_datetime"):
            continue
        grouped[(row.get("account_id", ""), row.get("contract", ""), row.get("product", ""), row.get("direction", ""))].append({
            **row,
            "entry_dt": parse_dt(row["entry_datetime"]),
            "exit_dt": parse_dt(row["exit_datetime"]),
        })

    episodes = []
    serial = 0
    for (account, contract, product, direction), items in sorted(grouped.items()):
        items.sort(key=lambda row: (row["entry_dt"], row["exit_dt"], row.get("completed_trade_id", "")))
        blocks = []
        current = []
        current_end = None
        for row in items:
            if current and row["entry_dt"] > current_end:
                blocks.append(current)
                current = []
                current_end = None
            current.append(row)
            current_end = row["exit_dt"] if current_end is None else max(current_end, row["exit_dt"])
        if current:
            blocks.append(current)

        for block in blocks:
            serial += 1
            start = min(row["entry_dt"] for row in block)
            end = max(row["exit_dt"] for row in block)
            quantity = sum(number(row.get("quantity")) for row in block)
            entry_value = sum(number(row.get("quantity")) * number(row.get("entry_price")) for row in block)
            exit_value = sum(number(row.get("quantity")) * number(row.get("exit_price")) for row in block)
            entry_notional = sum(
                number(row.get("quantity")) * number(row.get("entry_price")) * number(row.get("contract_multiplier"))
                for row in block
            )
            exact_count = sum(row.get("holding_time_quality") == "EXACT" for row in block)
            episodes.append({
                "episode_id": f"LEG{serial:06d}",
                "account_id": account,
                "contract": contract,
                "product": product,
                "direction": direction,
                "start_dt": start,
                "end_dt": end,
                "start_date": start.date().isoformat(),
                "end_date": end.date().isoformat(),
                "duration_days": (end.date() - start.date()).days + 1,
                "matched_quantity": quantity,
                "avg_entry_price": entry_value / quantity if quantity else 0.0,
                "avg_exit_price": exit_value / quantity if quantity else 0.0,
                "entry_notional": entry_notional,
                "net_pnl": sum(number(row.get("net_pnl_after_fees")) for row in block),
                "fees": sum(number(row.get("total_fee")) for row in block),
                "completed_rows": len(block),
                "entry_order_count": len({row.get("entry_trade_id", "") for row in block}),
                "exit_order_count": len({row.get("exit_trade_id", "") for row in block}),
                "data_quality": "精确时间" if exact_count == len(block) else "交易日级" if exact_count == 0 else "混合",
            })
    return sorted(episodes, key=lambda row: (row["start_dt"], row["end_dt"], row["contract"], row["direction"]))


def expiry_ordinal(contract, reference_year):
    match = re.search(r"(\d{3,4})$", contract or "")
    if not match:
        return None
    code = match.group(1)
    month = int(code[-2:])
    if not 1 <= month <= 12:
        return None
    if len(code) == 4:
        return 2000 + int(code[:2]) * 12 + month
    digit = int(code[0])
    decade = reference_year - reference_year % 10
    year = min((decade - 10 + digit, decade + digit, decade + 10 + digit), key=lambda y: abs(y - reference_year))
    return year * 12 + month


def gap_points(days, schedule):
    for limit, points in schedule:
        if days <= limit:
            return points
    return 0


def ratio_points(ratio):
    if ratio >= 0.80:
        return 18
    if ratio >= 0.60:
        return 14
    if ratio >= 0.40:
        return 8
    if ratio >= 0.25:
        return 3
    return 0


def overlap_points(ratio):
    if ratio >= 0.80:
        return 15
    if ratio >= 0.50:
        return 10
    if ratio >= 0.25:
        return 5
    return 0


def pair_type(a, b):
    if a["product"] == b["product"]:
        return "跨期套利", 30, "同品种、不同合约、方向相反且持仓重叠"
    key = tuple(sorted((a["product"], b["product"])))
    if key in RELATED_PAIRS:
        return "相关品种套利", 22, "预设相关品种、方向相反且持仓重叠"
    return "跨品种对冲候选", 0, "非预设相关品种；依靠开平协调、持仓重叠和名义金额平衡识别"


def term_structure(a, b):
    ref_year = min(a["start_dt"].year, b["start_dt"].year)
    ea = expiry_ordinal(a["contract"], ref_year)
    eb = expiry_ordinal(b["contract"], ref_year)
    if ea is None or eb is None:
        return "期限无法判断"
    if ea == eb:
        return "合约月份相同"
    return "A远月/B近月" if ea > eb else "A近月/B远月"


def build_candidates(episodes, minimum_score):
    by_account = defaultdict(list)
    for episode in episodes:
        by_account[episode["account_id"]].append(episode)

    candidates = []
    for account, items in by_account.items():
        items.sort(key=lambda row: row["start_dt"])
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                if b["start_dt"] > a["end_dt"]:
                    break
                if a["direction"] == b["direction"] or a["contract"] == b["contract"]:
                    continue
                overlap_start = max(a["start_dt"], b["start_dt"])
                overlap_end = min(a["end_dt"], b["end_dt"])
                if overlap_end < overlap_start:
                    continue

                overlap_days = (overlap_end.date() - overlap_start.date()).days + 1
                shorter_duration = min(a["duration_days"], b["duration_days"])
                overlap_ratio = overlap_days / shorter_duration if shorter_duration else 0.0
                entry_gap = abs((a["start_dt"].date() - b["start_dt"].date()).days)
                exit_gap = abs((a["end_dt"].date() - b["end_dt"].date()).days)
                larger_notional = max(a["entry_notional"], b["entry_notional"])
                balance = min(a["entry_notional"], b["entry_notional"]) / larger_notional if larger_notional else 0.0
                kind, relation_score, reason = pair_type(a, b)
                score = 8 + relation_score
                score += gap_points(entry_gap, [(0, 20), (1, 17), (3, 12), (7, 7), (15, 3)])
                score += gap_points(exit_gap, [(0, 14), (1, 11), (3, 8), (7, 5), (15, 2)])
                score += overlap_points(overlap_ratio)
                score += ratio_points(balance)
                if a["data_quality"] == b["data_quality"] == "精确时间":
                    score += 3
                score = min(score, 100)
                if score < minimum_score:
                    continue

                confidence = "高" if score >= 80 else "中" if score >= 65 else "低"
                if entry_gap > 0:
                    reason += f"；两腿跨{entry_gap}天建立"
                if exit_gap > 0:
                    reason += f"；两腿相差{exit_gap}天结束"
                candidates.append({
                    "account_id": account,
                    "confidence": confidence,
                    "score": score,
                    "arbitrage_type": kind,
                    "product_pair": "/".join(sorted((a["product"], b["product"]))),
                    "term_structure": term_structure(a, b),
                    "combo_start": min(a["start_dt"], b["start_dt"]).date().isoformat(),
                    "combo_end": max(a["end_dt"], b["end_dt"]).date().isoformat(),
                    "overlap_start": overlap_start.date().isoformat(),
                    "overlap_end": overlap_end.date().isoformat(),
                    "overlap_days": overlap_days,
                    "overlap_ratio": overlap_ratio,
                    "entry_gap_days": entry_gap,
                    "exit_gap_days": exit_gap,
                    "notional_balance": balance,
                    "a": a,
                    "b": b,
                    "combined_net_pnl": a["net_pnl"] + b["net_pnl"],
                    "data_quality": f"A:{a['data_quality']} / B:{b['data_quality']}",
                    "reason": reason,
                })

    candidates.sort(key=lambda row: (-row["score"], row["combo_start"], row["product_pair"], row["a"]["contract"], row["b"]["contract"]))
    for index, row in enumerate(candidates, 1):
        row["candidate_id"] = f"ARB{index:06d}"
    return candidates


def flatten_candidate(row):
    a, b = row["a"], row["b"]
    return {
        "候选编号": row["candidate_id"], "置信度": row["confidence"], "识别得分": row["score"],
        "套利类型": row["arbitrage_type"], "品种组合": row["product_pair"], "期限结构": row["term_structure"],
        "组合开始": row["combo_start"], "组合结束": row["combo_end"],
        "重叠开始": row["overlap_start"], "重叠结束": row["overlap_end"], "重叠天数": row["overlap_days"],
        "较短腿重叠率": row["overlap_ratio"], "开仓相差天数": row["entry_gap_days"], "平仓相差天数": row["exit_gap_days"],
        "名义金额平衡度": row["notional_balance"],
        "A品种": a["product"], "A合约": a["contract"], "A方向": a["direction"], "A开始": a["start_date"], "A结束": a["end_date"],
        "A配对手数": a["matched_quantity"], "A开仓均价": a["avg_entry_price"], "A名义金额": a["entry_notional"], "A净收益": a["net_pnl"],
        "B品种": b["product"], "B合约": b["contract"], "B方向": b["direction"], "B开始": b["start_date"], "B结束": b["end_date"],
        "B配对手数": b["matched_quantity"], "B开仓均价": b["avg_entry_price"], "B名义金额": b["entry_notional"], "B净收益": b["net_pnl"],
        "组合净收益(可能重复)": row["combined_net_pnl"], "A区间ID": a["episode_id"], "B区间ID": b["episode_id"],
        "数据质量": row["data_quality"], "识别依据": row["reason"],
    }


def select_priority_pairs(candidates):
    used = set()
    selected = []
    for row in candidates:
        if row["confidence"] == "低":
            continue
        ids = {row["a"]["episode_id"], row["b"]["episode_id"]}
        if ids & used:
            continue
        used.update(ids)
        selected.append(row)
    return selected


def summarize_pairs(candidates):
    groups = defaultdict(list)
    for row in candidates:
        groups[(row["arbitrage_type"], row["product_pair"])].append(row)
    result = []
    for (kind, pair), rows in groups.items():
        counts = Counter(row["confidence"] for row in rows)
        result.append({
            "套利类型": kind, "品种组合": pair, "候选数量": len(rows),
            "高置信度": counts["高"], "中置信度": counts["中"], "低置信度": counts["低"],
            "平均得分": sum(row["score"] for row in rows) / len(rows),
            "平均名义金额平衡度": sum(row["notional_balance"] for row in rows) / len(rows),
            "最早开始": min(row["combo_start"] for row in rows), "最晚结束": max(row["combo_end"] for row in rows),
        })
    return sorted(result, key=lambda row: (-row["高置信度"], -row["中置信度"], -row["候选数量"], row["品种组合"]))


def main():
    parser = argparse.ArgumentParser(description="识别跨期、跨品种和跨日期对冲套利候选")
    parser.add_argument("--completed-trades", type=Path, default=PROJECT_ROOT / "output" / "completed_trades.csv")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "output")
    parser.add_argument("--minimum-score", type=int, default=48)
    args = parser.parse_args()

    completed = read_csv(args.completed_trades)
    episodes = build_episodes(completed)
    candidates = build_candidates(episodes, args.minimum_score)
    priority = select_priority_pairs(candidates)
    flat_candidates = [flatten_candidate(row) for row in candidates]
    flat_priority = [flatten_candidate(row) for row in priority]
    pair_summary = summarize_pairs(candidates)

    candidate_fields = list(flat_candidates[0]) if flat_candidates else [
        "候选编号", "置信度", "识别得分", "套利类型", "品种组合", "期限结构", "组合开始", "组合结束",
        "重叠开始", "重叠结束", "重叠天数", "较短腿重叠率", "开仓相差天数", "平仓相差天数", "名义金额平衡度",
        "A品种", "A合约", "A方向", "A开始", "A结束", "A配对手数", "A开仓均价", "A名义金额", "A净收益",
        "B品种", "B合约", "B方向", "B开始", "B结束", "B配对手数", "B开仓均价", "B名义金额", "B净收益",
        "组合净收益(可能重复)", "A区间ID", "B区间ID", "数据质量", "识别依据",
    ]
    episode_rows = [{
        "区间ID": row["episode_id"], "账户": row["account_id"], "品种": row["product"], "合约": row["contract"],
        "方向": row["direction"], "开始日期": row["start_date"], "结束日期": row["end_date"], "持续天数": row["duration_days"],
        "配对手数": row["matched_quantity"], "开仓均价": row["avg_entry_price"], "平仓均价": row["avg_exit_price"],
        "开仓名义金额": row["entry_notional"], "净收益": row["net_pnl"], "费用": row["fees"],
        "已完成明细行": row["completed_rows"], "开仓委托数": row["entry_order_count"], "平仓委托数": row["exit_order_count"],
        "数据质量": row["data_quality"],
    } for row in episodes]

    episode_ids = {row["episode_id"] for row in episodes}
    confidence_counts = Counter(row["confidence"] for row in candidates)
    completed_net = sum(number(row.get("net_pnl_after_fees")) for row in completed)
    episode_net = sum(row["net_pnl"] for row in episodes)
    checks = [
        {"检查项": "持仓区间净收益与已完成交易一致", "实际值": episode_net, "预期值": completed_net, "差异": episode_net - completed_net, "状态": "OK" if math.isclose(episode_net, completed_net, abs_tol=0.01) else "ERROR", "说明": "区间是由 completed_trades.csv 合并而成"},
        {"检查项": "候选两腿方向相反", "实际值": sum(row["a"]["direction"] == row["b"]["direction"] for row in candidates), "预期值": 0, "差异": sum(row["a"]["direction"] == row["b"]["direction"] for row in candidates), "状态": "OK" if all(row["a"]["direction"] != row["b"]["direction"] for row in candidates) else "ERROR", "说明": "套利候选必须一多一空"},
        {"检查项": "候选持仓区间重叠", "实际值": sum(row["overlap_days"] <= 0 for row in candidates), "预期值": 0, "差异": sum(row["overlap_days"] <= 0 for row in candidates), "状态": "OK" if all(row["overlap_days"] > 0 for row in candidates) else "ERROR", "说明": "至少同一交易日存在共同持仓"},
        {"检查项": "候选腿ID存在", "实际值": sum(row["a"]["episode_id"] not in episode_ids or row["b"]["episode_id"] not in episode_ids for row in candidates), "预期值": 0, "差异": sum(row["a"]["episode_id"] not in episode_ids or row["b"]["episode_id"] not in episode_ids for row in candidates), "状态": "OK" if all(row["a"]["episode_id"] in episode_ids and row["b"]["episode_id"] in episode_ids for row in candidates) else "ERROR", "说明": "每个候选均可追溯到逐腿区间"},
    ]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "arbitrage_candidates.csv", candidate_fields, flat_candidates)
    write_csv(args.output_dir / "arbitrage_priority_review.csv", candidate_fields, flat_priority)
    write_csv(args.output_dir / "arbitrage_leg_episodes.csv", list(episode_rows[0]) if episode_rows else [], episode_rows)
    write_csv(args.output_dir / "arbitrage_pair_summary.csv", list(pair_summary[0]) if pair_summary else [], pair_summary)
    write_csv(args.output_dir / "arbitrage_checks.csv", list(checks[0]), checks)

    metadata = {
        "source": str(args.completed_trades), "completed_rows": len(completed), "episodes": len(episodes),
        "candidates": len(candidates), "priority_pairs": len(priority), "high": confidence_counts["高"],
        "medium": confidence_counts["中"], "low": confidence_counts["低"], "minimum_score": args.minimum_score,
        "date_start": min(row["entry_trading_date"] for row in completed if row.get("entry_trading_date")),
        "date_end": max(row["exit_trading_date"] for row in completed if row.get("exit_trading_date")),
    }
    (args.output_dir / "arbitrage_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False))


if __name__ == "__main__":
    main()
