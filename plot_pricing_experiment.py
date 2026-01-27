#!/usr/bin/env python3
"""Generate a plot highlighting the shop_pricing_v1 Control/A/B behavior.

The pricing experiment in this repo affects only a subset of purchases
("Category X") via:
- a price multiplier per variant
- a conversion multiplier per variant and spend segment

This script reads the SQLite DB and produces a PNG that makes the effect
visible:
1) Category X purchases per 1k assigned players by variant

Usage:
  python plot_pricing_experiment.py
  python plot_pricing_experiment.py --db mock_game2.db --out pricing_experiment_effects.png

Dependencies:
  pip install pandas matplotlib
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pandas as pd


def _read_category_x_summary(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT
            ea.variant AS variant,
            COUNT(*) AS purchases,
            COUNT(DISTINCT p.player_id) AS buyers,
            SUM(p.price_eur) / 100.0 AS revenue_eur,
            AVG(p.price_eur) / 100.0 AS avg_price_eur
        FROM purchases p
        JOIN experiment_assignments ea
          ON ea.player_id = p.player_id
         AND ea.experiment_name = 'shop_pricing_v1'
        WHERE p.product_type = 'CategoryX'
        GROUP BY ea.variant
        ORDER BY ea.variant;
        """,
        conn,
    )


def _read_assignment_sizes(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT
            variant,
            COUNT(*) AS assigned_players
        FROM experiment_assignments
        WHERE experiment_name = 'shop_pricing_v1'
        GROUP BY variant
        ORDER BY variant;
        """,
        conn,
    )


def _variant_order_key(variant: str) -> int:
    order = {"Control": 0, "A": 1, "B": 2}
    return order.get(str(variant), 99)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plot shop_pricing_v1 experiment effects (Category X only)."
    )
    parser.add_argument(
        "--db",
        default="mock_game2.db",
        help="Path to SQLite database (default: mock_game2.db)",
    )
    parser.add_argument(
        "--out",
        default="pricing_experiment_effects.png",
        help="Output PNG path (default: pricing_experiment_effects.png)",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    out_path = Path(args.out)

    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        summary = _read_category_x_summary(conn)
        assigned = _read_assignment_sizes(conn)
    finally:
        conn.close()

    if summary.empty:
        print(
            "No CategoryX purchases found. Run generate_mock_data.py first (or increase CATEGORY_X_SHARE)."
        )
        return 0

    # Normalize and sort variants
    summary["variant"] = summary["variant"].astype(str)
    assigned["variant"] = assigned["variant"].astype(str)
    assigned_map = dict(zip(assigned["variant"], assigned["assigned_players"]))

    summary = summary.merge(assigned, on="variant", how="left")
    summary["assigned_players"] = summary["assigned_players"].fillna(0).astype(int)
    denom = summary["assigned_players"].replace(0, pd.NA)
    summary["purchases_per_1k_assigned"] = (summary["purchases"] / denom) * 1000.0
    summary = summary.sort_values(by="variant", key=lambda s: s.map(_variant_order_key))

    # Plot
    import matplotlib.pyplot as plt

    plt.style.use("seaborn-v0_8-whitegrid")

    variant_colors = {
        "Control": "#1f77b4",  # blue
        "A": "#ff7f0e",        # orange
        "B": "#2ca02c",        # green
    }

    fig, ax_cnt = plt.subplots(figsize=(10, 5), constrained_layout=True)

    # Summary: purchases per 1k assigned
    x = summary["variant"].tolist()
    ax_cnt.bar(
        x,
        summary["purchases_per_1k_assigned"].tolist(),
        color=[variant_colors.get(v) for v in x],
    )
    ax_cnt.set_title("Category X purchases per 1k assigned")
    ax_cnt.set_ylabel("Purchases")

    cohort_note = ", ".join(
        f"{v}: {assigned_map.get(v, 0)}" for v in ["Control", "A", "B"] if v in assigned_map
    )
    fig.suptitle(
        "Pricing experiment effect (Category X only)\n"
        "Control = baseline, A = expensive, B = cheap\n"
        f"Assigned players: {cohort_note}",
        fontsize=14,
    )

    fig.savefig(out_path, dpi=160)
    print(f"Wrote: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
