import os
import fitz  # PyMuPDF
import re
import pandas as pd
import streamlit as st
from io import BytesIO

# ---------------------------
# Extract text from PDF
# ---------------------------
def extract_text_from_pdf(pdf_input):
    if isinstance(pdf_input, (str, os.PathLike)):
        doc = fitz.open(str(pdf_input))
    else:
        if hasattr(pdf_input, "read"):
            data = pdf_input.read()
        else:
            data = pdf_input
        if isinstance(data, str):
            data = data.encode("utf-8")
        doc = fitz.open(stream=data, filetype="pdf")
    return [page.get_text("text") for page in doc]


# ---------------------------
# Extract Customer & Address
# ---------------------------
def extract_customer_address(text):
    lines = text.splitlines()
    customer, address = "", ""
    for i, line in enumerate(lines):
        if line.strip().upper().startswith("SITE ADDRESS") and i + 1 < len(lines):
            addr_lines = []
            for j in range(i + 1, len(lines)):
                next_line = lines[j].strip()
                if not next_line:
                    break
                addr_lines.append(next_line)
                if re.search(r"\b(?:VIC|NSW|QLD|TAS|WA|SA|NT|ACT)\b\s*\d{3,4}", next_line):
                    break
            address = " ".join(addr_lines).strip()
            customer = lines[i + 1].strip() if addr_lines else ""
            return customer, address

    cust_addr_match = re.search(
        r"([A-Za-z0-9 \-/&]+)\n([\d]+ .+?\s(?:VIC|TAS|NSW|QLD|WA|SA|NT|ACT)\s*\d{3,4})",
        text,
    )
    if cust_addr_match:
        customer = cust_addr_match.group(1).strip()
        address = cust_addr_match.group(2).replace("\n", ", ").strip()
    return customer, address


# ---------------------------
# Split Reference vs Service
# ---------------------------
def split_reference_and_service(desc):
    desc = desc.strip()
    tokens = desc.split(maxsplit=1)
    if not tokens:
        return "", desc
    first, rest = tokens[0], tokens[1] if len(tokens) > 1 else ""
    if re.match(r"CASE[:\-]?\d+", first, re.I):
        return first, rest
    if first.isdigit() and len(first) >= 3:
        return first, rest
    if any(c.isdigit() for c in first) and len(first) >= 5:
        return first, rest
    return "", desc


def clean_amount(value):
    if not value:
        return None
    try:
        return float(value.replace("$", "").replace(",", "").strip())
    except Exception:
        return None


def parse_invoice_lines(block_text, header_data):
    lines = [l.strip() for l in block_text.splitlines() if l.strip()]
    line_items, current = [], []
    for l in lines:
        if re.match(r"\d{2}/\d{2}/\d{2,4}", l):
            if current:
                line_items.append(current)
            current = [l]
        else:
            current.append(l)
    if current:
        line_items.append(current)

    records = []
    for item in line_items:
        text = " ".join(item)
        text = re.sub(r"(\d{1,3}(?:,\d{3})*)\.\s*(\d{2})", r"\1.\2", text)
        # Case 1: Quantity + Amount
        m = re.match(
            r"(\d{2}/\d{2}/\d{2,4})\s+(.+?)\s+(\d{1,6}(?:\.\d{1,2})?)\s+\$?\s*([\d,]+\.\d{2})",
            text,
        )
        if not m:
            m = re.match(
                r"(\d{2}/\d{2}/\d{2,4})\s+(.+?)\s+(\d{1,6}(?:\.\d{1,2})?)\s+\$?\s*([\d.,]+)",
                text,
            )
        if m:
            ref, service = split_reference_and_service(m.group(2))
            records.append(
                {
                    **header_data,
                    "Service Date": m.group(1),
                    "Reference": ref,
                    "Service Provided": service,
                    "Quantity": m.group(3),
                    "Amount": clean_amount(m.group(4)),
                }
            )
            continue

        # Case 2: Amount only
        m = re.match(r"(\d{2}/\d{2}/\d{2,4})\s+(.+?)\s+\$?([\d,]+\.\d{2})", text)
        if not m:
            m = re.match(
                r"(\d{2}/\d{2}/\d{2,4})\s+(.+?)\s+\$?\s*([\d.,]+)",
                text,
            )
        if m:
            ref, service = split_reference_and_service(m.group(2))
            records.append(
                {
                    **header_data,
                    "Service Date": m.group(1),
                    "Reference": ref,
                    "Service Provided": service,
                    "Quantity": "",
                    "Amount": clean_amount(m.group(3)),
                }
            )
            continue

        # Case 3: Quantity only
        m = re.match(
            r"(\d{2}/\d{2}/\d{2,4})\s+(.+?)\s+(\d{1,6}(?:\.\d{1,2})?)$",
            text,
        )
        if m:
            ref, service = split_reference_and_service(m.group(2))
            next_amount = ""
            if len(item) > 1:
                maybe_amount = item[-1].replace("$", "").replace(",", "").strip()
                if re.match(r"^\d+(?:\.\d{2})?$", maybe_amount):
                    next_amount = maybe_amount
            records.append(
                {
                    **header_data,
                    "Service Date": m.group(1),
                    "Reference": ref,
                    "Service Provided": service,
                    "Quantity": m.group(3),
                    "Amount": clean_amount(next_amount),
                }
            )
            continue
    return records


# ---------------------------
# Parse invoice page
# ---------------------------
def parse_invoice(text, prev_header=None):
    header_patterns = {
        "Tax Invoice": r"Tax Invoice\s+(\d+)",
        "Invoice Date": r"Invoice Date\s+([\d/]+)",
        "Account Number": r"Account Number\s+(\d+)",
        "Purchase Order": r"Purchase Order\s*(\S*)",
        "Total Inc GST": r"Total Inc GST\s*\$?([\d.,]+)",
        "GST": r"GST\s*\$?([\d.,]+)",
        "Payment Due": r"Payment due by\s+([\d/]+)",
    }

    header_data = {
        f: (m.group(1).strip() if (m := re.search(p, text, re.I)) else "")
        for f, p in header_patterns.items()
    }

    if prev_header:
        for key, val in header_data.items():
            if not val:
                header_data[key] = prev_header.get(key, "")

    records = []
    for block in re.finditer(
        r"Date\s+(?:Reference\s+)?Service Provided(.+?)(?:Site\s+Total|continued overleaf|Total\s+Inc|GST\s+|\Z)",
        text,
        re.S | re.I,
    ):
        block_text = block.group(1).strip()
        before = text[: block.start()].splitlines()[-8:]
        cust, addr = "", ""
        for i in range(len(before)):
            line = before[i].strip()
            if line and re.search(r"\b(?:VIC|NSW|QLD|TAS|WA|SA|NT|ACT)\b\s*\d{3,4}", line):
                cust = before[i - 2].strip() if i >= 2 else before[i - 1].strip()
                addr = " ".join(before[i - 1 : i + 1])
                break
        header_data["Customer"], header_data["Address"] = cust, addr
        block_records = parse_invoice_lines(block_text, header_data)
        records.extend(block_records)
    return records, header_data


# ---------------------------
# Validation
# ---------------------------
def validate_invoices(df):
    if df.empty:
        return pd.DataFrame(), pd.DataFrame()
    df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce")
    validation_records = []
    mismatched_lines = []

    for inv, group in df.groupby("Tax Invoice"):
        sum_amount = group["Amount"].sum()
        expected_gst = round(sum_amount * 0.10, 2)
        calc_total_inc = round(sum_amount + expected_gst, 2)

        reported_total = group["Total Inc GST"].iloc[0]
        try:
            reported_total = float(str(reported_total).replace(",", ""))
        except Exception:
            reported_total = None

        status = "MATCH"
        if reported_total and abs(calc_total_inc - reported_total) >= 1.00:
            status = "MISMATCH"

        validation_records.append(
            {
                "Tax Invoice": inv,
                "Extracted Sum": round(sum_amount, 2),
                "Expected GST (10%)": expected_gst,
                "Calculated Total Inc GST": calc_total_inc,
                "Reported Total Inc GST": reported_total,
                "Status": status,
            }
        )

        if status == "MISMATCH":
            mismatched_lines.extend(group.to_dict(orient="records"))

    return pd.DataFrame(validation_records), pd.DataFrame(mismatched_lines)


# ---------------------------
# STREAMLIT APP (with mismatch highlighting)
# ---------------------------
st.set_page_config(page_title="NEW VEOLIA(SUEZ) Invoice Parser", layout="wide")
st.title("📄 Invoice Parser NEW VEOLIA(SUEZ)")

uploaded_file = st.file_uploader("Upload a PDF Invoice", type=["pdf"])
show_raw = st.checkbox("Show raw extracted text (helpful for debugging)", value=False)

if uploaded_file:
    uploaded_bytes = uploaded_file.read()
    texts = extract_text_from_pdf(BytesIO(uploaded_bytes))

    if show_raw:
        st.subheader("Raw page texts")
        for i, t in enumerate(texts, start=1):
            st.markdown(f"**Page {i}**")
            st.text(t[:1000] + ("..." if len(t) > 1000 else ""))

    all_records = []
    prev_header = None
    for page_text in texts:
        records, prev_header = parse_invoice(page_text, prev_header)
        all_records.extend(records)

    df = pd.DataFrame(all_records)
    validation_df, mismatched_df = validate_invoices(df)

    st.subheader("Invoice Summary")

    # ✅ Highlight MISMATCH rows in red
    def highlight_mismatch(row):
        return ["background-color: #fdd" if row["Status"] == "MISMATCH" else "" for _ in row]

    styled_df = validation_df.style.apply(highlight_mismatch, axis=1)
    st.dataframe(styled_df, use_container_width=True)

    # Export summary to Excel
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        validation_df.to_excel(writer, sheet_name="Invoice_Summary", index=False)
        if not mismatched_df.empty:
            mismatched_df.to_excel(writer, sheet_name="Mismatched_Lines", index=False)

    st.download_button(
        label="📥 Download Excel Report",
        data=output.getvalue(),
        file_name="invoice_summary.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    st.success(f"✅ Processed {len(validation_df)} invoices")

else:
    st.info("Upload a single PDF invoice to parse.")
