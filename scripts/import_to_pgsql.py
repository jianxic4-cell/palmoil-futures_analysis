"""Import CSVs explicitly, preserving account and trade identifiers as text."""
import argparse
from pathlib import Path

import pandas as pd
from sqlalchemy import String, Text, inspect
from database import database_engine
from project_config import PROJECT_ROOT


def identifier_columns(columns):
    """Recognize pipeline identifier fields, not monetary amounts or quantities."""
    names = {"账户", "账号", "合约", "品种", "A合约", "B合约", "A品种", "B品种",
             "account", "contract", "product", "id"}
    return [
        column for column in columns
        if column in names or column.lower().endswith("_id")
        or column.endswith(("ID", "编号", "序号", "账号", "账户"))
    ]


def read_import_csv(path):
    columns = pd.read_csv(path, encoding="utf-8-sig", nrows=0).columns
    # Converters preserve literal identifiers such as "000001", "NA", and "NULL".
    # Empty identifiers become SQL NULL; other fields retain numeric inference.
    converters = {column: lambda value: value if value != "" else None
                  for column in identifier_columns(columns)}
    return pd.read_csv(path, encoding="utf-8-sig", low_memory=False, converters=converters)


def check_existing_identifier_types(connection, tables):
    """Reject incompatible append targets before writing either table."""
    inspector = inspect(connection)
    for name, frame in tables.items():
        if not inspector.has_table(name, schema="strategy"):
            continue
        existing = {column["name"]: column["type"]
                    for column in inspector.get_columns(name, schema="strategy")}
        incompatible = [
            column for column in identifier_columns(frame.columns)
            if column in existing and not isinstance(existing[column], String)
        ]
        if incompatible:
            raise ValueError(
                f"strategy.{name}: identifier columns must be TEXT/VARCHAR before append: "
                + ", ".join(incompatible)
                + ". No table was changed by this import. Review the schema separately; "
                  "zeros already lost cannot be recovered without the original CSV."
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=PROJECT_ROOT / "output")
    parser.add_argument("--write", action="store_true", help="Required: acknowledge database writes")
    parser.add_argument("--if-exists", choices=["fail", "append"], default="fail")
    args = parser.parse_args()
    if not args.write:
        parser.error("No database changes made. Add --write to import; existing tables fail by default.")
    tables = {
        name: read_import_csv(args.input_dir / f"{name}.csv")
        for name in ("completed_trades", "arbitrage_candidates")
    }
    engine = database_engine()
    try:
        # Both imports commit together, or both roll back.
        with engine.begin() as connection:
            if args.if_exists == "append":
                check_existing_identifier_types(connection, tables)
            for name, frame in tables.items():
                frame.to_sql(name, connection, schema="strategy", if_exists=args.if_exists,
                             index=False, chunksize=1000,
                             dtype={column: Text() for column in identifier_columns(frame.columns)})
                print(f"{name}: {len(frame)} rows")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
