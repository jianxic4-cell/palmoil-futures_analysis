"""Input safety regression tests using temporary, synthetic statements only."""
import contextlib
import csv
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import parse_statements


class StatementSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root / "raw"
        self.raw.mkdir()
        self.output = self.root / "output"
        self.output.mkdir()
        self.previous = {}
        for name in ("transactions.csv", "daily_accounts.csv", "parsing_errors.csv"):
            payload = f"existing-output,{name}\n".encode()
            (self.output / name).write_bytes(payload)
            self.previous[name] = payload

    def statement(self, folder="a", month="202401", active=False):
        path = self.raw / folder / f"000001-{month}账单.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        profit, fee = (100, 4) if active else (0, 0)
        balance = 100000 + profit - fee
        lines = [f"{label}: {value}" for label, value in [
            ("期初结存", 100000), ("期间入金", 0), ("期间出金", 0), ("期间出入金合计", 0),
            ("平仓盈亏", profit), ("期间手续费", fee), ("期末结存", balance),
            ("资金权益", balance), ("浮动持仓盈亏", 0),
        ]]
        lines.append("成交日期 成交时间")
        if active:
            lines.extend([
                f"{month}10 09:00:00 DCE P2405 111 买 投机 8000 1 80000 开 2 0 0 0",
                f"{month}11 09:00:00 DCE P2405 222 卖 投机 8010 1 80100 平 2 100 0 0",
            ])
        path.write_text("\n".join(lines), encoding="gb18030")
        return path

    def run_cli(self, source=None, year=2024):
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "parse_statements.py"),
             "--source", str(source if source is not None else self.raw),
             "--output", str(self.output), "--year", str(year)],
            capture_output=True, text=True, encoding="utf-8",
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8"),
        )

    def assert_unchanged(self):
        for name, payload in self.previous.items():
            self.assertEqual((self.output / name).read_bytes(), payload, name)

    def assert_blocked(self, result):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assert_unchanged()

    def test_nonexistent_directory_preserves_previous_outputs(self):
        self.assert_blocked(self.run_cli(self.root / "missing"))

    def test_file_instead_of_directory_preserves_previous_outputs(self):
        self.assert_blocked(self.run_cli(self.statement()))

    def test_empty_directory_preserves_previous_outputs(self):
        self.assert_blocked(self.run_cli())

    def test_wrong_year_preserves_previous_outputs(self):
        self.statement()
        self.assert_blocked(self.run_cli(year=2025))

    def test_all_statements_fail_preserves_previous_outputs(self):
        (self.raw / "000001-202401账单.txt").write_text("invalid statement", encoding="gb18030")
        self.assert_blocked(self.run_cli())

    def test_duplicate_txt_month_preserves_previous_outputs(self):
        self.statement(folder="a", active=True)
        self.statement(folder="b", active=False)
        result = self.run_cli()
        self.assert_blocked(result)
        self.assertIn("存在多份账单", result.stderr)

    def duplicate_xlsx(self, mixed):
        # Stub only the XLSX reader/metadata. No real XLSX files or accounts are required.
        if mixed:
            self.statement()
        else:
            (self.raw / "first.xlsx").touch()
        (self.raw / "second.xlsx").touch()
        argv = ["parse_statements.py", "--source", str(self.raw), "--output", str(self.output), "--year", "2024"]
        with patch.object(sys, "argv", argv), patch.object(parse_statements, "read_xlsx", return_value=object()), patch.object(
            parse_statements, "statement_metadata", return_value=("2024-01", "000001")
        ), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                parse_statements.main()
        self.assertEqual(raised.exception.code, 2)
        self.assert_unchanged()

    def test_duplicate_xlsx_month_preserves_previous_outputs(self):
        self.duplicate_xlsx(mixed=False)

    def test_txt_and_xlsx_same_month_preserves_previous_outputs(self):
        self.duplicate_xlsx(mixed=True)

    def test_valid_zero_trade_statement_is_allowed(self):
        self.statement()
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        with (self.output / "transactions.csv").open(encoding="utf-8-sig") as handle:
            self.assertEqual(list(csv.DictReader(handle)), [])
        with (self.output / "daily_accounts.csv").open(encoding="utf-8-sig") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 31)

    def test_valid_different_months_are_processed(self):
        self.statement(month="202401", active=True)
        self.statement(month="202402", active=True)
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        with (self.output / "transactions.csv").open(encoding="utf-8-sig") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 4)


if __name__ == "__main__":
    unittest.main()
