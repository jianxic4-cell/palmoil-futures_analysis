"""Create anonymized XLSX copies without modifying the broker originals."""

from __future__ import annotations

from project_config import PROJECT_ROOT, RAW_STATEMENTS_DIR, MARKET_DATA_DIR

import argparse
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from xlsx_reader import cell, find_header, find_value_after_label, normalized, read_xlsx


def sensitive_replacements(source: Path) -> dict[str, str]:
    workbook = read_xlsx(source)
    main = workbook.sheet("客户交易结算月报")
    replacements: dict[str, str] = {}
    account = normalized(find_value_after_label(main, "客户期货期权内部资金账户", 10))
    customer = normalized(find_value_after_label(main, "客户名称", 10))
    securities = normalized(find_value_after_label(main, "客户证券现货内部资金账户", 10))
    if account and account != "0":
        replacements[account] = "ACCOUNT_REDACTED"
    if customer:
        replacements[customer] = "CUSTOMER_REDACTED"
    if securities and securities != "0":
        replacements[securities] = "SECURITIES_ACCOUNT_REDACTED"
    try:
        rows = workbook.sheet("持仓明细")
        header_index, columns = find_header(rows, {"交易编码"})
        codes = sorted({normalized(cell(row, columns["交易编码"])) for row in rows[header_index + 1 :] if normalized(cell(row, columns["交易编码"]))})
        for index, code in enumerate(codes, 1):
            replacements[code] = f"TRADING_CODE_{index:03d}"
    except (KeyError, ValueError):
        pass
    return replacements


def transform_text(text: str, replacements: dict[str, str]) -> str:
    result = text
    for original, replacement in replacements.items():
        result = result.replace(original, replacement)
    result = re.sub(r"(转账账号:)\S+", r"\1REDACTED", result)
    return result


def anonymize_xlsx(source: Path, destination: Path) -> None:
    replacements = sensitive_replacements(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        temp_path = Path(temporary) / "anonymized.xlsx"
        with zipfile.ZipFile(source, "r") as reader, zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as writer:
            for item in reader.infolist():
                payload = reader.read(item.filename)
                if item.filename.endswith(".xml"):
                    try:
                        root = ET.fromstring(payload)
                        changed = False
                        for node in root.iter():
                            if node.text:
                                new_text = transform_text(node.text, replacements)
                                if new_text != node.text:
                                    node.text = new_text
                                    changed = True
                        if changed:
                            payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
                    except ET.ParseError:
                        pass
                writer.writestr(item, payload)
        shutil.copy2(temp_path, destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=RAW_STATEMENTS_DIR)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "output/anonymized_statements")
    parser.add_argument("--start-year", type=int, default=2026)
    parser.add_argument("--end-year", type=int, default=2026)
    args = parser.parse_args()
    count = 0
    for source in sorted(args.source.rglob("*.xlsx")):
        match = re.search(r"(20\d{2})-(\d{2})", source.name)
        if not match or not args.start_year <= int(match.group(1)) <= args.end_year:
            continue
        destination = args.output / match.group(1) / f"statement_{match.group(1)}-{match.group(2)}.xlsx"
        anonymize_xlsx(source, destination)
        count += 1
    print(f"Created {count} anonymized statements in {args.output.resolve()}")


if __name__ == "__main__":
    main()
