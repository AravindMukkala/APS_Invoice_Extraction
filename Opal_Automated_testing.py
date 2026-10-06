"""
Opal Recycling / Opal Packaging invoice PDF -> Excel extractor.

How it works (and why it is robust):

* Words are read with their x/y positions (pdfplumber.extract_words) and
  grouped into visual rows.
* The column header row ("Customer/ Description Period/ Reference Qty. ...")
  is located on every page, and its x positions are used as column
  boundaries.  Text that wraps onto a second line (e.g. "QR26-1590",
  "BINS AUGUST 26", "30.09.2026 - LDPE", "(Wasteflex)") is therefore
  appended to the correct column of the charge / customer it belongs to.
* Amounts are parsed from the right-hand end of each row with strict
  regexes, so descriptions and references may contain anything.
* Every figure is cross-checked:
    - each line:      Ex GST + GST = Incl. GST
    - each charge:    Item Total + linked fuel levy = Ex GST
    - each sub-total, customer total, invoice Total Payable and AMOUNT DUE

Run as a web app:   streamlit run opal_invoice_extractor.py
Run from the CLI:   python opal_invoice_extractor.py invoice.pdf [out.xlsx]
"""

import io
import re
import sys

import pandas as pd
import pdfplumber


# ============================================================
# PATTERNS
# ============================================================

DATE = r"\d{2}\.\d{2}\.\d{4}"
NUM = r"\d[\d,]*\.\d+"
AMT = r"-?\d[\d,]*\.\d{2}"
UNIT = r"[A-Za-z]{1,4}"

DATE_RE = re.compile(rf"^{DATE}$")
CUSTOMER_CODE_RE = re.compile(r"^R-[A-Z0-9]+$")

INVOICE_NO_RE = re.compile(r"Invoice\s+No\.?\s*(\d+)", re.I)
ACCOUNT_NO_RE = re.compile(r"Account\s+No\.?\s*(R-[A-Z0-9]+)", re.I)
INVOICE_DATE_RE = re.compile(rf"^Date\s+({DATE})\s*$", re.I | re.M)
AMOUNT_DUE_RE = re.compile(rf"AMOUNT\s+DUE\s+({AMT})\s*AUD", re.I)

# Amount tails, matched against the END of a row.
# FFS - Qty/Weight, Manual Price, period charges:
#   1.635 TO 9.01 BL 45.05 47.50 4.75 52.25 AUD
TAIL_UNIT_PRICE_RE = re.compile(
    rf"(?P<qty>{NUM})\s+(?P<qty_unit>{UNIT})\s+"
    rf"(?P<unit_price>{NUM})\s+(?P<price_unit>{UNIT})\s+"
    rf"(?P<item>{AMT})\s+(?P<ex>{AMT})\s+(?P<gst>{AMT})\s+(?P<inc>{AMT})\s+AUD$"
)
# FFS - Load (no unit price):
#   1.000 OT 240.00 253.30 25.33 278.63 AUD
TAIL_LOAD_RE = re.compile(
    rf"(?P<qty>{NUM})\s+(?P<qty_unit>{UNIT})\s+"
    rf"(?P<item>{AMT})\s+(?P<ex>{AMT})\s+(?P<gst>{AMT})\s+(?P<inc>{AMT})\s+AUD$"
)
# Fuel levy:
#   1.635 TO 0.49 BL 2.45     or     1.000 OT 13.30
TAIL_FUEL_RE = re.compile(
    rf"(?P<qty>{NUM})\s+(?P<qty_unit>{UNIT})\s+"
    rf"(?:(?P<unit_price>{NUM})\s+(?P<price_unit>{UNIT})\s+)?"
    rf"(?P<item>{AMT})$"
)

BILLED_QTY_RE = re.compile(rf"^Billed\s+Qty\s+({NUM})\s+({UNIT})$", re.I)

SUBTOTAL_RE = re.compile(
    rf"^SUB-TOTAL\s+(?P<code>\S+)\s+(?P<qty>{NUM})\s+(?P<qty_unit>{UNIT})\s+"
    rf"(?P<ex>{AMT})\s+(?P<gst>{AMT})\s+(?P<inc>{AMT})\s+AUD$",
    re.I,
)
# Note: the PDF sometimes renders "(TOTAL)" as "( TOTAL)".
CUSTOMER_TOTAL_RE = re.compile(
    rf"^(?P<code>R-[A-Z0-9]+)-\s*(?P<name>.*?)\s*\(\s*TOTAL\s*\)\s+"
    rf"(?P<ex>{AMT})\s+(?P<gst>{AMT})\s+(?P<inc>{AMT})\s+AUD$",
    re.I,
)
TOTAL_PAYABLE_RE = re.compile(
    rf"^Total\s+Payable\s+(?P<ex>{AMT})\s+(?P<gst>{AMT})\s+(?P<inc>{AMT})\s+AUD$",
    re.I,
)
PAGE_FOOTER_RE = re.compile(r"^Page\s+\d+\s+of\s+\d+$", re.I)

FFS_QTY_RE = re.compile(r"FFS\s*-\s*Qty\s*/\s*Weight", re.I)
FFS_LOAD_RE = re.compile(r"FFS\s*-\s*Load", re.I)
MANUAL_RE = re.compile(r"Manual\s+Price", re.I)
FUEL_RE = re.compile(r"^OPR\s+Fuel\s+Levy", re.I)
PERIOD_RE = re.compile(rf"({DATE})\s+to\s*({DATE})?", re.I)

TOLERANCE = 0.015

INVOICE_COLUMNS = [
    "Invoice No.", "Invoice Date", "Account No.",
    "Customer Code", "Customer",
    "Date", "Description", "Charge Type", "Period From", "Period To",
    "Reference", "Sub-Total Code",
    "Qty.", "Qty Value", "Qty Unit",
    "Unit Price", "Unit Price Value", "Price Unit",
    "Item Total", "Fuel Levy (linked)",
    "Amount excl. GST", "GST", "Amount Incl. GST",
    "Billed qty", "Billed Qty Value", "Billed Qty Unit",
    "Is Fuel Levy", "Line Check", "Source Page",
]


# ============================================================
# SMALL HELPERS
# ============================================================

def to_num(value):
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def to_money(value):
    n = to_num(value)
    return None if n is None else round(n, 2)


def close(a, b):
    if a is None or b is None or pd.isna(a) or pd.isna(b):
        return False
    return abs(float(a) - float(b)) <= TOLERANCE


def join(*parts):
    return " ".join(p for p in parts if p).strip()


# ============================================================
# PAGE LAYOUT
# ============================================================

def group_rows(words, y_tol=3.0):
    """Group pdfplumber words into visual rows, left to right."""
    rows = []
    for w in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        if rows and abs(w["top"] - rows[-1]["top"]) <= y_tol:
            rows[-1]["words"].append(w)
        else:
            rows.append({"top": w["top"], "words": [w]})
    for r in rows:
        r["words"].sort(key=lambda w: w["x0"])
        r["text"] = " ".join(w["text"] for w in r["words"])
    return rows


def find_columns(rows):
    """
    Locate the table header row and return (header_index, boundaries).
    Boundaries are the left edges of the Description, Period/Charge Type
    and Reference columns.
    """
    for idx, row in enumerate(rows):
        texts = [w["text"] for w in row["words"]]
        if "Customer/" in texts and "Description" in texts and "Reference" in texts:
            def x_of(label):
                return next(w["x0"] for w in row["words"] if w["text"] == label)

            period_label = "Period/" if "Period/" in texts else "Period"
            bounds = {
                "description": x_of("Description") - 2,
                "charge_type": x_of(period_label) - 2,
                "reference": x_of("Reference") - 2,
            }
            # The header is two visual lines ("Date", "Charge Type" ...).
            # Skip the second one if present.
            start = idx + 1
            if start < len(rows) and rows[start]["text"].startswith("Date"):
                start += 1
            return start, bounds
    return None, None


def split_columns(words, bounds):
    """Assign text words to the four text columns by x position."""
    cols = {"first": [], "description": [], "charge_type": [], "reference": []}
    for w in words:
        x = w["x0"]
        if x < bounds["description"]:
            cols["first"].append(w["text"])
        elif x < bounds["charge_type"]:
            cols["description"].append(w["text"])
        elif x < bounds["reference"]:
            cols["charge_type"].append(w["text"])
        else:
            cols["reference"].append(w["text"])
    return {k: " ".join(v) for k, v in cols.items()}


def split_tail(row, pattern):
    """
    Match an amount pattern at the end of a row.
    Returns (match, text_words_before_the_amounts) or (None, None).
    """
    m = pattern.search(row["text"])
    if not m:
        return None, None
    n_tail = len(m.group(0).split())
    words = row["words"]
    if n_tail > len(words):
        return None, None
    tail_text = " ".join(w["text"] for w in words[-n_tail:])
    if tail_text != m.group(0):
        return None, None
    # The amounts must start a new token (not the middle of a reference).
    if m.start() > 0 and row["text"][m.start() - 1] != " ":
        return None, None
    return m, words[:-n_tail]


# ============================================================
# MAIN PARSER
# ============================================================

def parse_charge_type(text):
    """Return (charge_type, period_from, period_to) for the charge-type column."""
    if FFS_QTY_RE.search(text):
        return "FFS - Qty/Weight", "", ""
    if FFS_LOAD_RE.search(text):
        return "FFS - Load", "", ""
    if MANUAL_RE.search(text):
        return "Manual Price", "", ""
    m = PERIOD_RE.search(text)
    if m:
        return "Period Charge", m.group(1), m.group(2) or ""
    return text.strip(), "", ""


def process_pdf(file_stream):
    rows_out = []          # charges + fuel levies
    subtotals = []
    customer_totals = []
    invoice_totals = []
    unmatched = []

    inv = {"no": "", "date": "", "account": "", "amount_due": None}
    cust = {"code": "", "name": ""}

    open_charge = None     # charge row still collecting wrapped text
    open_customer = False  # customer header still collecting wrapped name
    last_main = None       # last main charge (for linking fuel levies)
    pending = []           # charge rows awaiting their SUB-TOTAL code

    with pdfplumber.open(file_stream) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            page_text = page.extract_text() or ""

            # ---------------- invoice header ----------------
            m = INVOICE_NO_RE.search(page_text)
            if m and m.group(1) != inv["no"]:
                # New invoice: reset all running state.
                inv = {"no": m.group(1), "date": "", "account": "", "amount_due": None}
                cust = {"code": "", "name": ""}
                open_charge, open_customer, last_main, pending = None, False, None, []
            m = INVOICE_DATE_RE.search(page_text)
            if m:
                inv["date"] = m.group(1)
            m = ACCOUNT_NO_RE.search(page_text)
            if m:
                inv["account"] = m.group(1)
            m = AMOUNT_DUE_RE.search(page_text)
            if m:
                inv["amount_due"] = to_money(m.group(1))

            words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
            rows = group_rows(words)
            start, bounds = find_columns(rows)
            if start is None:
                continue  # e.g. remittance-only page

            for row in rows[start:]:
                text = row["text"].strip()
                if not text or PAGE_FOOTER_RE.match(text):
                    continue

                def base_row():
                    return {
                        "Invoice No.": inv["no"],
                        "Invoice Date": inv["date"],
                        "Account No.": inv["account"],
                        "Customer Code": cust["code"],
                        "Customer": cust["name"],
                        "Source Page": page_num,
                    }

                # ---------- Total Payable (end of table) ----------
                m = TOTAL_PAYABLE_RE.match(text)
                if m:
                    invoice_totals.append({
                        "Invoice No.": inv["no"],
                        "Invoice Date": inv["date"],
                        "Account No.": inv["account"],
                        "Printed Ex GST": to_money(m["ex"]),
                        "Printed GST": to_money(m["gst"]),
                        "Printed Inc GST": to_money(m["inc"]),
                        "Amount Due": inv["amount_due"],
                    })
                    open_charge, open_customer = None, False
                    break  # everything after this is the remittance footer

                # ---------- Customer total ----------
                m = CUSTOMER_TOTAL_RE.match(text)
                if m:
                    customer_totals.append({
                        "Invoice No.": inv["no"],
                        "Customer Code": m["code"],
                        "Customer (printed)": m["name"].strip(),
                        "Printed Ex GST": to_money(m["ex"]),
                        "Printed GST": to_money(m["gst"]),
                        "Printed Inc GST": to_money(m["inc"]),
                    })
                    open_charge, open_customer = None, False
                    continue

                # ---------- Sub-total ----------
                m = SUBTOTAL_RE.match(text)
                if m:
                    subtotals.append({
                        "Invoice No.": inv["no"],
                        "Customer Code": cust["code"],
                        "Customer": cust["name"],
                        "Sub-Total Code": m["code"],
                        "Printed Qty": f'{m["qty"]} {m["qty_unit"]}',
                        "Printed Ex GST": to_money(m["ex"]),
                        "Printed GST": to_money(m["gst"]),
                        "Printed Inc GST": to_money(m["inc"]),
                    })
                    for r in pending:
                        r["Sub-Total Code"] = m["code"]
                    pending = []
                    open_charge, open_customer = None, False
                    continue

                # ---------- Billed Qty ----------
                m = BILLED_QTY_RE.match(text)
                if m:
                    target = open_charge if open_charge is not None else (
                        rows_out[-1] if rows_out else None)
                    if target is not None and not target.get("Billed qty"):
                        target["Billed qty"] = f"{m.group(1)} {m.group(2)}"
                        target["Billed Qty Value"] = to_num(m.group(1))
                        target["Billed Qty Unit"] = m.group(2)
                    open_charge = None
                    continue

                # ---------- Customer header ----------
                first_word = row["words"][0]["text"]
                if (CUSTOMER_CODE_RE.match(first_word)
                        and row["words"][0]["x0"] < bounds["description"]):
                    cust = {
                        "code": first_word,
                        "name": " ".join(w["text"] for w in row["words"][1:]),
                    }
                    open_customer, open_charge, last_main = True, None, None
                    continue

                # ---------- Fuel levy ----------
                if FUEL_RE.match(text):
                    m, text_words = split_tail(row, TAIL_FUEL_RE)
                    if m:
                        cols = split_columns(text_words, bounds)
                        fuel_type = ("Fuel Levy - Load"
                                     if "load" in cols["charge_type"].lower()
                                     else "Fuel Levy - Qty/Weight")
                        r = base_row()
                        r.update({
                            "Date": last_main["Date"] if last_main else "",
                            "Description": "OPR Fuel Levy",
                            "Charge Type": fuel_type,
                            "Reference": cols["reference"],
                            "Qty.": f'{m["qty"]} {m["qty_unit"]}',
                            "Qty Value": to_num(m["qty"]),
                            "Qty Unit": m["qty_unit"],
                            "Unit Price": (f'{m["unit_price"]} {m["price_unit"]}'
                                           if m["unit_price"] else ""),
                            "Unit Price Value": to_num(m["unit_price"]),
                            "Price Unit": m["price_unit"] or "",
                            "Item Total": to_money(m["item"]),
                            "Is Fuel Levy": True,
                        })
                        # Link to the charge it belongs to (same reference).
                        if last_main is not None and (
                                last_main["Reference"].split(" ")[0]
                                == r["Reference"].split(" ")[0]):
                            last_main["Fuel Levy (linked)"] = round(
                                (last_main.get("Fuel Levy (linked)") or 0)
                                + r["Item Total"], 2)
                            r["Date"] = last_main["Date"]
                        rows_out.append(r)
                        pending.append(r)
                        open_charge, open_customer = r, False
                        continue

                # ---------- Main charge ----------
                m, text_words = split_tail(row, TAIL_UNIT_PRICE_RE)
                has_unit_price = m is not None
                if not m:
                    m, text_words = split_tail(row, TAIL_LOAD_RE)
                if m and text_words:
                    cols = split_columns(text_words, bounds)
                    if DATE_RE.match(cols["first"]):
                        ctype, p_from, p_to = parse_charge_type(cols["charge_type"])
                        r = base_row()
                        r.update({
                            "Date": cols["first"],
                            "Description": cols["description"],
                            "Charge Type": ctype,
                            "Period From": p_from,
                            "Period To": p_to,
                            "Reference": cols["reference"],
                            "Qty.": f'{m["qty"]} {m["qty_unit"]}',
                            "Qty Value": to_num(m["qty"]),
                            "Qty Unit": m["qty_unit"],
                            "Unit Price": (f'{m["unit_price"]} {m["price_unit"]}'
                                           if has_unit_price else ""),
                            "Unit Price Value": (to_num(m["unit_price"])
                                                 if has_unit_price else None),
                            "Price Unit": m["price_unit"] if has_unit_price else "",
                            "Item Total": to_money(m["item"]),
                            "Amount excl. GST": to_money(m["ex"]),
                            "GST": to_money(m["gst"]),
                            "Amount Incl. GST": to_money(m["inc"]),
                            "Is Fuel Levy": False,
                        })
                        rows_out.append(r)
                        pending.append(r)
                        open_charge, last_main, open_customer = r, r, False
                        continue

                # ---------- Wrapped text (continuation lines) ----------
                cols = split_columns(row["words"], bounds)
                if open_charge is not None and not cols["first"]:
                    c = open_charge
                    c["Description"] = join(c.get("Description", ""), cols["description"])
                    ct = cols["charge_type"]
                    if ct:
                        if c["Charge Type"] == "Period Charge" and not c.get("Period To"):
                            pm = re.match(rf"^({DATE})\b\s*(.*)$", ct)
                            if pm:
                                c["Period To"] = pm.group(1)
                                ct = pm.group(2)
                        if ct:
                            c["Charge Type"] = join(c["Charge Type"], ct)
                    ref = cols["reference"]
                    # A lone "A" under the reference is a PDF artefact.
                    if ref and ref != "A":
                        c["Reference"] = join(c.get("Reference", ""), ref)
                    continue

                if open_customer and not cols["first"]:
                    cust["name"] = join(cust["name"], text)
                    # Keep rows already created for this customer in sync.
                    continue

                # ---------- Anything else that looks like money ----------
                if re.search(AMT, text) or DATE_RE.match(first_word):
                    unmatched.append({
                        "Page": page_num,
                        "Invoice No.": inv["no"],
                        "Customer Code": cust["code"],
                        "Line": text,
                        "Reason": "Row contains amounts/date but matched no pattern",
                    })

    invoice_df = pd.DataFrame(rows_out)
    for col in INVOICE_COLUMNS:
        if col not in invoice_df.columns:
            invoice_df[col] = None
    invoice_df = invoice_df[INVOICE_COLUMNS]

    invoice_df["Line Check"] = invoice_df.apply(line_check, axis=1)

    return (
        invoice_df,
        build_invoice_validation(invoice_df, invoice_totals),
        build_customer_validation(invoice_df, customer_totals),
        build_subtotal_validation(invoice_df, subtotals),
        pd.DataFrame(unmatched, columns=[
            "Page", "Invoice No.", "Customer Code", "Line", "Reason"]),
    )


# ============================================================
# VALIDATION
# ============================================================

def line_check(r):
    if r["Is Fuel Levy"]:
        return "Fuel levy (included in parent charge)"
    problems = []
    if not close(r["Amount excl. GST"] + r["GST"], r["Amount Incl. GST"]):
        problems.append("Ex GST + GST != Incl. GST")
    fuel = r["Fuel Levy (linked)"] if pd.notna(r["Fuel Levy (linked)"]) else 0
    # Manual / period charges show Item Total 0.00; their Ex GST is the price.
    if r["Item Total"] and not close(r["Item Total"] + fuel, r["Amount excl. GST"]):
        problems.append("Item Total + Fuel != Ex GST")
    if r["Charge Type"] in ("Manual Price", "Period Charge") and r["Unit Price Value"]:
        expected = round(r["Qty Value"] * r["Unit Price Value"], 2)
        if not close(expected, r["Amount excl. GST"]):
            problems.append("Qty x Unit Price != Ex GST")
    return "OK" if not problems else "; ".join(problems)


def _sum_main(invoice_df, keys):
    main = invoice_df[invoice_df["Is Fuel Levy"] != True]
    return (
        main.groupby(keys, dropna=False)
        .agg(**{
            "Extracted Ex GST": ("Amount excl. GST", "sum"),
            "Extracted GST": ("GST", "sum"),
            "Extracted Inc GST": ("Amount Incl. GST", "sum"),
            "Charge Lines": ("Amount excl. GST", "count"),
        })
        .round(2)
        .reset_index()
    )


def _compare(extracted, printed, keys):
    if printed.empty:
        printed = pd.DataFrame(columns=keys + [
            "Printed Ex GST", "Printed GST", "Printed Inc GST"])
    result = pd.merge(extracted, printed, on=keys, how="outer")
    for part in ("Ex GST", "GST", "Inc GST"):
        result[f"Diff {part}"] = (
            result[f"Extracted {part}"].fillna(0) - result[f"Printed {part}"].fillna(0)
        ).round(2)

    def status(r):
        if pd.isna(r["Printed Inc GST"]):
            return "NO PRINTED TOTAL"
        if pd.isna(r["Extracted Inc GST"]):
            return "NO LINES EXTRACTED"
        ok = all(abs(r[f"Diff {p}"]) <= TOLERANCE for p in ("Ex GST", "GST", "Inc GST"))
        return "PASS" if ok else "FAIL"

    result["Validation"] = result.apply(status, axis=1) if len(result) else []
    return result


def build_invoice_validation(invoice_df, invoice_totals):
    keys = ["Invoice No."]
    printed = pd.DataFrame(invoice_totals)
    result = _compare(_sum_main(invoice_df, keys), printed, keys)
    if "Amount Due" in result.columns:
        result["Amount Due Check"] = result.apply(
            lambda r: "PASS" if close(r["Amount Due"], r["Printed Inc GST"]) else "FAIL",
            axis=1)
    return result


def build_customer_validation(invoice_df, customer_totals):
    keys = ["Invoice No.", "Customer Code"]
    extracted = _sum_main(invoice_df, keys + ["Customer"])
    return _compare(extracted, pd.DataFrame(customer_totals), keys)


def build_subtotal_validation(invoice_df, subtotals):
    keys = ["Invoice No.", "Customer Code", "Sub-Total Code"]
    printed = pd.DataFrame(subtotals)
    if not printed.empty:
        printed = printed.drop(columns=["Customer"])
    return _compare(_sum_main(invoice_df, keys), printed, keys)


# ============================================================
# EXCEL EXPORT
# ============================================================

def create_excel(sheets):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, sheet_name=name, index=False)
        for ws in writer.book.worksheets:
            ws.freeze_panes = "A2"
            headers = [c.value for c in ws[1]]
            for col_idx, header in enumerate(headers, start=1):
                if header in ("Reference", "Invoice No."):
                    for row in ws.iter_rows(min_row=2, min_col=col_idx, max_col=col_idx):
                        row[0].number_format = "@"
                elif header in ("Amount excl. GST", "GST", "Item Total"):
                    for row in ws.iter_rows(min_row=2, min_col=col_idx, max_col=col_idx):
                        row[0].number_format = "0.00"
            for column_cells in ws.columns:
                width = max(len(str(c.value)) if c.value is not None else 0
                            for c in column_cells)
                ws.column_dimensions[column_cells[0].column_letter].width = min(
                    max(width + 2, 10), 45)
    return output.getvalue()


TEMPLATE_COLUMNS = [
    "Invoice No.", "Customer", "Date", "Description",
    "Charge Type/Period Reference", "Reference", "Billed qty", "Qty.",
    "Unit Price", "Amount excl. GST", "GST", "Amount Incl. GST",
]


def build_template(invoice_df):
    """Invoice Data reshaped to the paste-in template's columns and order."""
    def charge_or_period(r):
        if r["Charge Type"] == "Period Charge" and r["Period From"]:
            return join(r["Period From"], "to", r["Period To"])
        return r["Charge Type"]

    def inc_aud(v):
        return "" if v is None or pd.isna(v) else f"{v:,.2f} AUD"

    out = pd.DataFrame({
        "Invoice No.": invoice_df["Invoice No."].astype(str),
        # "Customer Code - Customer", e.g. "R-ALB3003 - Albert Park Golf Maintenance"
        "Customer": (invoice_df["Customer Code"].fillna("").astype(str)
                     + " - " + invoice_df["Customer"].fillna("").astype(str)),
        "Date": invoice_df["Date"],
        "Description": invoice_df["Description"],
        "Charge Type/Period Reference": invoice_df.apply(charge_or_period, axis=1),
        # Kept as text so Excel doesn't turn 50001003546705 into 5.0E+13.
        "Reference": invoice_df["Reference"].astype(str),
        "Billed qty": invoice_df["Billed qty"],
        "Qty.": invoice_df["Qty."],
        "Unit Price": invoice_df["Unit Price"].fillna(""),
        "Amount excl. GST": invoice_df["Amount excl. GST"],
        "GST": invoice_df["GST"],
        "Amount Incl. GST": invoice_df["Amount Incl. GST"].map(inc_aud),
    })
    return out[TEMPLATE_COLUMNS]


def extract_all(file_stream):
    invoice_df, inv_val, cust_val, sub_val, unmatched = process_pdf(file_stream)
    return {
        "Invoice Data": invoice_df,
        "Copy to Template": build_template(invoice_df),
        "Invoice Validation": inv_val,
        "Customer Validation": cust_val,
        "Sub-Total Validation": sub_val,
        "Unmatched Lines": unmatched,
    }


# ============================================================
# STREAMLIT UI
# ============================================================

def run_streamlit():
    import streamlit as st

    st.set_page_config(page_title="Opal Invoice PDF → Excel", page_icon="📄",
                       layout="wide")
    st.title("📄 Opal Invoice PDF → Excel Extractor")
    st.caption("Extracts Opal Recycling invoice charges and fuel levies, and "
               "reconciles every line, sub-total, customer total and invoice total.")

    uploaded = st.file_uploader("Upload an Opal invoice PDF", type=["pdf"])
    if not uploaded:
        return

    with st.spinner("Processing PDF..."):
        try:
            sheets = extract_all(io.BytesIO(uploaded.read()))
        except Exception as e:  # noqa: BLE001
            st.error(f"❌ Error while processing PDF: {e}")
            st.exception(e)
            st.stop()

    df = sheets["Invoice Data"]
    inv_val = sheets["Invoice Validation"]
    cust_val = sheets["Customer Validation"]
    sub_val = sheets["Sub-Total Validation"]
    unmatched = sheets["Unmatched Lines"]
    bad_lines = df[~df["Line Check"].isin(
        ["OK", "Fuel levy (included in parent charge)"])]

    c = st.columns(6)
    c[0].metric("Invoices", df["Invoice No."].nunique())
    c[1].metric("Charges", int((df["Is Fuel Levy"] != True).sum()))
    c[2].metric("Fuel levies", int((df["Is Fuel Levy"] == True).sum()))
    c[3].metric("Invoice PASS", f'{(inv_val["Validation"] == "PASS").sum()}/{len(inv_val)}')
    c[4].metric("Customer PASS", f'{(cust_val["Validation"] == "PASS").sum()}/{len(cust_val)}')
    c[5].metric("Unmatched rows", len(unmatched))

    all_ok = (
        len(inv_val) > 0
        and (inv_val["Validation"] == "PASS").all()
        and (cust_val["Validation"] == "PASS").all()
        and (sub_val["Validation"] == "PASS").all()
        and bad_lines.empty and unmatched.empty
    )
    if all_ok:
        st.success("✅ Every line, sub-total, customer total and invoice total reconciles.")
    else:
        st.error("⚠️ Some checks failed — see the validation tabs.")

    tabs = st.tabs(list(sheets.keys()))
    for tab, (name, frame) in zip(tabs, sheets.items()):
        with tab:
            if frame.empty:
                st.info("Nothing to show.")
            else:
                st.dataframe(frame, use_container_width=True, height=600)

    numbers = df["Invoice No."].dropna().astype(str).unique()
    file_name = (f"Opal_Invoice_{numbers[0]}.xlsx" if len(numbers) == 1
                 else "Opal_Invoice_Extract.xlsx")
    st.download_button(
        "📥 Download Excel", data=create_excel(sheets), file_name=file_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def run_cli(pdf_path, out_path=None):
    with open(pdf_path, "rb") as f:
        sheets = extract_all(io.BytesIO(f.read()))
    out_path = out_path or re.sub(r"\.pdf$", "", pdf_path, flags=re.I) + ".xlsx"
    with open(out_path, "wb") as f:
        f.write(create_excel(sheets))

    df = sheets["Invoice Data"]
    print(f"Rows: {len(df)}  (charges {(df['Is Fuel Levy'] != True).sum()}, "
          f"fuel levies {(df['Is Fuel Levy'] == True).sum()})")
    for name in ("Invoice Validation", "Customer Validation", "Sub-Total Validation"):
        v = sheets[name]["Validation"].value_counts().to_dict()
        print(f"{name}: {v}")
    print("Line checks:", df["Line Check"].value_counts().to_dict())
    print("Unmatched rows:", len(sheets["Unmatched Lines"]))
    print("Saved:", out_path)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].lower().endswith(".pdf"):
        run_cli(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        run_streamlit()
