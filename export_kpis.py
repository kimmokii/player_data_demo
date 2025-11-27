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
    ab_df.to_excel(writer, sheet_name="AB_retention", index=False)

print(f"KPI Excel generated: {OUTPUT_XLSX}")
