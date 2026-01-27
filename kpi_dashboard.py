from pathlib import Path
import calendar
from statistics import mean

from openpyxl import load_workbook
from openpyxl.chart import BarChart, LineChart, Reference

# --------------------------------------------------------------------
# Load source workbook with KPI data
# --------------------------------------------------------------------
src_path = Path.cwd() / "kpi_dashboard_data.xlsx"
wb = load_workbook(src_path)

# Expected sheets
dau_ws = wb["DAU_daily"]        # columns: day (A), dau (B)
wau_ws = wb["WAU_weekly"]       # columns: week (A), wau (B)
mau_ws = wb["MAU_monthly"]      # columns: month_yyyy_mm (A), mau (B)
rev_ws = wb["Revenue_daily"]    # columns: day (A), revenue_eur (B)
rev_var_ws = wb["Revenue_by_variant_daily"]  # columns: day (A), Control (B), A (C), B (D)
ab_ws = wb["AB_retention"]      # raw AB data


# --------------------------------------------------------------------
# 1) Build AB-retention summary: average D1 retention per variant
# --------------------------------------------------------------------
variants = ["Control", "A", "B"]

if "AB_summary" in wb.sheetnames:
    wb.remove(wb["AB_summary"])
ab_sum_ws = wb.create_sheet("AB_summary")

ab_sum_ws["A1"] = "variant"
ab_sum_ws["B1"] = "avg_d1_retention"

variant_to_d1_values: dict[str, list[float]] = {v: [] for v in variants}
variant_key = {v.lower(): v for v in variants}

# AB_retention columns (from export_kpis.py):
# A experiment_name, B variant, ... H d1_retention
for row in ab_ws.iter_rows(min_row=2, values_only=True):
    variant = row[1]
    d1_retention = row[7]
    variant_norm = str(variant).strip().lower() if variant is not None else ""
    canonical_variant = variant_key.get(variant_norm)
    if canonical_variant is not None and d1_retention is not None:
        try:
            variant_to_d1_values[canonical_variant].append(float(d1_retention))
        except (TypeError, ValueError):
            continue

for i, v in enumerate(variants, start=2):
    ab_sum_ws[f"A{i}"] = v
    values = variant_to_d1_values[v]
    ab_sum_ws[f"B{i}"] = mean(values) if values else None


# --------------------------------------------------------------------
# 2) Add month name column to MAU sheet (Jan, Feb, ...)
#    A: '2025-01' style, B: MAU, C: 'Jan', 'Feb', ...
# --------------------------------------------------------------------
mau_ws["C1"] = "month_name"
for row in range(2, mau_ws.max_row + 1):
    ym = str(mau_ws[f"A{row}"].value)  # e.g. '2025-01'
    parts = ym.split("-")
    if len(parts) == 2 and parts[1].isdigit():
        month_idx = int(parts[1])
        mau_ws[f"C{row}"] = calendar.month_abbr[month_idx]  # Jan, Feb, ...
    else:
        # Fallback: just reuse the original value
        mau_ws[f"C{row}"] = ym


# --------------------------------------------------------------------
# 3) Get or create Dashboard sheet
# --------------------------------------------------------------------
if "Dashboard" in wb.sheetnames:
    dash = wb["Dashboard"]
else:
    dash = wb.create_sheet("Dashboard")

# Keep the sheet clean (no header text in cells)
dash["A1"] = None

# If previous runs added external title cells, clear them so we don't get duplicates.
for addr in ["A2", "M2", "A17", "M17", "A32", "M32"]:
    dash[addr].value = None


def set_chart_title(chart, title: str, font_size_pt: int | None = None) -> None:
    """Set chart title text in an Excel-compatible way.

    NOTE: Setting chart-title font size has previously triggered Excel repair
    prompts on macOS for some workbooks. We apply it only when explicitly
    requested (font_size_pt not None), and we do it in a best-effort way.
    """
    chart.title = title
    try:
        chart.title.overlay = False
    except Exception:
        pass

    if font_size_pt is None:
        return

    # DrawingML uses 1/100 pt units.
    try:
        rich = chart.title.tx.rich
        if rich is not None and rich.p and rich.p[0].pPr is not None:
            rich.p[0].pPr.defRPr.sz = int(font_size_pt) * 100
    except Exception:
        # Best-effort; keep workbook generation working.
        return


def _configure_axis(chart, label_skip: int = 1) -> None:
    # Avoid Excel showing placeholder axis titles like "Horizontal (Category) Axis"
    chart.x_axis.title = None
    chart.y_axis.title = None

    # Excel is picky about axis positions. openpyxl defaults both axes to "l".
    chart.x_axis.axPos = "b"
    chart.y_axis.axPos = "l"
    chart.x_axis.crossAx = chart.y_axis.axId
    chart.y_axis.crossAx = chart.x_axis.axId

    chart.x_axis.tickLblPos = "nextTo"
    chart.y_axis.tickLblPos = "nextTo"
    chart.x_axis.delete = False
    chart.y_axis.delete = False
    chart.x_axis.majorTickMark = "out"
    chart.y_axis.majorTickMark = "out"

    if label_skip and label_skip > 1:
        chart.x_axis.tickLblSkip = label_skip
        chart.x_axis.tickMarkSkip = label_skip


# --------------------------------------------------------------------
# Helper: add a 1-series bar chart with a single colour and visible axes
# --------------------------------------------------------------------
def add_bar_chart(
    sheet,
    title,
    data_sheet,
    cat_col,
    val_col,
    pos,
    color="4472C4",
    title_font_size_pt: int | None = None,
):
    """
    Create a simple bar chart:

    - Categories from column `cat_col` (rows 2..max_row).
    - Values from column `val_col` (header in row 1, data rows 2..max_row).
    - Single series with uniform colour.
    - Axis labels and tick labels forced to be visible.
    """
    max_row = data_sheet.max_row

    data_ref = Reference(data_sheet, min_col=val_col, min_row=1, max_row=max_row)
    cat_ref = Reference(data_sheet, min_col=cat_col, min_row=2, max_row=max_row)

    chart = BarChart()
    set_chart_title(chart, title, font_size_pt=title_font_size_pt)

    chart.add_data(data_ref, titles_from_data=True)
    chart.set_categories(cat_ref)

    chart.legend = None
    chart.varyColors = False

    _configure_axis(chart, label_skip=1)

    # Apply a single colour to the only series
    if chart.series:
        s = chart.series[0]
        s.graphicalProperties.solidFill = color
        s.graphicalProperties.line.solidFill = color

    sheet.add_chart(chart, pos)


def add_line_chart(
    sheet,
    title,
    data_sheet,
    cat_col,
    val_col,
    pos,
    label_skip: int = 1,
    title_font_size_pt: int | None = None,
):
    max_row = data_sheet.max_row
    data_ref = Reference(data_sheet, min_col=val_col, min_row=1, max_row=max_row)
    cat_ref = Reference(data_sheet, min_col=cat_col, min_row=2, max_row=max_row)

    chart = LineChart()
    set_chart_title(chart, title, font_size_pt=title_font_size_pt)
    chart.add_data(data_ref, titles_from_data=True)
    chart.set_categories(cat_ref)
    chart.legend = None
    chart.varyColors = False

    # No point markers on dense daily series
    if chart.series:
        chart.series[0].marker.symbol = "none"

    _configure_axis(chart, label_skip=label_skip)
    sheet.add_chart(chart, pos)


def add_multi_line_chart(
    sheet,
    title,
    data_sheet,
    cat_col,
    min_val_col,
    max_val_col,
    pos,
    label_skip: int = 1,
    legend_position: str = "t",
    title_font_size_pt: int | None = None,
):
    max_row = data_sheet.max_row
    data_ref = Reference(
        data_sheet,
        min_col=min_val_col,
        min_row=1,
        max_col=max_val_col,
        max_row=max_row,
    )
    cat_ref = Reference(data_sheet, min_col=cat_col, min_row=2, max_row=max_row)

    chart = LineChart()
    set_chart_title(chart, title, font_size_pt=title_font_size_pt)
    chart.add_data(data_ref, titles_from_data=True)
    chart.set_categories(cat_ref)
    chart.varyColors = False

    # No point markers on dense daily series
    for s in chart.series:
        try:
            s.marker.symbol = "none"
        except Exception:
            pass

    if chart.legend is not None:
        chart.legend.position = legend_position
        chart.legend.overlay = False

    _configure_axis(chart, label_skip=label_skip)

    # Safer way to remove the “extra horizontal line” look: disable gridlines.
    # (Avoids editing axis line shape properties which can corrupt files in Excel.)
    try:
        chart.y_axis.majorGridlines = None
    except Exception:
        pass
    sheet.add_chart(chart, pos)


# --------------------------------------------------------------------
# 4) KPI charts
# --------------------------------------------------------------------

daily_label_skip = max(1, dau_ws.max_row // 18)
revenue_label_skip = max(1, rev_ws.max_row // 18)

# DAU per day
add_line_chart(
    dash,
    title="DAU",
    data_sheet=dau_ws,
    cat_col=1,
    val_col=2,
    pos="A3",
    label_skip=daily_label_skip,
    title_font_size_pt=18,
)

# Revenue per day by variant (EUR)
add_multi_line_chart(
    dash,
    title="Revenue per day by variant (EUR)",
    data_sheet=rev_var_ws,
    cat_col=1,
    min_val_col=2,
    max_val_col=4,
    pos="M33",
    label_skip=revenue_label_skip,
    legend_position="t",
    title_font_size_pt=18,
)

# WAU per week
add_bar_chart(
    dash,
    title="WAU",
    data_sheet=wau_ws,
    cat_col=1,
    val_col=2,
    pos="M3",
    title_font_size_pt=18,
)

# MAU per month (x-axis shows month names, e.g. Jan, Feb, ...)
add_bar_chart(
    dash,
    title="MAU",
    data_sheet=mau_ws,
    cat_col=3,  # month_name column
    val_col=2,
    pos="A18",
    title_font_size_pt=18,
)

# Revenue per day (EUR)
add_line_chart(
    dash,
    title="Revenue per day (EUR)",
    data_sheet=rev_ws,
    cat_col=1,
    val_col=2,
    pos="M18",
    label_skip=revenue_label_skip,
    title_font_size_pt=18,
)


# --------------------------------------------------------------------
# 5) A/B test chart: one bar per variant (control, A, B)
# --------------------------------------------------------------------
ab_max_row = 1 + len(variants)

# Data: header in B1, values B2..B4
data_ref = Reference(ab_sum_ws, min_col=2, min_row=1, max_row=ab_max_row)
# Categories: A2..A4 (control, A, B)
cat_ref = Reference(ab_sum_ws, min_col=1, min_row=2, max_row=ab_max_row)

ab_chart = BarChart()
set_chart_title(ab_chart, "Day-1 retention (%)", font_size_pt=18)
ab_chart.add_data(data_ref, titles_from_data=True)
ab_chart.set_categories(cat_ref)

# One series with three categories, different colours per bar,
# x-axis labels show which is control/A/B so legend is not necessary.
ab_chart.varyColors = True
ab_chart.legend = None

_configure_axis(ab_chart, label_skip=1)

# Show retention as percentages on a readable 0–4% scale (no data labels;
# data label XML has been a frequent source of Excel repair prompts on macOS).
ab_chart.y_axis.scaling.min = 0
ab_chart.y_axis.scaling.max = 0.04
ab_chart.y_axis.number_format = "0.0%"

dash.add_chart(ab_chart, "A33")


# --------------------------------------------------------------------
# 6) Save dashboard workbook
# --------------------------------------------------------------------
out_path = Path.cwd() / "kpi_dashboard_with_charts.xlsx"
wb.save(out_path)
print("Saved:", out_path)
