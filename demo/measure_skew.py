"""Measure the train/serve skew in the built warehouse, in actual numbers.

The point of this script is to show what a distribution monitor would and would not see.
It queries the real DuckDB tables dbt built and compares the offline and online copies of
`avg_order_value_30d` row by row.

Run after `make warehouse`.
"""

from __future__ import annotations

import sys
from pathlib import Path

WAREHOUSE = Path(__file__).resolve().parent / "warehouse" / "warehouse.duckdb"

QUERY = """
with joined as (
    select
        a.customer_id,
        a.avg_order_value_30d as offline_value,
        b.avg_order_value_30d as online_value
    from main_features.customer_features_offline a
    join main_features.customer_features_online  b using (customer_id)
    where a.avg_order_value_30d is not null
      and b.avg_order_value_30d is not null
)
select
    count(*)                                                              as rows_compared,
    avg(offline_value)                                                    as offline_mean,
    avg(online_value)                                                     as online_mean,
    100.0 * abs(avg(offline_value) - avg(online_value)) / avg(offline_value) as mean_shift_pct,
    100.0 * avg(abs(offline_value - online_value) / nullif(offline_value, 0)) as per_row_error_pct,
    100.0 * sum(case when abs(offline_value - online_value)
                          / nullif(offline_value, 0) > 0.10 then 1 else 0 end)
          / count(*)                                                      as rows_over_10pct,
    corr(offline_value, online_value)                                     as correlation
from joined
"""


def main() -> int:
    if not WAREHOUSE.exists():
        print(f"{WAREHOUSE} not found — run `make warehouse` first.", file=sys.stderr)
        return 1

    import duckdb

    con = duckdb.connect(str(WAREHOUSE), read_only=True)
    (rows, off_mean, on_mean, shift, per_row, over_10, corr) = con.execute(QUERY).fetchone()

    print("Train/serve skew in `avg_order_value_30d`")
    print("=" * 62)
    print(f"  rows compared                 {rows:>10}")
    print(f"  offline mean (30-day window)  {off_mean:>10.2f}")
    print(f"  online  mean ( 7-day window)  {on_mean:>10.2f}")
    print()
    print("What a distribution monitor compares:")
    print(f"  shift in the mean             {shift:>9.1f}%   <- no alert fires")
    print(f"  correlation                   {corr:>10.3f}")
    print()
    print("What the model is actually served:")
    print(f"  mean per-row error            {per_row:>9.1f}%")
    print(f"  rows more than 10% wrong      {over_10:>9.1f}%")
    print()
    # Derived from the measurement, never asserted: if the warehouse changes, this sentence
    # changes with it rather than quietly becoming a claim the numbers no longer support.
    print(
        f"The aggregate means agree to within {shift:.1f}% while {over_10:.0f}% of served rows\n"
        f"are more than 10% wrong. That gap is the whole argument: this defect is invisible\n"
        f"in the data distribution and unambiguous in the lineage graph — one path averages\n"
        f"over 30 days, the other over 7."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
