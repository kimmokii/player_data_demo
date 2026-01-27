import sqlite3
import pandas as pd

DB_PATH = "mock_game2.db"
OUTPUT_XLSX = "kpi_dashboard_data.xlsx"

conn = sqlite3.connect(DB_PATH)

# -----------------------------
#    DAU per day
# -----------------------------
dau_sql = """
SELECT
    substr(session_start_utc, 1, 10) AS day,   -- YYYY-MM-DD
    COUNT(DISTINCT player_id) AS dau
FROM sessions
GROUP BY day
ORDER BY day;
"""

dau_df = pd.read_sql_query(dau_sql, conn)

# -----------------------------
#    WAU per calendar week (YYYY-WW)
#    WAU = unique players per week
# -----------------------------
wau_sql = """
SELECT
    strftime('%Y-%W', session_start_utc) AS week,   -- e.g. 2025-03 (year-week)
    COUNT(DISTINCT player_id) AS wau
FROM sessions
GROUP BY week
ORDER BY week;
"""

wau_df = pd.read_sql_query(wau_sql, conn)

# -----------------------------
#    MAU per calendar month (YYYY-MM)
#    MAU = unique players per month
# -----------------------------
mau_sql = """
SELECT
    substr(session_start_utc, 1, 7) AS month,   -- YYYY-MM
    COUNT(DISTINCT player_id) AS mau
FROM sessions
GROUP BY month
ORDER BY month;
"""

mau_df = pd.read_sql_query(mau_sql, conn)

# -----------------------------
#    Revenue per day (EUR)
# -----------------------------
rev_sql = """
SELECT
    substr(purchase_time_utc, 1, 10) AS day,          -- YYYY-MM-DD
    SUM(price_eur) / 100.0 AS revenue_eur             -- cents -> euros
FROM purchases
GROUP BY day
ORDER BY day;
"""

rev_df = pd.read_sql_query(rev_sql, conn)

# -----------------------------
#    Revenue per day by variant (EUR)
# -----------------------------
# We use a day spine from sessions so days with zero purchases are included.
day_spine_sql = """
SELECT
    substr(session_start_utc, 1, 10) AS day
FROM sessions
GROUP BY day
ORDER BY day;
"""

day_spine_df = pd.read_sql_query(day_spine_sql, conn)

rev_by_variant_sql = """
SELECT
    substr(p.purchase_time_utc, 1, 10) AS day,
    ea.variant AS variant,
    SUM(p.price_eur) / 100.0 AS revenue_eur
FROM purchases p
JOIN experiment_assignments ea
  ON ea.player_id = p.player_id
GROUP BY day, variant
ORDER BY day, variant;
"""

rev_by_variant_long_df = pd.read_sql_query(rev_by_variant_sql, conn)

if not rev_by_variant_long_df.empty:
    rev_by_variant_wide_df = (
        rev_by_variant_long_df.pivot_table(
            index="day",
            columns="variant",
            values="revenue_eur",
            aggfunc="sum",
            fill_value=0.0,
        )
        .reset_index()
    )
else:
    rev_by_variant_wide_df = pd.DataFrame({"day": []})

# Ensure stable columns and ordering
for v in ["Control", "A", "B"]:
    if v not in rev_by_variant_wide_df.columns:
        rev_by_variant_wide_df[v] = 0.0
rev_by_variant_wide_df = rev_by_variant_wide_df[["day", "Control", "A", "B"]]

# Left join to the day spine so all days exist (fill missing with 0)
rev_by_variant_df = day_spine_df.merge(rev_by_variant_wide_df, on="day", how="left")
for v in ["Control", "A", "B"]:
    rev_by_variant_df[v] = rev_by_variant_df[v].fillna(0.0)

# Also apply the same day spine to total revenue
rev_df = day_spine_df.merge(rev_df, on="day", how="left")
rev_df["revenue_eur"] = rev_df["revenue_eur"].fillna(0.0)

# -----------------------------
#   A/B test results: D1/D7/D30 retention per experiment & variant
#
# - first_session: first day player ever played
# - activity: all (player_id, day) pairs
# - exp_cohorts: players joined to experiments (experiment_name, variant)
# - retention: cohort_size, day1/day7/day30 & ratios per cohort
# -----------------------------
ab_sql = """
WITH first_session AS (
    SELECT
        player_id,
        date(MIN(session_start_utc)) AS first_day
    FROM sessions
    GROUP BY player_id
),
activity AS (
    SELECT
        player_id,
        date(session_start_utc) AS day
    FROM sessions
),
exp_cohorts AS (
    SELECT
        ea.experiment_name,
        ea.variant,
        fs.player_id,
        fs.first_day
    FROM experiment_assignments ea
    JOIN first_session fs
      ON fs.player_id = ea.player_id
)
SELECT
    ec.experiment_name,
    ec.variant,
    ec.first_day AS cohort_first_day,
    COUNT(*) AS cohort_size,
    SUM(CASE WHEN a.day = date(ec.first_day, '+1 day') THEN 1 ELSE 0 END) AS day1_active,
    SUM(CASE WHEN a.day = date(ec.first_day, '+7 day') THEN 1 ELSE 0 END) AS day7_active,
    SUM(CASE WHEN a.day = date(ec.first_day, '+30 day') THEN 1 ELSE 0 END) AS day30_active,
    CASE WHEN COUNT(*) > 0
         THEN 1.0 * SUM(CASE WHEN a.day = date(ec.first_day, '+1 day') THEN 1 ELSE 0 END) / COUNT(*)
         ELSE NULL
    END AS d1_retention,
    CASE WHEN COUNT(*) > 0
         THEN 1.0 * SUM(CASE WHEN a.day = date(ec.first_day, '+7 day') THEN 1 ELSE 0 END) / COUNT(*)
         ELSE NULL
    END AS d7_retention,
    CASE WHEN COUNT(*) > 0
         THEN 1.0 * SUM(CASE WHEN a.day = date(ec.first_day, '+30 day') THEN 1 ELSE 0 END) / COUNT(*)
         ELSE NULL
    END AS d30_retention
FROM exp_cohorts ec
LEFT JOIN activity a
  ON a.player_id = ec.player_id
GROUP BY
    ec.experiment_name,
    ec.variant,
    ec.first_day
ORDER BY
    ec.experiment_name,
    ec.variant,
    ec.first_day;
"""

ab_df = pd.read_sql_query(ab_sql, conn)

conn.close()

# -----------------------------
#  Write everything to a single excel file but to different sheets
# -----------------------------
with pd.ExcelWriter(OUTPUT_XLSX, engine="xlsxwriter") as writer:
    dau_df.to_excel(writer, sheet_name="DAU_daily", index=False)
    wau_df.to_excel(writer, sheet_name="WAU_weekly", index=False)
    mau_df.to_excel(writer, sheet_name="MAU_monthly", index=False)
    rev_df.to_excel(writer, sheet_name="Revenue_daily", index=False)
    rev_by_variant_df.to_excel(writer, sheet_name="Revenue_by_variant_daily", index=False)
    ab_df.to_excel(writer, sheet_name="AB_retention", index=False)

print(f"KPI Excel generated: {OUTPUT_XLSX}")
