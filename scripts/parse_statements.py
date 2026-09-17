"""Batch-parse broker XLSX statements into normalized transaction/account CSVs."""

from __future__ import annotations

from project_config import PROJECT_ROOT, RAW_STATEMENTS_DIR, MARKET_DATA_DIR

import argparse
import calendar
import csv
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from xlsx_reader import cell, find_header, find_value_after_label, normalized, read_xlsx


TRANSACTION_FIELDS = [
    "source_file", "source_format", "data_granularity", "time_quality", "actual_date_quality",
    "statement_month", "account_id", "exchange", "trade_date", "actual_trade_date",
    "trade_time", "trade_datetime", "raw_contract", "contract", "product", "trade_id", "side",
    "side_cn", "position_effect", "position_effect_cn", "hedge_type", "price",
    "quantity", "turnover", "fee", "realized_pnl", "contract_multiplier", "source_row",
]

DAILY_FIELDS = [
    "date", "statement_month", "account_id", "source_format", "realized_pnl", "transaction_fee",
    "other_fee", "total_fee", "deposit", "withdrawal", "net_cash_flow",
    "estimated_cash_balance", "estimated_equity", "equity_quality", "cash_flow_quality", "balance_quality",
    "reported_month_end_balance", "reported_month_end_equity", "month_end_floating_pnl",
    "source_file",
]

ERROR_FIELDS = [
    "severity", "source_file", "sheet", "row_number", "error_type", "message", "raw_values",
]


def decimal_value(value: Any, *, allow_blank: bool = True) -> Decimal | None:
    text = normalized(value).replace(",", "")
    if text in {"", "--", "-"}:
        return None if allow_blank else Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Not a number: {value!r}") from exc


def decimal_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    result = format(value.quantize(Decimal("0.000001")), "f").rstrip("0").rstrip(".")
    return result or "0"


def product_from_contract(contract: str) -> str:
    match = re.match(r"([A-Za-z]+)", contract)
    return match.group(1).upper() if match else contract


def normalize_contract(contract: str, trading_date: str) -> str:
    """Expand legacy Zhengzhou 3-digit delivery codes (e.g. PR609 -> PR2609)."""
    raw = contract.upper()
    match = re.fullmatch(r"([A-Z]+)(\d{3})", raw)
    if not match:
        return raw
    trade_year = int(trading_date[:4])
    year_digit = int(match.group(2)[0])
    delivery_year = (trade_year // 10) * 10 + year_digit
    while delivery_year < trade_year - 1:
        delivery_year += 10
    while delivery_year > trade_year + 5:
        delivery_year -= 10
    return f"{match.group(1)}{delivery_year % 100:02d}{match.group(2)[1:]}"


def iso_datetime(actual_date: str, trade_time: str) -> str:
    if not actual_date:
        return ""
    clean_time = trade_time or "00:00:00"
    return f"{actual_date}T{clean_time}"


def error_record(
    source: Path,
    sheet: str,
    row_number: int | str,
    error_type: str,
    message: str,
    raw_values: Any = "",
    severity: str = "ERROR",
) -> dict[str, str]:
    return {
        "severity": severity,
        "source_file": str(source),
        "sheet": sheet,
        "row_number": str(row_number),
        "error_type": error_type,
        "message": message,
        "raw_values": json.dumps(raw_values, ensure_ascii=False, default=str),
    }


def statement_metadata(workbook: Any) -> tuple[str, str]:
    rows = workbook.sheet("客户交易结算月报")
    month = normalized(find_value_after_label(rows, "交易月份", 10))
    account_id = normalized(find_value_after_label(rows, "客户期货期权内部资金账户", 10))
    return month, account_id


def parse_transactions(source: Path, workbook: Any, errors: list[dict[str, str]]) -> list[dict[str, str]]:
    rows = workbook.sheet("成交明细")
    month, account_id = statement_metadata(workbook)
    required = {"交易日期", "合约", "成交序号", "成交时间", "买/卖", "成交价", "手数", "开/平", "手续费", "平仓盈亏"}
    header_index, columns = find_header(rows, required)
    output: list[dict[str, str]] = []
    for index, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        trade_date = normalized(cell(row, columns["交易日期"]))
        if trade_date == "合计":
            break
        if not trade_date:
            continue
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", trade_date):
            errors.append(error_record(source, "成交明细", index, "INVALID_TRADE_DATE", f"无法识别交易日期 {trade_date!r}", row))
            continue
        try:
            contract = normalized(cell(row, columns["合约"]))
            trade_id = normalized(cell(row, columns["成交序号"]))
            trade_time = normalized(cell(row, columns["成交时间"]))
            side_cn = normalized(cell(row, columns["买/卖"]))
            position_cn = normalized(cell(row, columns["开/平"]))
            hedge_column = columns.get("投机（一般）/套保/套利")
            actual_column = columns.get("实际成交日期")
            actual_date = normalized(cell(row, actual_column)) if actual_column is not None else trade_date
            price = decimal_value(cell(row, columns["成交价"]))
            quantity = decimal_value(cell(row, columns["手数"]))
            turnover = decimal_value(cell(row, columns.get("成交额", -1))) if "成交额" in columns else None
            fee = decimal_value(cell(row, columns["手续费"])) or Decimal("0")
            realized_pnl = decimal_value(cell(row, columns["平仓盈亏"]))
            if side_cn not in {"买", "卖"}:
                raise ValueError(f"未知买卖方向 {side_cn!r}")
            if not position_cn:
                raise ValueError("开/平字段为空")
            if price is None or quantity is None or quantity <= 0:
                raise ValueError("价格或手数无效")
            multiplier = None
            if turnover is not None and price != 0 and quantity != 0:
                multiplier = turnover / price / quantity
            normalized_contract = normalize_contract(contract, trade_date)
            output.append({
                "source_file": str(source),
                "source_format": "XLSX",
                "data_granularity": "DETAILED_EXECUTION",
                "time_quality": "REPORTED",
                "actual_date_quality": "REPORTED",
                "statement_month": month,
                "account_id": account_id,
                "exchange": "",
                "trade_date": trade_date,
                "actual_trade_date": actual_date or trade_date,
                "trade_time": trade_time,
                "trade_datetime": iso_datetime(actual_date or trade_date, trade_time),
                "raw_contract": contract,
                "contract": normalized_contract,
                "product": product_from_contract(normalized_contract),
                "trade_id": trade_id,
                "side": "BUY" if side_cn == "买" else "SELL",
                "side_cn": side_cn,
                "position_effect": "OPEN" if "开" in position_cn else "CLOSE" if "平" in position_cn else "UNKNOWN",
                "position_effect_cn": position_cn,
                "hedge_type": normalized(cell(row, hedge_column)) if hedge_column is not None else "",
                "price": decimal_text(price),
                "quantity": decimal_text(quantity),
                "turnover": decimal_text(turnover),
                "fee": decimal_text(fee),
                "realized_pnl": decimal_text(realized_pnl),
                "contract_multiplier": decimal_text(multiplier),
                "source_row": str(index),
            })
        except Exception as exc:  # keep malformed rows visible in parsing_errors.csv
            errors.append(error_record(source, "成交明细", index, "INVALID_TRANSACTION", str(exc), row))
    return output


def _summary_number(rows: list[list[Any]], label: str) -> Decimal:
    value = find_value_after_label(rows[8:20], label)
    return decimal_value(value, allow_blank=False) or Decimal("0")


def _section_rows(rows: list[list[Any]], title_starts_with: str, next_title: str | None = None) -> list[tuple[int, list[Any]]]:
    start = next((i for i, row in enumerate(rows) if normalized(cell(row, 0)).startswith(title_starts_with)), -1)
    if start < 0:
        return []
    end = len(rows)
    if next_title:
        end = next((i for i, row in enumerate(rows[start + 1 :], start + 1) if normalized(cell(row, 0)).startswith(next_title)), len(rows))
    return list(enumerate(rows[start + 2 : end], start=start + 3))


def build_daily_accounts(
    source: Path,
    workbook: Any,
    transactions: list[dict[str, str]],
    errors: list[dict[str, str]],
) -> list[dict[str, str]]:
    month, account_id = statement_metadata(workbook)
    main_rows = workbook.sheet("客户交易结算月报")
    opening = _summary_number(main_rows, "上月结存")
    monthly_deposit = _summary_number(main_rows, "当月存取合计")
    monthly_pnl = _summary_number(main_rows, "当月盈亏")
    monthly_fee = _summary_number(main_rows, "当月手续费")
    ending = _summary_number(main_rows, "当月结存")
    equity = _summary_number(main_rows, "客户权益")
    floating = _summary_number(main_rows, "浮动盈亏")

    daily: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for transaction in transactions:
        day = transaction["trade_date"]
        daily[day]["realized_pnl"] += decimal_value(transaction["realized_pnl"]) or Decimal("0")
        daily[day]["transaction_fee"] += decimal_value(transaction["fee"]) or Decimal("0")

    for row_number, row in _section_rows(main_rows, "期货期权账户出入金明细", "其它资金明细"):
        day = normalized(cell(row, 0))
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            continue
        try:
            daily[day]["deposit"] += decimal_value(cell(row, 2)) or Decimal("0")
            daily[day]["withdrawal"] += decimal_value(cell(row, 4)) or Decimal("0")
        except Exception as exc:
            errors.append(error_record(source, "客户交易结算月报", row_number, "INVALID_CASH_FLOW", str(exc), row))

    for row_number, row in _section_rows(main_rows, "其它资金明细", "期货成交汇总"):
        day = normalized(cell(row, 0))
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            continue
        fee_type = normalized(cell(row, 4))
        amount = decimal_value(cell(row, 6)) or Decimal("0")
        if fee_type != "交易手续费" and amount < 0:
            daily[day]["other_fee"] += -amount

    year, month_number = map(int, month.split("-"))
    first_day = date(year, month_number, 1)
    last_day = date(year, month_number, calendar.monthrange(year, month_number)[1])
    balance = opening
    output: list[dict[str, str]] = []
    current = first_day
    while current <= last_day:
        day = current.isoformat()
        values = daily[day]
        realized = values["realized_pnl"]
        transaction_fee = values["transaction_fee"]
        other_fee = values["other_fee"]
        total_fee = transaction_fee + other_fee
        deposit = values["deposit"]
        withdrawal = values["withdrawal"]
        net_cash_flow = deposit - withdrawal
        balance += net_cash_flow + realized - total_fee
        is_month_end = current == last_day
        output.append({
            "date": day,
            "statement_month": month,
            "account_id": account_id,
            "source_format": "XLSX",
            "realized_pnl": decimal_text(realized),
            "transaction_fee": decimal_text(transaction_fee),
            "other_fee": decimal_text(other_fee),
            "total_fee": decimal_text(total_fee),
            "deposit": decimal_text(deposit),
            "withdrawal": decimal_text(withdrawal),
            "net_cash_flow": decimal_text(net_cash_flow),
            "estimated_cash_balance": decimal_text(balance),
            "estimated_equity": decimal_text(equity if is_month_end else balance),
            "equity_quality": "REPORTED_MONTH_END" if is_month_end else "ESTIMATED_EXCLUDES_DAILY_FLOATING_PNL",
            "cash_flow_quality": "REPORTED_DAILY",
            "balance_quality": "REPORTED_MONTH_END" if is_month_end else "ESTIMATED_EXCLUDES_DAILY_FLOATING_PNL",
            "reported_month_end_balance": decimal_text(ending) if is_month_end else "",
            "reported_month_end_equity": decimal_text(equity) if is_month_end else "",
            "month_end_floating_pnl": decimal_text(floating) if is_month_end else "",
            "source_file": str(source),
        })
        current += timedelta(days=1)

    transaction_fee_total = sum((decimal_value(row["fee"]) or Decimal("0") for row in transactions), Decimal("0"))
    transaction_pnl_total = sum((decimal_value(row["realized_pnl"]) or Decimal("0") for row in transactions), Decimal("0"))
    other_fee_total = sum((values["other_fee"] for values in daily.values()), Decimal("0"))
    cash_total = sum((values["deposit"] - values["withdrawal"] for values in daily.values()), Decimal("0"))
    checks = [
        ("PNL_RECONCILIATION", transaction_pnl_total - monthly_pnl, "逐笔平仓盈亏与月报当月盈亏"),
        ("FEE_RECONCILIATION", transaction_fee_total + other_fee_total - monthly_fee, "逐笔手续费+其它费用与月报手续费"),
        ("CASH_FLOW_RECONCILIATION", cash_total - monthly_deposit, "出入金明细与月报存取合计"),
        ("ENDING_BALANCE_RECONCILIATION", opening + monthly_deposit + monthly_pnl - monthly_fee - ending, "期初+存取+盈亏-费用与期末结存"),
        ("EQUITY_RECONCILIATION", ending + floating - equity, "期末结存+浮动盈亏与客户权益"),
    ]
    for error_type, difference, description in checks:
        if abs(difference) > Decimal("0.01"):
            errors.append(error_record(source, "客户交易结算月报", "", error_type, f"{description}差异={decimal_text(difference)}", severity="ERROR"))
    return output


def read_txt_statement(source: Path) -> str:
    """Read the broker's fixed-width text export without changing the original file."""
    payload = source.read_bytes()
    for encoding in ("gb18030", "utf-8-sig", "utf-8"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别TXT编码（已尝试GB18030和UTF-8）")


def txt_statement_metadata(source: Path, text: str) -> tuple[str, str]:
    month_match = re.search(r"(20\d{2})(\d{2})账单", source.name)
    if not month_match:
        month_match = re.search(r"\b(20\d{2})(\d{2})\d{2}\b", text)
    if not month_match:
        raise ValueError("无法从TXT文件名或内容识别账单月份")
    month = f"{month_match.group(1)}-{month_match.group(2)}"
    account_match = re.match(r"(\d+)-", source.name)
    if not account_match:
        account_match = re.search(r"\b(\d{6,20})\b", text)
    if not account_match:
        raise ValueError("无法从TXT文件名或内容识别资产账号")
    return month, account_match.group(1)


def txt_summary_number(text: str, label: str) -> Decimal:
    match = re.search(rf"{re.escape(label)}\s*[:：]\s*(-?[\d,]+(?:\.\d+)?)", text)
    if not match:
        raise ValueError(f"TXT资金清单缺少字段：{label}")
    return decimal_value(match.group(1), allow_blank=False) or Decimal("0")


def parse_txt_transactions(source: Path, text: str, errors: list[dict[str, str]]) -> list[dict[str, str]]:
    month, account_id = txt_statement_metadata(source, text)
    output: list[dict[str, str]] = []
    header_found = "成交日期 成交时间" in text
    for line_number, line in enumerate(text.splitlines(), 1):
        if not re.match(r"^\s*\d{8}\s+\d{2}:\d{2}:\d{2}\s+", line):
            continue
        parts = line.split()
        if len(parts) < 15:
            errors.append(error_record(source, "期货客户账单_成交记录单", line_number, "INVALID_TXT_TRANSACTION", f"预期至少15列，实际{len(parts)}列", line))
            continue
        try:
            (
                raw_date, trade_time, exchange, contract, trade_id, side_cn, hedge_type,
                raw_price, raw_quantity, raw_turnover, position_cn, raw_fee, raw_pnl,
                _premium, _settlement_transfer,
            ) = parts[:15]
            trade_date = datetime.strptime(raw_date, "%Y%m%d").date().isoformat()
            price = decimal_value(raw_price)
            quantity = decimal_value(raw_quantity)
            turnover = decimal_value(raw_turnover)
            fee = decimal_value(raw_fee) or Decimal("0")
            realized_pnl = decimal_value(raw_pnl)
            if side_cn not in {"买", "卖"}:
                raise ValueError(f"未知买卖方向 {side_cn!r}")
            if price is None or quantity is None or quantity <= 0:
                raise ValueError("价格或手数无效")
            multiplier = turnover / price / quantity if turnover is not None and price and quantity else None
            time_quality = "NOT_AVAILABLE" if trade_time == "00:00:00" else "REPORTED"
            normalized_contract = normalize_contract(contract, trade_date)
            output.append({
                "source_file": str(source),
                "source_format": "TXT",
                "data_granularity": "AGGREGATED_SETTLEMENT_ROW",
                "time_quality": time_quality,
                "actual_date_quality": "ASSUMED_EQUAL_TRADING_DATE",
                "statement_month": month,
                "account_id": account_id,
                "exchange": exchange,
                "trade_date": trade_date,
                "actual_trade_date": trade_date,
                "trade_time": trade_time,
                "trade_datetime": iso_datetime(trade_date, trade_time),
                "raw_contract": contract,
                "contract": normalized_contract,
                "product": product_from_contract(normalized_contract),
                "trade_id": trade_id,
                "side": "BUY" if side_cn == "买" else "SELL",
                "side_cn": side_cn,
                "position_effect": "OPEN" if "开" in position_cn else "CLOSE" if "平" in position_cn else "UNKNOWN",
                "position_effect_cn": position_cn,
                "hedge_type": hedge_type,
                "price": decimal_text(price),
                "quantity": decimal_text(quantity),
                "turnover": decimal_text(turnover),
                "fee": decimal_text(fee),
                "realized_pnl": decimal_text(realized_pnl),
                "contract_multiplier": decimal_text(multiplier),
                "source_row": str(line_number),
            })
        except Exception as exc:
            errors.append(error_record(source, "期货客户账单_成交记录单", line_number, "INVALID_TXT_TRANSACTION", str(exc), line))
    if not header_found:
        monthly_pnl = txt_summary_number(text, "平仓盈亏")
        monthly_fee = txt_summary_number(text, "期间手续费")
        if monthly_pnl != 0 or monthly_fee != 0:
            errors.append(error_record(source, "期货客户账单_成交记录单", "", "TXT_TRANSACTION_SECTION_MISSING", "资金清单存在盈亏或手续费，但TXT没有成交记录表头"))
    return output


def build_txt_daily_accounts(
    source: Path,
    text: str,
    transactions: list[dict[str, str]],
    errors: list[dict[str, str]],
) -> list[dict[str, str]]:
    month, account_id = txt_statement_metadata(source, text)
    opening = txt_summary_number(text, "期初结存")
    monthly_deposit = txt_summary_number(text, "期间入金")
    monthly_withdrawal = txt_summary_number(text, "期间出金")
    monthly_net_cash = txt_summary_number(text, "期间出入金合计")
    monthly_pnl = txt_summary_number(text, "平仓盈亏")
    monthly_fee = txt_summary_number(text, "期间手续费")
    ending = txt_summary_number(text, "期末结存")
    equity = txt_summary_number(text, "资金权益")
    floating = txt_summary_number(text, "浮动持仓盈亏")

    daily: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for transaction in transactions:
        day = transaction["trade_date"]
        daily[day]["realized_pnl"] += decimal_value(transaction["realized_pnl"]) or Decimal("0")
        daily[day]["transaction_fee"] += decimal_value(transaction["fee"]) or Decimal("0")

    year, month_number = map(int, month.split("-"))
    first_day = date(year, month_number, 1)
    last_day = date(year, month_number, calendar.monthrange(year, month_number)[1])
    last_day_text = last_day.isoformat()
    daily[last_day_text]["deposit"] += monthly_deposit
    daily[last_day_text]["withdrawal"] += monthly_withdrawal
    transaction_fee_total = sum((decimal_value(row["fee"]) or Decimal("0") for row in transactions), Decimal("0"))
    other_fee_total = monthly_fee - transaction_fee_total
    if other_fee_total >= 0:
        daily[last_day_text]["other_fee"] += other_fee_total

    balance = opening
    output: list[dict[str, str]] = []
    current = first_day
    while current <= last_day:
        day = current.isoformat()
        values = daily[day]
        realized = values["realized_pnl"]
        transaction_fee = values["transaction_fee"]
        other_fee = values["other_fee"]
        total_fee = transaction_fee + other_fee
        deposit = values["deposit"]
        withdrawal = values["withdrawal"]
        net_cash_flow = deposit - withdrawal
        balance += net_cash_flow + realized - total_fee
        is_month_end = current == last_day
        output.append({
            "date": day,
            "statement_month": month,
            "account_id": account_id,
            "source_format": "TXT",
            "realized_pnl": decimal_text(realized),
            "transaction_fee": decimal_text(transaction_fee),
            "other_fee": decimal_text(other_fee),
            "total_fee": decimal_text(total_fee),
            "deposit": decimal_text(deposit),
            "withdrawal": decimal_text(withdrawal),
            "net_cash_flow": decimal_text(net_cash_flow),
            "estimated_cash_balance": decimal_text(balance),
            "estimated_equity": decimal_text(equity if is_month_end else balance),
            "equity_quality": "REPORTED_MONTH_END" if is_month_end else "ESTIMATED_EXCLUDES_DAILY_FLOATING_PNL",
            "cash_flow_quality": "MONTHLY_TOTAL_APPLIED_AT_MONTH_END",
            "balance_quality": "REPORTED_MONTH_END" if is_month_end else "ESTIMATED_MONTHLY_CASHFLOW_TIMING_UNKNOWN",
            "reported_month_end_balance": decimal_text(ending) if is_month_end else "",
            "reported_month_end_equity": decimal_text(equity) if is_month_end else "",
            "month_end_floating_pnl": decimal_text(floating) if is_month_end else "",
            "source_file": str(source),
        })
        current += timedelta(days=1)

    transaction_pnl_total = sum((decimal_value(row["realized_pnl"]) or Decimal("0") for row in transactions), Decimal("0"))
    checks = [
        ("TXT_PNL_RECONCILIATION", transaction_pnl_total - monthly_pnl, "TXT成交记录平仓盈亏与资金清单"),
        ("TXT_FEE_RECONCILIATION", transaction_fee_total + max(other_fee_total, Decimal("0")) - monthly_fee, "TXT成交手续费与资金清单"),
        ("TXT_CASH_FLOW_RECONCILIATION", monthly_deposit - monthly_withdrawal - monthly_net_cash, "TXT入金-出金与出入金合计"),
        ("TXT_ENDING_BALANCE_RECONCILIATION", opening + monthly_net_cash + monthly_pnl - monthly_fee - ending, "TXT期初+存取+盈亏-费用与期末结存"),
        ("TXT_EQUITY_RECONCILIATION", ending + floating - equity, "TXT期末结存+浮动持仓盈亏与资金权益"),
    ]
    for error_type, difference, description in checks:
        if abs(difference) > Decimal("0.01"):
            errors.append(error_record(source, "期货客户账单_资金清单", "", error_type, f"{description}差异={decimal_text(difference)}"))
    return output


def extract_txt_position_snapshot(source: Path) -> list[dict[str, str]]:
    text = read_txt_statement(source)
    month, account_id = txt_statement_metadata(source, text)
    positions: list[dict[str, str]] = []
    in_position_section = False
    for line_number, line in enumerate(text.splitlines(), 1):
        if "期货客户账单_持仓盈亏单" in line:
            in_position_section = True
            continue
        if not in_position_section:
            continue
        parts = line.split()
        if len(parts) != 13 or parts[0] == "成交编号" or parts[0] == "合计":
            continue
        try:
            trade_id, raw_contract = parts[0], parts[1].upper()
            year, month_number = map(int, month.split("-"))
            snapshot_date = date(year, month_number, calendar.monthrange(year, month_number)[1]).isoformat()
            contract = normalize_contract(raw_contract, snapshot_date)
            buy_quantity, buy_price = decimal_value(parts[2]) or Decimal("0"), decimal_value(parts[3]) or Decimal("0")
            sell_quantity, sell_price = decimal_value(parts[4]) or Decimal("0"), decimal_value(parts[5]) or Decimal("0")
        except ValueError:
            continue
        for direction, quantity, price in (("LONG", buy_quantity, buy_price), ("SHORT", sell_quantity, sell_price)):
            if quantity <= 0:
                continue
            positions.append({
                "source_file": str(source), "statement_month": month, "account_id": account_id,
                "contract": contract, "product": product_from_contract(contract), "trade_id": trade_id,
                "direction": direction, "entry_price": decimal_text(price), "quantity": decimal_text(quantity),
                "entry_datetime": f"{snapshot_date}T00:00:00", "entry_trading_date": "",
                "entry_fee": "0", "entry_source": "PRIOR_MONTH_TXT_POSITION_SNAPSHOT",
                "data_granularity": "AGGREGATED_POSITION_SNAPSHOT", "time_quality": "NOT_AVAILABLE",
                "source_format": "TXT",
            })
    return positions


def extract_position_snapshot(source: Path) -> list[dict[str, str]]:
    workbook = read_xlsx(source)
    month, account_id = statement_metadata(workbook)
    rows = workbook.sheet("持仓明细")
    required = {"合约", "成交序号", "买持仓", "买入价", "卖持仓", "卖出价", "实际成交日期"}
    header_index, columns = find_header(rows, required)
    positions: list[dict[str, str]] = []
    for index, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        raw_contract = normalized(cell(row, columns["合约"]))
        if not raw_contract or raw_contract == "合计":
            continue
        year, month_number = map(int, month.split("-"))
        snapshot_date = date(year, month_number, calendar.monthrange(year, month_number)[1]).isoformat()
        contract = normalize_contract(raw_contract, snapshot_date)
        for direction, quantity_label, price_label in (
            ("LONG", "买持仓", "买入价"),
            ("SHORT", "卖持仓", "卖出价"),
        ):
            quantity = decimal_value(cell(row, columns[quantity_label])) or Decimal("0")
            if quantity <= 0:
                continue
            positions.append({
                "source_file": str(source),
                "statement_month": month,
                "account_id": account_id,
                "contract": contract,
                "product": product_from_contract(contract),
                "trade_id": normalized(cell(row, columns["成交序号"])),
                "direction": direction,
                "entry_price": decimal_text(decimal_value(cell(row, columns[price_label]))),
                "quantity": decimal_text(quantity),
                "entry_datetime": f"{normalized(cell(row, columns['实际成交日期']))}T00:00:00",
                "entry_trading_date": "",
                "entry_fee": "0",
                "entry_source": "PRIOR_MONTH_POSITION_SNAPSHOT",
                "data_granularity": "DETAILED_POSITION_LOT",
                "time_quality": "DATE_ONLY",
                "source_format": "XLSX",
            })
    return positions


def extract_any_position_snapshot(source: Path) -> list[dict[str, str]]:
    if source.suffix.lower() == ".txt":
        return extract_txt_position_snapshot(source)
    return extract_position_snapshot(source)


def write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=RAW_STATEMENTS_DIR)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "output")
    parser.add_argument("--year", type=int, default=2026, help="Export one year (default: 2026)")
    parser.add_argument("--start-year", type=int, help="Export from this year; overrides --year")
    parser.add_argument("--end-year", type=int, help="Export through this year; defaults to --start-year")
    args = parser.parse_args()
    if not args.source.is_dir():
        parser.error(f"File doesn't exist or it's not a folder：{args.source}")
    
    xlsx_files = sorted(path for path in args.source.rglob("*.xlsx") if "anonymized" not in str(path).lower())
    txt_files = sorted(path for path in args.source.rglob("*.txt") if "anonymized" not in str(path).lower())
    selected: dict[tuple[str, str], tuple[str, Path]] = {}
    
    discovery_errors: list[dict[str, str]] = []
    def register_statement(source_format, source, month, account_id):
        key = (account_id, month)

        if key in selected:
            previous_file = selected[key][1]

            parser.error(
                f"账户 {account_id} 在 {month} 存在多份账单：\n"
                f"第一份：{previous_file}\n"
                f"第二份：{source}\n"
                "请确认输入目录中该账户该月份只保留一份完整账单。"
                "本次没有更新输出文件。"
            )

        selected[key] = (source_format, source)
    
    for source in txt_files:
        try:
            text = read_txt_statement(source)
            month, account_id = txt_statement_metadata(source, text)
            register_statement("TXT", source, month, account_id)
        except Exception as exc:
            discovery_errors.append(error_record(source, "", "", "TXT_DISCOVERY_FAILED", str(exc)))
    for source in xlsx_files:
        try:
            workbook = read_xlsx(source)
            month, account_id = statement_metadata(workbook)
            register_statement("XLSX", source, month, account_id)
        except Exception as exc:
            discovery_errors.append(error_record(source, "", "", "XLSX_DISCOVERY_FAILED", str(exc)))
    transactions: list[dict[str, str]] = []
    daily_accounts: list[dict[str, str]] = []
    errors: list[dict[str, str]] = discovery_errors
    processed = 0
    xlsx_processed = 0
    txt_processed = 0
    start_year = args.start_year if args.start_year is not None else args.year
    end_year = args.end_year if args.end_year is not None else start_year
    for (account_id, month), (source_format, source) in sorted(selected.items(), key=lambda item: (item[0][1], item[0][0])):
        try:
            statement_year = int(month[:4])
            if not start_year <= statement_year <= end_year:
                continue
            if source_format == "XLSX":
                workbook = read_xlsx(source)
                current_transactions = parse_transactions(source, workbook, errors)
                current_daily = build_daily_accounts(source, workbook, current_transactions, errors)
                xlsx_processed += 1
            else:
                text = read_txt_statement(source)
                current_transactions = parse_txt_transactions(source, text, errors)
                current_daily = build_txt_daily_accounts(source, text, current_transactions, errors)
                txt_processed += 1
            transactions.extend(current_transactions)
            daily_accounts.extend(current_daily)
            processed += 1
        except Exception as exc:
            errors.append(error_record(source, "", "", "STATEMENT_PARSE_FAILED", str(exc)))

    if processed == 0:
        parser.error(
            f"没有成功处理 {start_year}—{end_year} 年的账单。"
            "请检查目录、年份和文件格式；原有输出文件未修改。"
        )

    transactions.sort(key=lambda row: (row["trade_datetime"], row["source_file"], int(row.get("source_row") or 0), row["trade_id"]))
    daily_accounts.sort(key=lambda row: (row["date"], row["account_id"]))
    write_csv(args.output / "transactions.csv", TRANSACTION_FIELDS, transactions)
    write_csv(args.output / "daily_accounts.csv", DAILY_FIELDS, daily_accounts)
    write_csv(args.output / "parsing_errors.csv", ERROR_FIELDS, errors)
    print(json.dumps({
        "statements_processed": processed,
        "xlsx_statements": xlsx_processed,
        "txt_statements": txt_processed,
        "transactions": len(transactions),
        "daily_rows": len(daily_accounts),
        "errors": len(errors),
        "output": str(args.output.resolve()),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
