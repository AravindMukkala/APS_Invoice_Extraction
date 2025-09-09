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

    with pdfplumber.open(file_stream) as pdf:
        full_text = ""
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

                # Match customer line
                cust_match = re.match(r"^(R-[A-Z0-9]+)\s+(.+)", line)
                if cust_match:
                    customer = cust_match.group(1) + " " + cust_match.group(2).strip()
                    matched = True
                    continue

                # Example pattern (Rental)
                rental_match = re.match(
                    r"(\d{2}\.\d{2}\.\d{4})\s+(.+?)\s+(\d{2}\.\d{2}\.\d{4} to \d{2}\.\d{2}\.\d{4})\s+([\d\.]+)\s+(\w+)\s+([\d\.]+)\s+(\w+)\s+([\d,\.]+)\s+([\d,\.]+)\s+([\d,\.]+) AUD",
                    line
                )
                if rental_match:
                    date, description, period, qty, qty_unit, unit_price, unit_unit, amt_excl_gst, gst, amt_incl_gst = rental_match.groups()
                    data.append({
                        "Invoice No.": invoice_no, "Customer": customer, "Date": date,
                        "Description": description, "Charge Type/Period Reference": period,
                        "Reference": "", "Billed qty": f"{qty} {qty_unit}",
                        "Qty.": f"{qty} {qty_unit}", "Unit Price": f"{unit_price} {unit_unit}",
                        "Amount excl. GST": amt_excl_gst, "GST": gst, "Amount Incl. GST": amt_incl_gst
                    })
                    matched = True
                    continue

                # 🔁 Apply learned patterns if no match yet
                if not matched:
                    for token_pattern, pattern_data in learned_patterns.items():
                        regex = pattern_data["regex"]
                        field_map = pattern_data["field_map"]
                        charge_type = pattern_data.get("Charge Type", "")

                        tokens = line.split()
                        current_token_pattern = " ".join([
                            "<DATE>" if re.match(r"\d{2}\.\d{2}\.\d{4}", t)
                            else "<NUMBER>" if re.match(r"^\d[\d,\.]*$", t)
                            else "<TEXT>"
                            for t in tokens
                        ])

                        if current_token_pattern == token_pattern:
                            match = re.match(regex, line)
                            if match:
                                groups = match.groups()
                                parsed = {
                                    "Invoice No.": invoice_no,
                                    "Customer": customer,
                                    "Charge Type/Period Reference": charge_type,
                                }
                                for field, group_index in field_map.items():
                                    parsed[field] = groups[group_index - 1]
                                data.append(parsed)
                                matched = True
                                break

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

    if len(data) == 0:
        st.error("⚠️ No invoice data extracted. Please check patterns or PDF content.")
    else:
        st.success(f"✅ Extracted {len(data)} invoice data lines")
        df = pd.DataFrame(data)
        st.dataframe(df.head(20))

        # Save to Excel in memory
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Invoice Data", index=False)
            if missed_lines:
                pd.DataFrame(missed_lines).to_excel(writer, sheet_name="Unmatched Lines", index=False)

        st.download_button(
            label="📥 Download Excel",
            data=output.getvalue(),
            file_name=f"Opal_Invoice_{invoice_no or 'Unknown'}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    if missed_lines:
        st.warning(f"⚠️ Found {len(missed_lines)} unmatched lines. They were added to 'Unmatched Lines' sheet.")

