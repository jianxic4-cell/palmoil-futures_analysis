"""Local-only configuration: no credentials or datasets are bundled."""
import os
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
# An optional external working root must have data/ and output/ subdirectories.
PROJECT_ROOT = Path(os.environ.get("FUTURES_WORK_ROOT", str(REPOSITORY_ROOT))).expanduser().resolve()
RAW_STATEMENTS_DIR = Path(os.environ.get(
    "FUTURES_STATEMENTS_DIR", str(PROJECT_ROOT / "data" / "raw")
)).expanduser().resolve()
MARKET_DATA_DIR = Path(os.environ.get(
    "FUTURES_MARKET_DIR", str(PROJECT_ROOT / "data" / "market")
)).expanduser().resolve()

