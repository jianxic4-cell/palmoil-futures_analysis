"""Build the arbitrage-candidate Excel report from identify_arbitrage.py outputs."""
from report_workbook import report_cli

if __name__ == "__main__":
    report_cli("arbitrage")
