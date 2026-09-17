"""Database-import boundaries tested entirely offline with synthetic CSVs/mocks."""
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
from sqlalchemy import Integer, Text, VARCHAR

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import import_to_pgsql as importer


class DatabaseImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def frame(self, content):
        path = self.root / "sample.csv"
        path.write_text(content, encoding="utf-8-sig")
        return importer.read_import_csv(path)

    def test_english_identifier_leading_zeros(self):
        frame = self.frame("account_id,entry_trade_id,exit_trade_id\n000001,00001234,00005678\n")
        self.assertEqual(frame.iloc[0].tolist(), ["000001", "00001234", "00005678"])

    def test_chinese_identifier_leading_zeros(self):
        frame = self.frame("账户,候选编号,A区间ID,B区间ID,成交序号\n000001,000010,000020,000030,000040\n")
        self.assertEqual(frame.iloc[0].tolist(), ["000001", "000010", "000020", "000030", "000040"])

    def test_long_identifiers_do_not_lose_precision(self):
        identifier = "0012345678901234567890123456789"
        frame = self.frame(f"account_id,entry_trade_id\n{identifier},{identifier}\n")
        self.assertEqual(frame.iloc[0].tolist(), [identifier, identifier])

    def test_literal_na_and_null_identifiers_are_not_missing(self):
        frame = self.frame("account_id,quantity\nNA,1\nNULL,2\nNaN,3\n")
        self.assertEqual(frame["account_id"].tolist(), ["NA", "NULL", "NaN"])

    def test_empty_identifiers_remain_null(self):
        frame = self.frame("account_id,quantity\n,1\n,2\n")
        self.assertTrue(frame["account_id"].isna().all())

    def test_amounts_and_quantities_remain_numeric(self):
        frame = self.frame("account_id,quantity,fee,net_pnl_after_fees\n000001,2,1.25,-15.5\n")
        for name in ("quantity", "fee", "net_pnl_after_fees"):
            self.assertTrue(pd.api.types.is_numeric_dtype(frame[name]), name)
        self.assertEqual(frame["fee"].iloc[0], 1.25)

    def cli_files(self):
        (self.root / "completed_trades.csv").write_text(
            "account_id,entry_trade_id,quantity,fee\n000001,00001234,2,1.25\n,00005678,3,\n",
            encoding="utf-8-sig")
        (self.root / "arbitrage_candidates.csv").write_text(
            "候选编号,A区间ID,B区间ID,识别得分\n000010,000020,,90\n",
            encoding="utf-8-sig")

    def test_cli_without_write_does_not_connect(self):
        with patch.object(sys, "argv", ["import_to_pgsql.py"]), patch.object(
            importer, "database_engine") as factory, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                importer.main()
        factory.assert_not_called()

    def test_cli_passes_preserved_values_and_explicit_text_types_to_sql(self):
        self.cli_files()
        engine = MagicMock()
        with patch.object(sys, "argv", ["import_to_pgsql.py", "--write", "--input-dir", str(self.root)]), patch.object(
            importer, "database_engine", return_value=engine), patch.object(
            pd.DataFrame, "to_sql", autospec=True) as to_sql, contextlib.redirect_stdout(io.StringIO()):
            importer.main()
        self.assertEqual(to_sql.call_count, 2)
        completed = to_sql.call_args_list[0].args[0]
        candidates = to_sql.call_args_list[1].args[0]
        self.assertEqual(completed["account_id"].iloc[0], "000001")
        self.assertEqual(candidates["候选编号"].iloc[0], "000010")
        self.assertTrue(candidates["B区间ID"].isna().all())
        for call in to_sql.call_args_list:
            for column in importer.identifier_columns(call.args[0].columns):
                self.assertIsInstance(call.kwargs["dtype"][column], Text)
            self.assertEqual(call.kwargs["if_exists"], "fail")
        engine.dispose.assert_called_once()

    def test_numeric_existing_identifier_is_rejected(self):
        inspector = MagicMock()
        inspector.has_table.return_value = True
        inspector.get_columns.return_value = [{"name": "account_id", "type": Integer()}]
        with patch.object(importer, "inspect", return_value=inspector):
            with self.assertRaisesRegex(ValueError, "account_id"):
                importer.check_existing_identifier_types(object(), {
                    "completed_trades": pd.DataFrame({"account_id": ["000001"]})})

    def test_existing_text_identifiers_and_new_tables_are_allowed(self):
        inspector = MagicMock()
        inspector.has_table.side_effect = [True, False]
        inspector.get_columns.return_value = [
            {"name": "account_id", "type": Text()}, {"name": "entry_trade_id", "type": VARCHAR(32)}]
        with patch.object(importer, "inspect", return_value=inspector):
            importer.check_existing_identifier_types(object(), {
                "completed_trades": pd.DataFrame({"account_id": ["000001"], "entry_trade_id": ["00002"]}),
                "arbitrage_candidates": pd.DataFrame({"候选编号": ["00003"]})})

    def test_append_checks_both_tables_before_any_write(self):
        self.cli_files()
        inspector = MagicMock()
        inspector.has_table.return_value = True
        inspector.get_columns.side_effect = [
            [{"name": "account_id", "type": Text()}, {"name": "entry_trade_id", "type": Text()}],
            [{"name": "候选编号", "type": Integer()}],
        ]
        engine = MagicMock()
        with patch.object(sys, "argv", ["import_to_pgsql.py", "--write", "--if-exists", "append",
                                      "--input-dir", str(self.root)]), patch.object(
            importer, "database_engine", return_value=engine), patch.object(
            importer, "inspect", return_value=inspector), patch.object(pd.DataFrame, "to_sql") as write:
            with self.assertRaisesRegex(ValueError, "候选编号"):
                importer.main()
        write.assert_not_called()
        engine.dispose.assert_called_once()


if __name__ == "__main__":
    unittest.main()
