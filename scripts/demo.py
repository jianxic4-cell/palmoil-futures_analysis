"""Offline demonstration using entirely synthetic prices, not private trading records."""
from pathlib import Path

import numpy as np
import pandas as pd

from analyze_p_calendar_entry_exit import build_pair_series
from analyze_p_main_liquid_far import choose_monthly_contracts
from project_config import REPOSITORY_ROOT


def synthetic_history():
    dates = pd.bdate_range("2020-01-02", "2020-04-30")
    step = np.arange(len(dates), dtype=float)
    rows = []
    for contract, premium, volume in [
        ("P2005", 0, 5000), ("P2008", 80, 600),
        ("P2009", 110, 3000), ("P2010", 160, 1200),
    ]:
        prices = 6000 + 2 * step + premium + (25 if premium else 90) * np.sin(step / 7)
        for date, price in zip(dates, prices):
            rows.append({"date": date, "contract": contract, "price": price,
                         "volume": volume, "open_interest": 10000})
    return pd.DataFrame(rows)


def main():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter

    output = REPOSITORY_ROOT / "output" / "synthetic_demo"
    output.mkdir(parents=True, exist_ok=True)
    history = synthetic_history()
    selection = choose_monthly_contracts(history)
    by_contract = {contract: frame for contract, frame in history.groupby("contract")}
    continuous = pd.DataFrame({"date": history["date"].drop_duplicates().sort_values()})
    pair = build_pair_series(by_contract, continuous, "P2005", "P2009")
    selection.to_csv(output / "synthetic_monthly_selection.csv", index=False, encoding="utf-8-sig")
    pair.to_csv(output / "synthetic_spread_features.csv", index=False, encoding="utf-8-sig")
    fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    axes[0].plot(pair["date"], pair["价差比例"])
    axes[0].yaxis.set_major_formatter(PercentFormatter(1))
    axes[0].set_ylabel("Near / far - 1")
    axes[0].set_title("SYNTHETIC DATA ONLY — P2005 / P2009")
    axes[1].plot(pair["date"], pair["Z60"])
    axes[1].axhline(0, color="grey", linewidth=0.7)
    axes[1].set_ylabel("60-observation Z-score")
    for ax in axes:
        ax.grid(alpha=0.2)
    fig.tight_layout()
    fig.savefig(output / "synthetic_spread.png", dpi=130)
    plt.close(fig)
    print(f"Synthetic demo: {len(history)} price rows, {len(selection)} monthly selections.")
    print(f"Saved to {output}. Not historical evidence or backtest performance.")


if __name__ == "__main__":
    main()

