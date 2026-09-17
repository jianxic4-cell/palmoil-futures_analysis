"""Offline regression checks; no database, LSEG session, or private files needed."""
from pathlib import Path
import importlib
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from analyze_p_calendar_entry_exit import build_pair_series, prior_row
from analyze_p_main_liquid_far import choose_monthly_contracts
from backtest_p_calendar_v1 import contract_year_month
from demo import synthetic_history
from filter_p_calendar_spreads_pg import filter_candidates


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.history = synthetic_history()
        self.by_contract = dict(tuple(self.history.groupby("contract")))
        self.continuous = pd.DataFrame({"date": self.history["date"].drop_duplicates()})

    def test_imports_have_no_pipeline_side_effects(self):
        for name in ["parse_statements", "match_trades", "identify_arbitrage",
                     "analyze_trading_style", "import_to_pgsql",
                     "backtest_p_calendar_v2", "build_p_daily_research_panel"]:
            importlib.import_module(name)

    def test_contract_month(self):
        self.assertEqual(contract_year_month("P2701"), (2027, 1))
        self.assertEqual(contract_year_month("P2705"), (2027, 5))

    def test_liquidity_selects_four_month_far(self):
        selection = choose_monthly_contracts(self.history)
        self.assertTrue(selection["主力合约"].eq("P2005").all())
        self.assertTrue(selection["远月合约"].eq("P2009").all())
        self.assertTrue(selection["期限差月"].eq(4).all())
        self.assertTrue((selection["选择参考截止日"] < selection["月份"]).all())

    def test_current_month_volume_does_not_change_its_selection(self):
        before = choose_monthly_contracts(self.history)
        changed = self.history.copy()
        mask = changed["date"].ge("2020-02-01") & changed["contract"].eq("P2008")
        changed.loc[mask, "volume"] = 1_000_000
        after = choose_monthly_contracts(changed)
        feb = pd.Timestamp("2020-02-01")
        fields = ["主力合约", "远月合约", "期限差月"]
        pd.testing.assert_series_equal(
            before.loc[before["月份"].eq(feb), fields].iloc[0],
            after.loc[after["月份"].eq(feb), fields].iloc[0],
        )

    def test_percentage_spread_and_prior_event_date(self):
        pair = build_pair_series(self.by_contract, self.continuous, "P2005", "P2009")
        np.testing.assert_allclose(pair["价差比例"], pair["近月结算价"] / pair["远月结算价"] - 1)
        event_date = pair["date"].iloc[50]
        self.assertLess(prior_row(pair, event_date)["date"], event_date)
        self.assertIsNone(prior_row(pair, pair["date"].iloc[0]))
        self.assertTrue(pair["Z60"].iloc[:39].isna().all())

    def test_future_prices_do_not_change_past_features(self):
        original = build_pair_series(self.by_contract, self.continuous, "P2005", "P2009")
        changed = {key: value.copy() for key, value in self.by_contract.items()}
        cutoff = pd.Timestamp("2020-03-02")
        changed["P2005"].loc[changed["P2005"]["date"].ge(cutoff), "price"] *= 2
        revised = build_pair_series(changed, self.continuous, "P2005", "P2009")
        columns = ["date", "价差比例", "Z20", "Z60"]
        pd.testing.assert_frame_equal(original.loc[original["date"] < cutoff, columns],
                                      revised.loc[revised["date"] < cutoff, columns])

    def test_p_filter_excludes_cross_product_same_contract_and_missing_leg(self):
        base = {"A品种": "P", "B品种": "P", "A合约": "P2005", "B合约": "P2009",
                "A方向": "LONG", "B方向": "SHORT", "重叠天数": 2,
                "组合开始": "2020-02-03", "组合结束": "2020-02-05", "识别得分": 90}
        variants = [{}, {"B品种": "Y"}, {"B合约": "P2005"},
                    {"B方向": "LONG"}, {"重叠天数": 0}, {"B合约": None}]
        frame = pd.DataFrame([{**base, **change} for change in variants])
        result = filter_candidates(frame)
        self.assertEqual(result.index.tolist(), [0])
        self.assertEqual(len(frame), 6)  # input untouched


if __name__ == "__main__":
    unittest.main()

