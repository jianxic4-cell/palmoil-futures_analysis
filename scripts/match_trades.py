"""FIFO-match futures opens/closes and create completed_trades.csv."""

from __future__ import annotations

from project_config import PROJECT_ROOT, RAW_STATEMENTS_DIR, MARKET_DATA_DIR

import argparse
import csv
import json
from collections import defaultdict, deque
from decimal import Decimal
from pathlib import Path
from typing import Any

from parse_statements import (
    ERROR_FIELDS, decimal_text, decimal_value, error_record, extract_any_position_snapshot,
    read_txt_statement, statement_metadata, txt_statement_metadata, write_csv,
)
from xlsx_reader import read_xlsx


COMPLETED_FIELDS = [
    "completed_trade_id", "account_id", "contract", "product", "direction", "quantity",
    "entry_datetime", "exit_datetime", "entry_trading_date", "exit_trading_date", "holding_seconds", "holding_days_approx",
    "holding_time_quality", "entry_price", "exit_price",
    "contract_multiplier", "entry_trade_id", "exit_trade_id", "entry_source", "entry_fee",
    "exit_fee", "total_fee", "broker_realized_pnl", "calculated_gross_pnl", "pnl_difference", "pnl_validation_status",
    "net_pnl_after_fees", "match_status", "entry_source_format", "exit_source_format",
    "entry_data_granularity", "exit_data_granularity", "entry_source_file", "exit_source_file",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def holding_seconds(entry: str, exit_: str) -> str:
    from datetime import datetime
    try:
        return str(int((datetime.fromisoformat(exit_) - datetime.fromisoformat(entry)).total_seconds()))
    except (TypeError, ValueError):
        return ""


def holding_days_approx(entry: str, exit_: str) -> str:
    from datetime import datetime
    try:
        return str((datetime.fromisoformat(exit_).date() - datetime.fromisoformat(entry).date()).days)
    except (TypeError, ValueError):
        return ""


def discover_prior_statement(source_root: Path, first_month: str) -> Path | None:
    candidates: dict[str, tuple[int, Path]] = {}
    for path in list(source_root.rglob("*.txt")) + list(source_root.rglob("*.xlsx")):
        try:
            if path.suffix.lower() == ".txt":
                month, _ = txt_statement_metadata(path, read_txt_statement(path))
                priority = 1
            else:
                month, _ = statement_metadata(read_xlsx(path))
                priority = 2
            if month and month < first_month:
                current = candidates.get(month)
                if current is None or priority > current[0]:
                    candidates[month] = (priority, path)
        except Exception:
            continue
    if not candidates:
        return None
    return candidates[max(candidates)][1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transactions", type=Path, default=PROJECT_ROOT / "output/transactions.csv")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "output/completed_trades.csv")
    parser.add_argument("--errors", type=Path, default=PROJECT_ROOT / "output/parsing_errors.csv")
    parser.add_argument("--source-root", type=Path, default=RAW_STATEMENTS_DIR)
    parser.add_argument("--prior-statement", type=Path, help="Month-end position snapshot before the first transaction month")
    args = parser.parse_args()

    transactions = read_csv(args.transactions)
    transactions.sort(key=lambda row: (
        row["trade_datetime"],
        row["source_file"],
        0 if row.get("source_format") == "TXT" and row.get("position_effect") == "OPEN" else 1,
        int(row.get("source_row") or 0),
        row["trade_id"],
    ))
    errors = read_csv(args.errors) if args.errors.exists() else []
    errors = [row for row in errors if not row.get("error_type", "").startswith("MATCH_")]
    inventory: dict[tuple[str, str, str], deque[dict[str, Any]]] = defaultdict(deque)

    prior_statement = args.prior_statement
    if transactions and prior_statement is None:
        prior_statement = discover_prior_statement(args.source_root, min(row["statement_month"] for row in transactions))
    seed_count = 0
    if prior_statement:
        seed_positions = sorted(extract_any_position_snapshot(prior_statement), key=lambda row: (row["entry_datetime"], row["trade_id"]))
        for position in seed_positions:
            quantity = decimal_value(position["quantity"]) or Decimal("0")
            fee = decimal_value(position["entry_fee"]) or Decimal("0")
            lot = dict(position)
            lot.update({"quantity_remaining": quantity, "fee_remaining": fee, "contract_multiplier": ""})
            inventory[(position["account_id"], position["contract"], position["direction"])].append(lot)
            seed_count += 1

    completed: list[dict[str, str]] = []
    completed_id = 0
    close_quantity_total = Decimal("0")
    completed_quantity_total = Decimal("0")
    close_pnl_total = Decimal("0")
    completed_pnl_total = Decimal("0")

    for transaction in transactions:
        account = transaction["account_id"]
        contract = transaction["contract"]
        quantity = decimal_value(transaction["quantity"]) or Decimal("0")
        fee = decimal_value(transaction["fee"]) or Decimal("0")
        price = decimal_value(transaction["price"]) or Decimal("0")
        multiplier = decimal_value(transaction["contract_multiplier"])
        side = transaction["side"]
        effect = transaction["position_effect"]

        if effect == "OPEN":
            direction = "LONG" if side == "BUY" else "SHORT"
            inventory[(account, contract, direction)].append({
                "account_id": account,
                "contract": contract,
                "product": transaction["product"],
                "direction": direction,
                "entry_price": transaction["price"],
                "quantity_remaining": quantity,
                "fee_remaining": fee,
                "entry_datetime": transaction["trade_datetime"],
                "entry_trading_date": transaction["trade_date"],
                "time_quality": transaction.get("time_quality", "REPORTED"),
                "data_granularity": transaction.get("data_granularity", "DETAILED_EXECUTION"),
                "source_format": transaction.get("source_format", "XLSX"),
                "trade_id": transaction["trade_id"],
                "entry_source": "TRANSACTION",
                "source_file": transaction["source_file"],
                "contract_multiplier": transaction["contract_multiplier"],
            })
            continue

        if effect != "CLOSE":
            errors.append(error_record(Path(transaction["source_file"]), "成交明细", transaction["source_row"], "MATCH_UNKNOWN_POSITION_EFFECT", f"无法配对开平标志 {transaction['position_effect_cn']!r}", transaction))
            continue

        direction = "LONG" if side == "SELL" else "SHORT"
        queue = inventory[(account, contract, direction)]
        remaining = quantity
        close_quantity_total += quantity
        broker_total = decimal_value(transaction["realized_pnl"]) or Decimal("0")
        close_pnl_total += broker_total
        while remaining > 0 and queue:
            lot = queue[0]
            before = lot["quantity_remaining"]
            matched = min(remaining, before)
            entry_fee = lot["fee_remaining"] * matched / before if before else Decimal("0")
            exit_fee = fee * matched / quantity if quantity else Decimal("0")
            broker_pnl = broker_total * matched / quantity if quantity else Decimal("0")
            entry_price = decimal_value(lot["entry_price"]) or Decimal("0")
            used_multiplier = multiplier or decimal_value(lot.get("contract_multiplier")) or Decimal("0")
            calculated = (
                (price - entry_price) * used_multiplier * matched
                if direction == "LONG"
                else (entry_price - price) * used_multiplier * matched
            )
            completed_id += 1
            completed_quantity_total += matched
            completed_pnl_total += broker_pnl
            precise_time = lot.get("time_quality") == "REPORTED" and transaction.get("time_quality") == "REPORTED"
            txt_derived = lot.get("source_format") == "TXT" or transaction.get("source_format") == "TXT"
            completed.append({
                "completed_trade_id": f"CT{completed_id:07d}",
                "account_id": account,
                "contract": contract,
                "product": transaction["product"],
                "direction": direction,
                "quantity": decimal_text(matched),
                "entry_datetime": lot["entry_datetime"],
                "exit_datetime": transaction["trade_datetime"],
                "entry_trading_date": lot.get("entry_trading_date", ""),
                "exit_trading_date": transaction["trade_date"],
                "holding_seconds": holding_seconds(lot["entry_datetime"], transaction["trade_datetime"]) if precise_time else "",
                "holding_days_approx": holding_days_approx(lot["entry_datetime"], transaction["trade_datetime"]),
                "holding_time_quality": "EXACT" if precise_time else "DATE_ONLY_APPROXIMATION",
                "entry_price": decimal_text(entry_price),
                "exit_price": decimal_text(price),
                "contract_multiplier": decimal_text(used_multiplier),
                "entry_trade_id": lot["trade_id"],
                "exit_trade_id": transaction["trade_id"],
                "entry_source": lot["entry_source"],
                "entry_fee": decimal_text(entry_fee),
                "exit_fee": decimal_text(exit_fee),
                "total_fee": decimal_text(entry_fee + exit_fee),
                "broker_realized_pnl": decimal_text(broker_pnl),
                "calculated_gross_pnl": decimal_text(calculated),
                "pnl_difference": "",
                "pnl_validation_status": "PENDING_EXIT_TRADE_CHECK",
                "net_pnl_after_fees": decimal_text(broker_pnl - entry_fee - exit_fee),
                "match_status": (
                    "MATCHED_PRIOR_SNAPSHOT" if lot["entry_source"] != "TRANSACTION"
                    else "INFERRED_FIFO_TXT_AGGREGATED" if txt_derived
                    else "MATCHED_FIFO"
                ),
                "entry_source_format": lot.get("source_format", "XLSX"),
                "exit_source_format": transaction.get("source_format", "XLSX"),
                "entry_data_granularity": lot.get("data_granularity", "DETAILED_EXECUTION"),
                "exit_data_granularity": transaction.get("data_granularity", "DETAILED_EXECUTION"),
                "entry_source_file": lot["source_file"],
                "exit_source_file": transaction["source_file"],
            })
            lot["quantity_remaining"] -= matched
            lot["fee_remaining"] -= entry_fee
            remaining -= matched
            if lot["quantity_remaining"] == 0:
                queue.popleft()

        if remaining > 0:
            unmatched_fee = fee * remaining / quantity if quantity else Decimal("0")
            unmatched_pnl = broker_total * remaining / quantity if quantity else Decimal("0")
            completed_id += 1
            completed_quantity_total += remaining
            completed_pnl_total += unmatched_pnl
            completed.append({
                "completed_trade_id": f"CT{completed_id:07d}", "account_id": account,
                "contract": contract, "product": transaction["product"], "direction": direction,
                "quantity": decimal_text(remaining), "entry_datetime": "", "exit_datetime": transaction["trade_datetime"],
                "entry_trading_date": "", "exit_trading_date": transaction["trade_date"],
                "holding_seconds": "", "holding_days_approx": "", "holding_time_quality": "NOT_AVAILABLE",
                "entry_price": "", "exit_price": transaction["price"],
                "contract_multiplier": transaction["contract_multiplier"], "entry_trade_id": "",
                "exit_trade_id": transaction["trade_id"], "entry_source": "MISSING_HISTORY",
                "entry_fee": "", "exit_fee": decimal_text(unmatched_fee), "total_fee": decimal_text(unmatched_fee),
                "broker_realized_pnl": decimal_text(unmatched_pnl), "calculated_gross_pnl": "", "pnl_difference": "",
                "pnl_validation_status": "NOT_COMPARABLE_MISSING_HISTORY",
                "net_pnl_after_fees": decimal_text(unmatched_pnl - unmatched_fee), "match_status": "UNMATCHED_OPEN_HISTORY",
                "entry_source_format": "", "exit_source_format": transaction.get("source_format", ""),
                "entry_data_granularity": "", "exit_data_granularity": transaction.get("data_granularity", ""),
                "entry_source_file": "", "exit_source_file": transaction["source_file"],
            })
            errors.append(error_record(Path(transaction["source_file"]), "成交明细", transaction["source_row"], "MATCH_MISSING_OPEN", f"{contract} {direction} 缺少历史开仓 {decimal_text(remaining)} 手", transaction, "WARNING"))

    if close_quantity_total != completed_quantity_total:
        errors.append(error_record(args.transactions, "", "", "MATCH_QUANTITY_RECONCILIATION", f"平仓手数差异={decimal_text(close_quantity_total - completed_quantity_total)}"))
    if abs(close_pnl_total - completed_pnl_total) > Decimal("0.01"):
        errors.append(error_record(args.transactions, "", "", "MATCH_PNL_RECONCILIATION", f"平仓盈亏差异={decimal_text(close_pnl_total - completed_pnl_total)}"))

    by_exit: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in completed:
        by_exit[(row["exit_source_file"], row["exit_trade_id"])].append(row)
    for rows in by_exit.values():
        if any(row["entry_source_format"] == "TXT" or row["exit_source_format"] == "TXT" for row in rows):
            for row in rows:
                if row["pnl_validation_status"] == "PENDING_EXIT_TRADE_CHECK":
                    row["pnl_validation_status"] = "NOT_COMPARABLE_TXT_AGGREGATED"
            continue
        comparable = all(
            row["entry_source"] == "TRANSACTION"
            and row["entry_trading_date"]
            and row["entry_trading_date"] == row["exit_trading_date"]
            for row in rows
        )
        if not comparable:
            for row in rows:
                if row["pnl_validation_status"] == "PENDING_EXIT_TRADE_CHECK":
                    row["pnl_validation_status"] = "NOT_COMPARABLE_DAILY_SETTLEMENT"
            continue
        broker = sum((decimal_value(row["broker_realized_pnl"]) or Decimal("0") for row in rows), Decimal("0"))
        calculated = sum((decimal_value(row["calculated_gross_pnl"]) or Decimal("0") for row in rows), Decimal("0"))
        difference = broker - calculated
        status = "PASS" if abs(difference) <= Decimal("0.01") else "NOT_COMPARABLE_POSITION_SELECTION"
        for row in rows:
            row["pnl_validation_status"] = status
            if status != "PASS":
                row["match_status"] = "INFERRED_FIFO_POSITION_SELECTION_AMBIGUOUS"
        rows[0]["pnl_difference"] = decimal_text(difference)
        if status != "PASS":
            first = rows[0]
            errors.append(error_record(
                Path(first["exit_source_file"]), "成交明细", "", "MATCH_POSITION_SELECTION_AMBIGUOUS",
                f"成交序号 {first['exit_trade_id']} 的券商平仓盈亏无法由 FIFO 开仓价直接复算，差异={decimal_text(difference)}；保留 FIFO 推断并以券商盈亏为准。",
                {"contract": first["contract"], "exit_trade_id": first["exit_trade_id"]}, "WARNING",
            ))

    write_csv(args.output, COMPLETED_FIELDS, completed)
    write_csv(args.errors, ERROR_FIELDS, errors)
    open_lots = sum(len(queue) for queue in inventory.values())
    unmatched = sum(row["match_status"] == "UNMATCHED_OPEN_HISTORY" for row in completed)
    print(json.dumps({
        "transactions": len(transactions), "prior_statement": str(prior_statement) if prior_statement else None,
        "seed_positions": seed_count, "completed_trade_rows": len(completed), "unmatched_rows": unmatched,
        "open_lots_carried_forward": open_lots, "output": str(args.output.resolve()),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
