"""
Rentokil Initial invoice -> Excel (Streamlit app, single file).
Run:  streamlit run Rentokil-App.py

Parser (regex based).

Strategy
--------
1. Header fields (invoice #, account #, date, period, customer) -> regex on page text.
2. Line items -> each text line is matched against ROW_RE, a regex that anchors on
   the fixed-format fields (contract no., state, postcode, product code, money values).
3. The free-text middle part (Premises Name / Address / Suburb) cannot be split by
   regex alone because the PDF separates those columns with spaces only, so we use
   the table's vertical grid lines (x positions) to split those three columns.
   If the grid can't be found, we fall back to a pure-regex split.
4. Totals are read with regex and reconciled against the sum of the line items.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import pandas as pd
import pdfplumber

# --------------------------------------------------------------------------- #
# Regex patterns
# --------------------------------------------------------------------------- #
MONEY = r"(?:-?\$?-?[\d,]+\.\d{2}|\(\$?[\d,]+\.\d{2}\))"   # 1,234.56  $-9.95  -$9.95  ($155.16)
STATES = r"NSW|VIC|QLD|SA|WA|TAS|NT|ACT"

# One invoice line item. Everything between Pink No. and State is "middle"
# (premises + address + suburb), split later.
ROW_RE = re.compile(
    rf"""^
    (?P<contract>\d{{8}})\s+
    (?P<pink>\d+)\s+
    (?P<middle>.+)\s+          # greedy: premises names can contain 'VIC 3110'
    (?P<state>{STATES})\s+
    (?P<pc>\d{{3,4}})\s+
    (?P<pn>\d+)\s+
    (?P<code>[A-Z0-9][A-Z0-9-]{{1,9}})   # PPS, CPNTMU, 568400...\s+
    (?P<desc>.+?)\s+
    (?P<branch>\d+)\s+
    (?P<qty>\d+)\s+
    (?P<freq>\d+)\s+
    (?:(?P<annual>{MONEY})\s+)?
    (?P<monthly>{MONEY})
    \s*$""",
    re.VERBOSE | re.IGNORECASE,
)

HEADER_RES = {
    "Supplier": re.compile(r"^(Rentokil?\s+Initial\s+Pty\s+Ltd)", re.M | re.I),
    "Supplier ABN": re.compile(r"ABN\s+([\d ]{11,14})", re.I),
    "Customer": re.compile(r"^([A-Z][A-Z &]+?)\s+Date:", re.M),
    "Invoice Date": re.compile(r"Date:\s*(\d{1,2}/\d{1,2}/\d{4})", re.I),
    "Invoice #": re.compile(r"Invoice\s*#\s*(\d+)", re.I),
    "Service Period": re.compile(r"month of\s+([A-Za-z]+-\d{4})", re.I),
}

TOTALS_RE = re.compile(rf"^[ \t]*({MONEY})[ \t]+({MONEY})[ \t]*$", re.M)   # annual + monthly
GST_TOTAL_RE = re.compile(rf"^[ \t]*({MONEY})[ \t]*$", re.M)              # gst, then total

COLUMNS = [
    "Contract No.", "Pink No.", "Premises Name", "Address", "Suburb", "State",
    "PC", "PN", "Product Code", "Product Description", "Service Branch",
    "Qty", "Freq", "Annual Value (Excl. GST)", "Monthly Value (Excl. GST)",
]


def to_num(s: str) -> float:
    """'$1,234.56' -> 1234.56 ; '-$9.95' / '($9.95)' -> -9.95 (credits)."""
    s = s.strip()
    neg = s.startswith("(") or "-" in s
    val = float(re.sub(r"[^\d.]", "", s))
    return -val if neg else val


@dataclass
class ParseResult:
    header: dict
    items: pd.DataFrame
    totals: dict
    unmatched: list = field(default_factory=list)
    flagged: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Middle-column splitting
# --------------------------------------------------------------------------- #
def _vertical_lines(page) -> list[float]:
    xs = sorted({round(l["x0"], 1) for l in page.lines if abs(l["x0"] - l["x1"]) < 1})
    return xs


def _middle_bounds(page):
    """Return (premises_x0, address_x0, suburb_x0, state_x0) from grid lines, or None."""
    xs = _vertical_lines(page)
    if len(xs) < 6:
        return None
    # Grid columns: Contract | Pink | Premises | Address | Suburb | State | ...
    return xs[2], xs[3], xs[4], xs[5]


def _split_middle_by_position(row_chars, bounds):
    """Bucket individual characters into Premises / Address / Suburb by x position.
    Character level (not word level) because text that overflows a cell is clipped
    and can run straight into the next column with no space between them."""
    prem_x, addr_x, sub_x, state_x = bounds
    cols = {"Premises Name": (prem_x, addr_x), "Address": (addr_x, sub_x),
            "Suburb": (sub_x, state_x)}
    out = {}
    for name, (lo, hi) in cols.items():
        chars = sorted((c for c in row_chars if lo <= c["x0"] + 0.5 < hi),
                       key=lambda c: c["x0"])
        txt, prev = "", None
        for c in chars:
            if prev is not None and c["x0"] - prev["x1"] > 1.0:
                txt += " "
            txt += c["text"]
            prev = c
        out[name] = re.sub(r"\s+", " ", txt).strip()
    return out


def _cell_text(row_chars, lo, hi):
    return "".join(c["text"] for c in sorted(row_chars, key=lambda c: c["x0"])
                   if lo <= c["x0"] + 0.5 < hi)


# Fallback: regex-only split. Address normally starts with a number, "UNIT", "LOT"
# or "CNR"; suburb is the trailing upper-case words. Less accurate than positions.
FALLBACK_MIDDLE_RE = re.compile(
    r"^(?P<prem>.+?)\s+(?P<addr>(?:UNIT|LOT|CNR|\d)[^\s]*\s.+?)\s+(?P<sub>[A-Z][A-Z' -]+)$",
    re.I,
)


def _split_middle_by_regex(middle: str):
    m = FALLBACK_MIDDLE_RE.match(middle)
    if not m:
        return {"Premises Name": middle, "Address": "", "Suburb": ""}
    return {"Premises Name": m["prem"], "Address": m["addr"], "Suburb": m["sub"]}


def _group_words_by_line(page, tol=2.0):
    """Group words into visual lines keyed by rounded 'top'."""
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    lines: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(l, key=lambda w: w["x0"]) for l in lines]


# --------------------------------------------------------------------------- #
# Row extraction: grid cells (preferred) or regex on the text line (fallback)
# --------------------------------------------------------------------------- #
CONTRACT_START_RE = re.compile(r"^\d{8}\s")
CELL_RES = {   # regex each grid cell must satisfy
    "Contract No.": re.compile(r"^\d{8}$"),
    "Pink No.": re.compile(r"^\d+$"),
    "State": re.compile(rf"^(?:{STATES})$", re.I),
    "PC": re.compile(r"^\d{3,4}$"),
    "PN": re.compile(r"^\d+$"),
    "Product Code": re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$", re.I),
    "Service Branch": re.compile(r"^\d+$"),
    "Qty": re.compile(r"^\d+$"),
    "Freq": re.compile(r"^\d+$"),
    "Annual Value (Excl. GST)": re.compile(rf"^(?:{MONEY})?$"),   # blank on grouped lines
    "Monthly Value (Excl. GST)": re.compile(rf"^{MONEY}$"),
}
HARD_FIELDS = ("Contract No.", "Pink No.", "Monthly Value (Excl. GST)")
INT_FIELDS = ("Pink No.", "PN", "Service Branch", "Qty", "Freq")


def _row_cells(row_chars, vlines):
    """Read every grid cell of one row from character positions."""
    cells = {}
    for i, name in enumerate(COLUMNS):
        lo, hi = vlines[i], vlines[i + 1]
        chars = sorted((c for c in row_chars if lo <= c["x0"] + 0.5 < hi),
                       key=lambda c: c["x0"])
        txt, prev = "", None
        for c in chars:
            if prev is not None and c["x0"] - prev["x1"] > 1.0:
                txt += " "
            txt += c["text"]
            prev = c
        cells[name] = re.sub(r"\s+", " ", txt).strip()
    return cells


def _row_cells_regex(line):
    m = ROW_RE.match(line)
    if not m:
        return None
    return {
        "Contract No.": m["contract"], "Pink No.": m["pink"],
        **_split_middle_by_regex(m["middle"]),
        "State": m["state"], "PC": m["pc"], "PN": m["pn"], "Product Code": m["code"],
        "Product Description": m["desc"], "Service Branch": m["branch"],
        "Qty": m["qty"], "Freq": m["freq"],
        "Annual Value (Excl. GST)": m["annual"] or "",
        "Monthly Value (Excl. GST)": m["monthly"],
    }


# Australian postcode ranges -> state (used only when the State cell is unusable)
POSTCODE_STATES = [
    ("ACT", 200, 299), ("NT", 800, 999), ("NSW", 1000, 2599), ("ACT", 2600, 2618),
    ("NSW", 2619, 2899), ("ACT", 2900, 2920), ("NSW", 2921, 2999), ("VIC", 3000, 3999),
    ("QLD", 4000, 4999), ("SA", 5000, 5999), ("WA", 6000, 6999), ("TAS", 7000, 7999),
    ("VIC", 8000, 8999), ("QLD", 9000, 9999),
]


def state_from_postcode(pc: str):
    if not pc.isdigit():
        return None
    n = int(pc)
    return next((st for st, lo, hi in POSTCODE_STATES if lo <= n <= hi), None)


def _validate_row(cells):
    """Regex-check each cell. Returns (row, issues); row is None if unusable."""
    issues = []
    for name, rx in CELL_RES.items():
        if not rx.match(cells.get(name, "")):
            if name in HARD_FIELDS:
                return None, [f"bad {name}: '{cells.get(name, '')}'"]
            issues.append(f"{name} = '{cells.get(name, '')}' looks wrong")
    row = dict(cells)
    for k in INT_FIELDS:
        row[k] = int(row[k]) if row[k].isdigit() else None
    row["State"] = row["State"].upper()
    if not CELL_RES["State"].match(row["State"]):
        guess = state_from_postcode(row["PC"])
        if guess:
            issues = [i for i in issues if not i.startswith("State ")]
            issues.append(f"State '{cells['State']}' invalid -> set to {guess} from postcode")
            row["State"] = guess
    if row["PC"].isdigit():
        row["PC"] = row["PC"].zfill(4)
    annual = cells["Annual Value (Excl. GST)"]
    row["Annual Value (Excl. GST)"] = to_num(annual) if annual else None
    row["Monthly Value (Excl. GST)"] = to_num(cells["Monthly Value (Excl. GST)"])
    if row["PN"] is not None and row["PN"] != row["Pink No."]:
        issues.append("PN != Pink No.")
    a, mth = row["Annual Value (Excl. GST)"], row["Monthly Value (Excl. GST)"]
    if a and abs(a / 12 - mth) > 0.05:
        issues.append(f"Monthly {mth:,.2f} != Annual/12 ({a / 12:,.2f})")
    elif a == 0 and mth:
        issues.append("Annual value is $0.00")
    return row, issues


# --------------------------------------------------------------------------- #
# Main parser
# --------------------------------------------------------------------------- #
def parse_invoice(pdf_source) -> ParseResult:
    """pdf_source: file path, bytes, or file-like object."""
    if isinstance(pdf_source, (bytes, bytearray)):
        pdf_source = io.BytesIO(pdf_source)

    header: dict = {}
    rows: list[dict] = []
    unmatched: list[str] = []
    totals: dict = {}
    accounts: list[str] = []
    flagged: list[str] = []

    with pdfplumber.open(pdf_source) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""

            for acc in re.findall(r"Account\s*#\s*(\d+)", text, re.I):
                if acc not in accounts:
                    accounts.append(acc)

            for key, rx in HEADER_RES.items():
                if key not in header:
                    m = rx.search(text)
                    if m:
                        header[key] = m.group(1).strip()

            vlines = _vertical_lines(page)
            grid = len(vlines) == len(COLUMNS) + 1   # 15 columns -> 16 vertical lines

            for words in _group_words_by_line(page):
                line = " ".join(w["text"] for w in words)
                if not CONTRACT_START_RE.match(line):
                    continue                      # header, footer, totals...

                if grid:
                    top = min(w["top"] for w in words)
                    bottom = max(w["bottom"] for w in words)
                    row_chars = [c for c in page.chars
                                 if c["top"] >= top - 1 and c["bottom"] <= bottom + 1]
                    cells = _row_cells(row_chars, vlines)
                else:
                    cells = _row_cells_regex(line)
                    if cells is None:
                        unmatched.append(f"p{page_no}: {line}")
                        continue

                row, issues = _validate_row(cells)
                if row is None:                    # contract/money cells failed regex
                    unmatched.append(f"p{page_no}: {line}  ({'; '.join(issues)})")
                    continue
                rows.append({"Page": page_no, **row})
                if issues:
                    flagged.append(f"p{page_no} contract {row['Contract No.']} pink "
                                   f"{row['Pink No.']}: " + "; ".join(issues) + f"  |  {line}")

            # Totals appear on the last detail page under the table.
            t = TOTALS_RE.findall(text)
            if t and page_no > 1:
                annual, monthly = t[-1]
                totals["Annual Total (Excl. GST)"] = to_num(annual)
                totals["Subtotal (Excl. GST)"] = to_num(monthly)
                singles = GST_TOTAL_RE.findall(text)
                if len(singles) >= 2:
                    totals["GST"] = to_num(singles[-2])
                    totals["Total Payable (Incl. GST)"] = to_num(singles[-1])

    if accounts:
        header["Account # (summary page)"] = accounts[0]
        if len(accounts) > 1:
            header["Account # (detail pages)"] = ", ".join(accounts[1:])
    df = pd.DataFrame(rows, columns=["Page"] + COLUMNS)
    return ParseResult(header=header, items=df, totals=totals, unmatched=unmatched, flagged=flagged)


def reconcile(result: ParseResult) -> pd.DataFrame:
    df = result.items
    checks = []
    calc_monthly = round(df["Monthly Value (Excl. GST)"].sum(), 2)
    calc_annual = round(df["Annual Value (Excl. GST)"].sum(), 2)
    for label, calc, key in [
        ("Monthly subtotal (Excl. GST)", calc_monthly, "Subtotal (Excl. GST)"),
        ("Annual total (Excl. GST)", calc_annual, "Annual Total (Excl. GST)"),
    ]:
        stated = result.totals.get(key)
        checks.append({
            "Check": label,
            "Extracted sum": calc,
            "Invoice stated": stated,
            "Difference": None if stated is None else round(calc - stated, 2),
            "OK": stated is not None and abs(calc - stated) < 0.05,
        })
    if "GST" in result.totals and "Total Payable (Incl. GST)" in result.totals:
        gst, total = result.totals["GST"], result.totals["Total Payable (Incl. GST)"]
        calc_total = round(calc_monthly + gst, 2)
        checks.append({
            "Check": "Subtotal + GST = Total payable",
            "Extracted sum": calc_total,
            "Invoice stated": total,
            "Difference": round(calc_total - total, 2),
            "OK": abs(calc_total - total) < 0.05,
        })
    checks.append({
        "Check": "Rows flagged for review",
        "Extracted sum": len(result.flagged), "Invoice stated": None, "Difference": None,
        "OK": len(result.flagged) == 0,
    })
    checks.append({
        "Check": "Line items extracted",
        "Extracted sum": len(df), "Invoice stated": None, "Difference": None,
        "OK": len(result.unmatched) == 0,
    })
    return pd.DataFrame(checks)


def to_excel_bytes(result: ParseResult) -> bytes:
    """Build a formatted Excel workbook: Line Items, Summary by contract/state/product, Header & Checks."""
    df = result.items.drop(columns=["Page"])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="Line Items", index=False)

        money = ["Annual Value (Excl. GST)", "Monthly Value (Excl. GST)"]
        summaries = {
            "By Contract": df.groupby("Contract No.")[money].sum().round(2).reset_index(),
            "By State": df.groupby("State")[money].sum().round(2).reset_index(),
            "By Product": df.groupby(["Product Code", "Product Description"])[money]
                            .sum().round(2).reset_index(),
        }
        for name, s in summaries.items():
            s.insert(len(s.columns) - 2, "Lines",
                     df.groupby(list(s.columns[: len(s.columns) - 2])).size().values)
            s.to_excel(xw, sheet_name=name, index=False)

        info = pd.DataFrame(
            list(result.header.items()) + list(result.totals.items()),
            columns=["Field", "Value"],
        )
        info.to_excel(xw, sheet_name="Invoice Info", index=False)
        reconcile(result).to_excel(xw, sheet_name="Invoice Info", index=False,
                                   startrow=len(info) + 2)

        _format_workbook(xw.book)
    return buf.getvalue()


def _format_workbook(wb):
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    head_fill = PatternFill("solid", fgColor="1F4E78")
    head_font = Font(bold=True, color="FFFFFF")
    for ws in wb.worksheets:
        for cell in ws[1]:
            cell.fill, cell.font = head_fill, head_font
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.freeze_panes = "A2"
        for col_idx, col in enumerate(ws.iter_cols(min_row=1, max_row=ws.max_row), 1):
            width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
            ws.column_dimensions[get_column_letter(col_idx)].width = min(max(width + 2, 8), 45)
            header = str(col[0].value or "")
            if "Value" in header or header in ("Extracted sum", "Invoice stated", "Difference"):
                for c in col[1:]:
                    if isinstance(c.value, (int, float)):
                        c.number_format = '"$"#,##0.00'
        if ws.title == "Line Items":
            ws.auto_filter.ref = ws.dimensions
            last = ws.max_row + 1
            ws.cell(last, 1, "TOTAL").font = Font(bold=True)
            for i, cell in enumerate(ws[1], 1):
                if "Value" in str(cell.value):
                    L = get_column_letter(i)
                    c = ws.cell(last, i, f"=SUM({L}2:{L}{last - 1})")
                    c.font, c.number_format = Font(bold=True), '"$"#,##0.00'


# =========================================================================== #
# Streamlit UI
# =========================================================================== #
import streamlit as st


st.set_page_config(page_title="Rentokil Invoice → Excel", page_icon="📄", layout="wide")
st.title("📄 Rentokil Invoice → Excel")
st.caption("Upload one or more Rentokil Initial tax invoice PDFs. Line items are extracted "
           "with regex, reconciled against the invoice totals, and exported to Excel.")

files = st.file_uploader("Upload invoice PDF(s)", type=["pdf"], accept_multiple_files=True)

if not files:
    st.info("Waiting for a PDF…")
    st.stop()

all_items = []

for f in files:
    st.divider()
    st.subheader(f.name)
    try:
        with st.spinner("Extracting…"):
            res = parse_invoice(f.getvalue())
    except Exception as e:  # noqa: BLE001
        st.error(f"Could not read this PDF: {e}")
        continue

    if res.items.empty:
        st.warning("No line items matched. Is this a Rentokil service-charge invoice?")
        continue

    h = res.header
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Invoice #", h.get("Invoice #", "—"))
    c2.metric("Date", h.get("Invoice Date", "—"))
    c3.metric("Line items", len(res.items))
    c4.metric("Subtotal (ex GST)", f"${res.items['Monthly Value (Excl. GST)'].sum():,.2f}")
    c5.metric("Total payable", f"${res.totals.get('Total Payable (Incl. GST)', 0):,.2f}")

    checks = reconcile(res)
    totals_ok = checks.loc[checks["Check"] != "Rows flagged for review", "OK"].all()
    if not totals_ok:
        st.error("❌ Totals do NOT reconcile — some lines were missed or misread. "
                 "Do not use this file without checking.")
    elif res.flagged:
        st.warning(f"✅ Totals reconcile to the cent, but {len(res.flagged)} row(s) have "
                   "data issues in the PDF itself — see the flagged list below.")
    else:
        st.success("✅ All line items reconcile with the invoice totals.")
    with st.expander("Reconciliation & invoice details", expanded=not totals_ok):
        st.dataframe(checks, hide_index=True, use_container_width=True)
        st.json(h | res.totals)
    if res.flagged:
        with st.expander(f"⚠️ {len(res.flagged)} row(s) flagged for review", expanded=True):
            st.code("\n".join(res.flagged))
    if res.unmatched:
        with st.expander(f"⚠️ {len(res.unmatched)} row(s) could not be parsed"):
            st.code("\n".join(res.unmatched))

    df = res.items.drop(columns=["Page"])
    fc1, fc2, fc3 = st.columns(3)
    states = fc1.multiselect("State", sorted(df["State"].unique()), key=f"s{f.name}")
    codes = fc2.multiselect("Product code", sorted(df["Product Code"].unique()), key=f"c{f.name}")
    q = fc3.text_input("Search premises / address", key=f"q{f.name}")
    view = df
    if states:
        view = view[view["State"].isin(states)]
    if codes:
        view = view[view["Product Code"].isin(codes)]
    if q:
        mask = view[["Premises Name", "Address", "Suburb"]].apply(
            lambda col: col.str.contains(re.escape(q), case=False, na=False)).any(axis=1)
        view = view[mask]
    st.dataframe(view, hide_index=True, use_container_width=True, height=420)

    out_name = f"{h.get('Customer', 'Invoice')}_{h.get('Invoice #', 'extract')}.xlsx".replace(" ", "_")
    st.download_button(
        "⬇️ Download Excel", data=to_excel_bytes(res), file_name=out_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"dl{f.name}", type="primary",
    )

    all_items.append(df.assign(**{"Invoice #": h.get("Invoice #"), "Source File": f.name}))

if len(all_items) > 1:
    st.divider()
    combined = pd.concat(all_items, ignore_index=True)
    st.subheader(f"Combined: {len(combined)} lines from {len(all_items)} invoices")
    buf = io.BytesIO()
    combined.to_excel(buf, index=False, sheet_name="All Line Items")
    st.download_button("⬇️ Download combined Excel", buf.getvalue(), "combined_invoices.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
