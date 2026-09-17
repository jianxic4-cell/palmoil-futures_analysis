"""Regression tests for account-period profits, using synthetic cash ledgers."""
import calendar
import contextlib
import csv
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import analyze_trading_style as style
from demo_reports import generate_inputs
from parse_statements import DAILY_FIELDS, write_csv


def read_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


class AccountProfitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.fixture.cleanup)
        cls.fixture_path = Path(cls.fixture.name)
        generate_inputs(cls.fixture_path)
        result = subprocess.run([
            sys.executable, "-B", str(SCRIPTS / "match_trades.py"),
            "--transactions", str(cls.fixture_path / "transactions.csv"),
            "--output", str(cls.fixture_path / "completed_trades.csv"),
            "--errors", str(cls.fixture_path / "parsing_errors.csv"),
            "--source-root", str(cls.fixture_path / "no_private_data"),
        ], capture_output=True, text=True, encoding="utf-8",
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8"))
        if result.returncode:
            raise RuntimeError(result.stderr)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        for name in ("transactions.csv", "completed_trades.csv"):
            shutil.copyfile(self.fixture_path / name, self.output / name)

    def check_case(self, *, deposit=0, withdrawal=0, profit=0, fee=0,
                   reverse=False, months=("2024-01", "2024-02")):
        # Independent ledger arithmetic: begin with 100,000, then apply each
        # day's cashflows and net trading result. Each baseline month earns 192.
        daily = []
        balance = 100000.0
        for month_index, month in enumerate(months):
            year, month_number = map(int, month.split("-"))
            last_day = calendar.monthrange(year, month_number)[1]
            for day in range(1, last_day + 1):
                first = month_index == 0 and day == 1
                day_deposit, day_withdrawal = (deposit, withdrawal) if first else (0, 0)
                day_pnl = (200 if day == 11 else 0) + (profit if first else 0)
                day_fee = (4 if day in (10, 11) else 0) + (fee if first else 0)
                flow = day_deposit - day_withdrawal
                balance += flow + day_pnl - day_fee
                daily.append({
                    "date": f"{month}-{day:02d}", "statement_month": month,
                    "account_id": "SYNTHETIC-DEMO", "source_format": "XLSX",
                    "realized_pnl": day_pnl, "transaction_fee": day_fee,
                    "other_fee": 0, "total_fee": day_fee, "deposit": day_deposit,
                    "withdrawal": day_withdrawal, "net_cash_flow": flow,
                    "estimated_cash_balance": balance, "estimated_equity": balance,
                    "equity_quality": "SYNTHETIC", "cash_flow_quality": "REPORTED_DAILY",
                    "balance_quality": "SYNTHETIC",
                    "reported_month_end_balance": balance if day == last_day else "",
                    "reported_month_end_equity": balance if day == last_day else "",
                    "month_end_floating_pnl": 0, "source_file": f"SYNTHETIC-{month}",
                })
        # Match the baseline trade timestamps to cross-year account periods.
        mapping = dict(zip(("2024-01", "2024-02"), months))
        for name in ("transactions.csv", "completed_trades.csv"):
            rows = read_rows(self.output / name)
            for row in rows:
                for key, value in row.items():
                    for before, after in mapping.items():
                        if value.startswith(before):
                            row[key] = after + value[len(before):]
                            break
            write_csv(self.output / name, list(rows[0]), rows)
        write_csv(self.output / "daily_accounts.csv", DAILY_FIELDS,
                  list(reversed(daily)) if reverse else daily)
        with patch.object(sys, "argv", [
            "analyze_trading_style.py", "--input-dir", str(self.output),
            "--output-dir", str(self.output),
        ]), contextlib.redirect_stdout(io.StringIO()):
            style.main()
        monthly = read_rows(self.output / "monthly_performance.csv")
        yearly = read_rows(self.output / "yearly_performance.csv")
        summary = {row["指标"]: row["数值"]
                   for row in read_rows(self.output / "trading_style_summary.csv")}
        expected_first = 192 + profit - fee
        expected_total = 384 + profit - fee
        self.assertAlmostEqual(float(monthly[0]["账户实际收益"]), expected_first, places=7)
        self.assertAlmostEqual(float(monthly[1]["账户实际收益"]), 192, places=7)
        self.assertAlmostEqual(sum(float(row["账户实际收益"]) for row in monthly),
                               expected_total, places=7)
        self.assertAlmostEqual(sum(float(row["账户实际收益"]) for row in yearly),
                               expected_total, places=7)
        self.assertAlmostEqual(float(summary["累计账户实际收益"]), expected_total, places=7)
        self.assertAlmostEqual(float(summary["累计已实现净收益"]), expected_total, places=7)
        self.assertAlmostEqual(float(yearly[0]["期初权益"]), 100000, places=7)
        for year in yearly:
            matching = [row for row in monthly if row["月份"].startswith(year["年份"])]
            self.assertAlmostEqual(float(year["账户实际收益"]),
                                   sum(float(row["账户实际收益"]) for row in matching), places=7)

    def test_baseline_without_first_day_activity(self):
        self.check_case()

    def test_first_day_deposit_is_not_a_trading_loss(self):
        self.check_case(deposit=1000)

    def test_first_day_withdrawal_is_not_a_trading_profit(self):
        self.check_case(withdrawal=1000)

    def test_first_day_realized_profit_is_included(self):
        self.check_case(profit=200)

    def test_first_day_realized_loss_is_included(self):
        self.check_case(profit=-200)

    def test_first_day_fee_is_included(self):
        self.check_case(fee=12.34)

    def test_combined_first_day_movements(self):
        self.check_case(deposit=1000, withdrawal=300, profit=-50, fee=12.34)

    def test_reversed_daily_input_has_same_account_results(self):
        self.check_case(deposit=1000, profit=200, fee=12.34, reverse=True)

    def test_cross_year_monthly_annual_and_total_profits_agree(self):
        self.check_case(deposit=1000, months=("2023-12", "2024-01"))


if __name__ == "__main__":
    unittest.main()
