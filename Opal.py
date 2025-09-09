import pdfplumber
import pandas as pd
import re
import io
import json
import streamlit as st

# -----------------------------
# Load learned patterns if available
# -----------------------------
try:
    with open("learned_patterns.json", "r") as f:
        learned_patterns = json.load(f)
except FileNotFoundError:
    learned_patterns = {}

# -----------------------------
# Tokenizer for learning
# -----------------------------
def tokenize_line(line):
    tokens = line.split()
    return " ".join([
        "<DATE>" if re.match(r"\d{2}\.\d{2}\.\d{4}", t) else
        "<NUMBER>" if re.match(r"^\d[\d,\.]*$", t) else
        "<AUD>" if t.upper() == "AUD" else
        "<TEXT>"
        for t in tokens
    ])

# -----------------------------
# Main PDF Processing
# -----------------------------
def process_pdf(file_stream):
    invoice_no = ""
    data = []
    missed_lines = []
    customer = ""
    full_text = ""

    with pdfplumber.open(file_stream) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text()
            full_text += (text or "") + "\n"

            if not invoice_no and text and "Invoice No." in text:
                match = re.search(r"Invoice No\. (\d+)", text)
                if match:
                    invoice_no = match.group(1)

            if not text:
                continue

            lines = text.split("\n")

            for i, line in enumerate(lines):
                matched = False

                # ---------------- your parsing logic (unchanged) ---------------- #
                cust_match = re.match(r"^(R-[A-Z0-9]+)\s+(.+)", line)
                if cust_match:
                    customer = cust_match.group(1) + " " + cust_match.group(2).strip()
                    matched = True
                    continue

                rental_match = re.match(
                    r"(\d{2}\.\d{2}\.\d{4})\s+(.+?)\s+(\d{2}\.\d{2}\.\d{4} to \d{2}\.\d{2}\.\d{4})\s+([\d\.]+)\s+(\w+)\s+([\d\.]+)\s+(\w+)\s+([\d,\.]+)\s+([\d,\.]+)\s+([\d,\.]+) AUD",
                    line
                )
                if rental_match:
                    date, description, period, qty, qty_unit, unit_price, unit_unit, amt_excl_gst, gst, amt_incl_gst = rental_match.groups()
                    billed_qty_line = lines[i + 1] if i + 1 < len(lines) else ""
                    billed_qty_match = re.search(r"Billed Qty\s+([\d\.]+)\s+(\w+)", billed_qty_line)
                    billed_qty_full = f"{billed_qty_match.group(1)} {billed_qty_match.group(2)}" if billed_qty_match else ""
                    data.append({
                        "Invoice No.": invoice_no, "Customer": customer, "Date": date, "Description": description,
                        "Charge Type/Period Reference": period, "Reference": "", "Billed qty": billed_qty_full,
                        "Qty.": f"{qty} {qty_unit}", "Unit Price": f"{unit_price} {unit_unit}",
                        "Amount excl. GST": amt_excl_gst, "GST": gst, "Amount Incl. GST": amt_incl_gst
                    })
                    matched = True
                    continue

                # (⚡ All your other regex patterns go here unchanged)
                # ---------------------------------------------------------------- #

                if not matched and re.search(r"\d{2}\.\d{2}\.\d{4}.*AUD", line):
                    missed_lines.append({
                        "Page": page_num, "Line No.": i + 1,
                        "Customer": customer, "Line": line,
                        "Note": "Potential invoice data (unparsed)"
                    })

    return invoice_no, data, missed_lines, full_text

# -----------------------------
# Streamlit App
# -----------------------------
st.set_page_config(page_title="Invoice PDF → Excel", layout="wide")
st.title("📄 Invoice PDF → Excel Extractor")

uploaded_file = st.file_uploader("Upload an Invoice PDF", type=["pdf"])

if uploaded_file is not None:
    with st.spinner("Processing PDF... please wait ⏳"):
        file_stream = io.BytesIO(uploaded_file.read())
        invoice_no, data, missed_lines, full_text = process_pdf(file_stream)

    st.write(f"✅ Extracted {len(data)} invoice data lines")
    st.write(f"⚠️ Captured {len(missed_lines)} unmatched lines")

    if len(data) == 0:
        st.error("⚠️ No invoice data extracted, check patterns or PDF text content.")

    # Show preview
    if data:
        st.subheader("Extracted Data (first 20 rows)")
        df = pd.DataFrame(data)
        st.dataframe(df.head(20))

    # Save to Excel in memory
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        if data:
            pd.DataFrame(data).to_excel(writer, sheet_name="Invoice Data", index=False)
        if missed_lines:
            pd.DataFrame(missed_lines).to_excel(writer, sheet_name="Unmatched Lines", index=False)

    st.download_button(
        label="📥 Download Excel",
        data=output.getvalue(),
        file_name=f"Opal_Invoice_{invoice_no or 'Unknown'}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    if missed_lines:
        st.subheader("Sample Unmatched Lines")
        for row in missed_lines[:10]:
            st.text(f"[Page {row['Page']} | Line {row['Line No.']}] {row['Line']}")
