"""Export P/P calendar-spread candidates from a CSV or a read-only database query."""
import argparse
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from database import database_engine
from project_config import PROJECT_ROOT

QUERY = """
SELECT * FROM strategy.arbitrage_candidates
WHERE "A品种" = 'P' AND "B品种" = 'P'
  AND "A合约" <> "B合约" AND "A方向" <> "B方向" AND "重叠天数" > 0
ORDER BY "组合开始", "组合结束", "识别得分" DESC
"""


def filter_candidates(frame):
    required = ["A品种", "B品种", "A合约", "B合约", "A方向", "B方向", "重叠天数"]
    missing = set(required) - set(frame.columns)
    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")
    complete = frame[required].notna().all(axis=1)
    mask = (complete & frame["A品种"].eq("P") & frame["B品种"].eq("P")
            & frame["A合约"].ne(frame["B合约"]) & frame["A方向"].ne(frame["B方向"])
            & pd.to_numeric(frame["重叠天数"], errors="coerce").gt(0))
    result = frame.loc[mask].copy()
    return result.sort_values(["组合开始", "组合结束", "识别得分"], ascending=[True, True, False])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--csv", type=Path, default=PROJECT_ROOT / "output" / "arbitrage_candidates.csv")
    source.add_argument("--from-db", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "output" / "P跨期套利")
    args = parser.parse_args()
    if args.from_db:
        engine = database_engine()
        try:
            with engine.connect() as connection:
                with connection.begin():
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                    result = pd.read_sql_query(text(QUERY), connection)
        finally:
            engine.dispose()
    else:
        result = filter_candidates(pd.read_csv(args.csv, encoding="utf-8-sig", low_memory=False))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output_dir / "P跨期套利全部组合.csv", index=False, encoding="utf-8-sig")
    result.to_excel(args.output_dir / "P跨期套利全部组合.xlsx", index=False)
    print(f"{len(result)} candidates exported to {args.output_dir}. No database changes.")


if __name__ == "__main__":
    main()

