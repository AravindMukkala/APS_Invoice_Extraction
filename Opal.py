import streamlit as st
import pandas as pd
import pdfplumber
import re
import io

# ---------------------------
# PDF Processing Function
# ---------------------------
def process_pdf(file_content):
    invoice_no = ""
    data = []
    missed_lines = []
    customer = ""
    full_text = ""

    with pdfplumber.open(file_content) as pdf:
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

                # Example: capture customer
                cust_match = re.match(r"^(R-[A-Z0-9]+)\s+(.+)", line)
                if cust_match:
                    customer = cust_match.group(1) + " " + cust_match.group(2).strip()
                    matched = True
                    continue

                # Example: capture Rental pattern
                rental_match = re.match(
                    r"(\d{2}\.\d{2}\.\d{4})\s+(.+?)\s+(\d{2}\.\d{2}\.\d{4} to \d{2}\.\d{2}\.\d{4}).*([\d,\.]+)\s+AUD",
                    line
                )
                if rental_match:
                    date, description, period, amt_incl_gst = rental_match.groups()
                    data.append({
                        "Invoice No.": invoice_no,
                        "Customer": customer,
                        "Date": date,
                        "Description": description.strip(),
                        "Charge Type/Period Reference": period,
                        "Amount Incl. GST": amt_incl_gst
                    })
                    matched = True
                    continue

                # If looks like invoice line but no match
                if not matched and re.search(r"\d{2}\.\d{2}\.\d{4}.*AUD", line):
                    missed_lines.append({
                        "Page": page_num,
                        "Line No.": i + 1,
                        "Customer": customer,
                        "Line": line
                    })

    return invoice_no, data, missed_lines, full_text


# ---------------------------
# Streamlit UI
# ---------------------------
st.title("📄 Invoice → Excel Converter")

uploaded_file = st.file_uploader("Upload a PDF invoice", type=["pdf"])

if uploaded_file:
    st.info("⏳ Processing PDF, please wait...")
    invoice_no, data, missed_lines, full_text = process_pdf(uploaded_file)

    if data:
        df = pd.DataFrame(data)
        st.success(f"✅ Extracted {len(df)} invoice lines (Invoice {invoice_no})")

        # Show preview
        st.dataframe(df.head(10))

        # Save to Excel in memory
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Invoice Data", index=False)
            pd.DataFrame(missed_lines).to_excel(writer, sheet_name="Unmatched Lines", index=False)

        # Download button
        st.download_button(
            label="📥 Download Excel",
            data=output.getvalue(),
            file_name=f"Opal_Invoice_{invoice_no}_Full_Data.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    else:
        st.error("⚠️ No invoice data extracted. Check patterns or PDF content.")
