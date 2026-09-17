"""Generate two reports using explicitly synthetic trades, without private data."""
from __future__ import annotations

import argparse
import calendar
from pathlib import Path
import subprocess
import sys

from parse_statements import DAILY_FIELDS, TRANSACTION_FIELDS, write_csv
from project_config import PROJECT_ROOT


def generate_inputs(destination: Path):
    """Two months, two opposite P contracts, deterministic prices and fees."""
    transactions, daily = [], []
    balance = 100000
    for month_number in (1, 2):
        month = f"2024-{month_number:02d}"
        for leg, (contract, opening, closing, side, pnl) in enumerate([
            ("P2405", 8000, 8020, "BUY", 400),
            ("P2409", 8100, 8110, "SELL", -200),
        ]):
            for effect, day, price in [("OPEN", 10, opening), ("CLOSE", 11, closing)]:
                actual_side = side if effect == "OPEN" else ("SELL" if side == "BUY" else "BUY")
                date = f"{month}-{day:02d}"
                time = f"{9 + leg:02d}:00:00"
                transactions.append({
                    "source_file": f"SYNTHETIC-{month}", "source_format": "XLSX",
                    "data_granularity": "DETAILED_EXECUTION", "time_quality": "REPORTED",
                    "actual_date_quality": "REPORTED", "statement_month": month,
                    "account_id": "SYNTHETIC-DEMO", "exchange": "DCE",
                    "trade_date": date, "actual_trade_date": date, "trade_time": time,
                    "trade_datetime": f"{date}T{time}", "raw_contract": contract,
                    "contract": contract, "product": "P", "trade_id": f"DEMO-{len(transactions) + 1}",
                    "side": actual_side, "side_cn": "买" if actual_side == "BUY" else "卖",
                    "position_effect": effect, "position_effect_cn": "开" if effect == "OPEN" else "平",
                    "hedge_type": "SYNTHETIC", "price": price, "quantity": 2,
                    "turnover": price * 20, "fee": 2, "realized_pnl": pnl if effect == "CLOSE" else 0,
                    "contract_multiplier": 10, "source_row": len(transactions) + 2,
                })
        last_day = calendar.monthrange(2024, month_number)[1]
        for day in range(1, last_day + 1):
            pnl = 200 if day == 11 else 0
            fee = 4 if day in (10, 11) else 0
            balance += pnl - fee
            daily.append({
                "date": f"{month}-{day:02d}", "statement_month": month,
                "account_id": "SYNTHETIC-DEMO", "source_format": "XLSX",
                "realized_pnl": pnl, "transaction_fee": fee, "other_fee": 0, "total_fee": fee,
                "deposit": 0, "withdrawal": 0, "net_cash_flow": 0,
                "estimated_cash_balance": balance, "estimated_equity": balance,
                "equity_quality": "SYNTHETIC", "cash_flow_quality": "REPORTED_DAILY",
                "balance_quality": "SYNTHETIC", "reported_month_end_balance": balance if day == last_day else "",
                "reported_month_end_equity": balance if day == last_day else "",
                "month_end_floating_pnl": 0, "source_file": f"SYNTHETIC-{month}",
            })
    write_csv(destination / "transactions.csv", TRANSACTION_FIELDS, transactions)
    write_csv(destination / "daily_accounts.csv", DAILY_FIELDS, daily)


def prepare_analysis(destination: Path):
    generate_inputs(destination)
    scripts = Path(__file__).resolve().parent
    commands = [
        ["match_trades.py", "--transactions", str(destination / "transactions.csv"),
         "--output", str(destination / "completed_trades.csv"), "--errors", str(destination / "parsing_errors.csv"),
         "--source-root", str(destination / "no_private_statements")],
        ["analyze_trading_style.py", "--input-dir", str(destination), "--output-dir", str(destination)],
        ["identify_arbitrage.py", "--completed-trades", str(destination / "completed_trades.csv"), "--output-dir", str(destination)],
    ]
    for script, *args in commands:
        subprocess.run([sys.executable, str(scripts / script), *args], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "output" / "synthetic_reports")
    parser.add_argument("--previews", action="store_true", help="Also create PNG chart summaries")
    args = parser.parse_args()
    prepare_analysis(args.output_dir)
    for kind in ("style", "arbitrage"):
        command = [sys.executable, str(Path(__file__).with_name(f"build_{kind}_report.py")),
                   "--data-dir", str(args.output_dir), "--synthetic"]
        if args.previews:
            command += ["--preview-dir", str(args.output_dir / "previews" / kind)]
        subprocess.run(command, check=True)
    print("Synthetic report demo completed. These figures are invented, not historical performance.")


if __name__ == "__main__":
    main()
