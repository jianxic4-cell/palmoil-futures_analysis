"""Small dependency-free XLSX reader for the broker statement exports.

It intentionally implements only the OOXML features needed by these statements:
shared/inline strings, numbers, booleans, formulas' cached values, and sheet lookup.
"""

from __future__ import annotations

from project_config import PROJECT_ROOT, RAW_STATEMENTS_DIR, MARKET_DATA_DIR

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
import re
import zipfile


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _column_index(cell_reference: str) -> int:
    match = re.match(r"([A-Z]+)", cell_reference.upper())
    if not match:
        raise ValueError(f"Invalid cell reference: {cell_reference}")
    value = 0
    for char in match.group(1):
        value = value * 26 + ord(char) - 64
    return value - 1


def _all_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(node.text or "" for node in element.iter() if node.tag.endswith("}t"))


def _coerce_number(text: str | None) -> int | float | None:
    if text in (None, ""):
        return None
    try:
        number = float(text)
    except ValueError:
        return text
    return int(number) if number.is_integer() else number


@dataclass
class XlsxWorkbook:
    path: Path
    sheets: dict[str, list[list[Any]]]

    def sheet(self, name: str) -> list[list[Any]]:
        if name not in self.sheets:
            raise KeyError(f"Worksheet not found: {name}; available={list(self.sheets)}")
        return self.sheets[name]


def read_xlsx(path: str | Path) -> XlsxWorkbook:
    source = Path(path)
    with zipfile.ZipFile(source) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared_strings = [_all_text(item) for item in root.findall(f"{{{MAIN_NS}}}si")]

        workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
        rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relationships = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in rels_root.findall(f"{{{PKG_REL_NS}}}Relationship")
        }

        sheets: dict[str, list[list[Any]]] = {}
        sheet_nodes = workbook_root.find(f"{{{MAIN_NS}}}sheets")
        if sheet_nodes is None:
            return XlsxWorkbook(source, sheets)

        for sheet_node in sheet_nodes:
            name = sheet_node.attrib["name"]
            relationship_id = sheet_node.attrib[f"{{{REL_NS}}}id"]
            target = relationships[relationship_id].replace("\\", "/")
            worksheet_path = target.lstrip("/") if target.startswith("/xl/") else f"xl/{target.lstrip('/')}"
            worksheet_root = ET.fromstring(archive.read(worksheet_path))
            rows: list[list[Any]] = []
            sheet_data = worksheet_root.find(f"{{{MAIN_NS}}}sheetData")
            if sheet_data is None:
                sheets[name] = rows
                continue

            for row_node in sheet_data.findall(f"{{{MAIN_NS}}}row"):
                row_number = int(row_node.attrib.get("r", len(rows) + 1))
                while len(rows) < row_number - 1:
                    rows.append([])
                row_values: list[Any] = []
                for cell in row_node.findall(f"{{{MAIN_NS}}}c"):
                    index = _column_index(cell.attrib["r"])
                    while len(row_values) <= index:
                        row_values.append(None)
                    cell_type = cell.attrib.get("t")
                    value_node = cell.find(f"{{{MAIN_NS}}}v")
                    if cell_type == "s":
                        value = shared_strings[int(value_node.text)] if value_node is not None else ""
                    elif cell_type == "inlineStr":
                        value = _all_text(cell.find(f"{{{MAIN_NS}}}is"))
                    elif cell_type == "b":
                        value = value_node is not None and value_node.text == "1"
                    elif cell_type in {"str", "e"}:
                        value = value_node.text if value_node is not None else ""
                    else:
                        value = _coerce_number(value_node.text if value_node is not None else None)
                    row_values[index] = value
                while row_values and row_values[-1] is None:
                    row_values.pop()
                rows.append(row_values)
            sheets[name] = rows
    return XlsxWorkbook(source, sheets)


def cell(row: list[Any], index: int) -> Any:
    return row[index] if index < len(row) else None


def normalized(value: Any) -> str:
    return str(value if value is not None else "").strip()


def find_header(rows: list[list[Any]], required_labels: set[str]) -> tuple[int, dict[str, int]]:
    for row_index, row in enumerate(rows):
        labels = {normalized(value): index for index, value in enumerate(row) if normalized(value)}
        if required_labels.issubset(labels):
            return row_index, labels
    raise ValueError(f"Could not locate header containing: {sorted(required_labels)}")


def find_value_after_label(rows: list[list[Any]], label: str, limit: int | None = None) -> Any:
    for row in rows[:limit]:
        for index, value in enumerate(row):
            if normalized(value) == label:
                for candidate in row[index + 1 :]:
                    if candidate not in (None, ""):
                        return candidate
    return None
