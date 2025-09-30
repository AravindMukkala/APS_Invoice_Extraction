import streamlit as st
import pdfplumber
import pandas as pd
import re
import io

def extract_invoice_data(pdf_file):
    status = st.empty()  # Streamlit status updater
    status.text("Starting extraction...")

    all_headers_dict = {}
    all_lines = []
    all_bookings = []

    with pdfplumber.open(pdf_file) as pdf:
        total_pages = len(pdf.pages)
        status.text(f"PDF opened, total pages: {total_pages}")
        invoice_chunks = []
        current_chunk = []

        for i, page in enumerate(pdf.pages, 1):
            text = page.extract_text()
            if "Tax Invoice" in text and current_chunk:
                invoice_chunks.append(current_chunk)
                current_chunk = []
            current_chunk.append(page)
            status.text(f"Reading page {i} of {total_pages}...")

        if current_chunk:
            invoice_chunks.append(current_chunk)

    status.text(f"Found {len(invoice_chunks)} invoice chunks, processing...")

    for idx, chunk in enumerate(invoice_chunks, 1):
        status.text(f"Processing invoice chunk {idx} of {len(invoice_chunks)}...")
        first_page = chunk[0]
        text = first_page.extract_text()
        lines = text.splitlines()
        header = {}

        footer_line = next((l for l in lines if re.search(r"Tax Invoice:.*Invoice Date:.*Acc:", l)), None)
        if footer_line:
            invoice_match = re.search(r"Tax Invoice:\s*(\d+)", footer_line)
            date_match = re.search(r"Invoice Date:\s*([0-9/]+)", footer_line)
            acc_match = re.search(r"Acc:\s*([\d.]+)", footer_line)
            name_match = re.search(r"Acc:\s*[\d.]+\s+(.*)", footer_line)

            if invoice_match:
                header["Tax Invoice"] = invoice_match.group(1)
            if date_match:
                header["Invoice Date"] = date_match.group(1)
            if acc_match:
                header["Account Number"] = acc_match.group(1).split('.')[0]
            if name_match:
                header["Customer Name"] = name_match.group(1).strip()

        try:
            cust_idx = next(
                i for i, l in enumerate(lines)
                if ("PTY LTD" in l or "UNIT TRUST" in l)
                and "REMONDIS" not in l
                and not l.strip().startswith("Page:")
            )
            header["Customer Name"] = lines[cust_idx].strip()
        except StopIteration:
            header.setdefault("Customer Name", "")

        if "Tax Invoice" not in header or not header["Tax Invoice"]:
            match = re.search(r"Tax Invoice\s+(\d+)", text)
            if match:
                header["Tax Invoice"] = match.group(1)

        acc = re.search(r"Account Number\s+([\d.]+)", text)
        if acc:
            header["Account Number"] = acc.group(1).split('.')[0]

        bill = re.search(r"Billing Period\s+([0-9/]+ to [0-9/]+)", text)
        header["Billing Period"] = bill.group(1) if bill else ""

        date = re.search(r"Invoice Date\s+([0-9/]+)", text)
        if date:
            header["Invoice Date"] = date.group(1)

        total = re.search(r"Total\s+\$([0-9.,]+)", text)
        header["Total Amount"] = total.group(1) if total else ""

        site = re.search(r"Services\s*/\s*Site:\s+([A-Za-z0-9.]+)", text)
        header["Service Site"] = site.group(1) if site else ""

        invoice_no = header.get("Tax Invoice")
        if invoice_no:
            if invoice_no not in all_headers_dict:
                all_headers_dict[invoice_no] = header
            else:
                for key, val in header.items():
                    if not all_headers_dict[invoice_no].get(key) and val:
                        all_headers_dict[invoice_no][key] = val

        for idx_page, page in enumerate(chunk):
            text = page.extract_text()
            lines = text.splitlines()

            if idx_page != 0:
                footer_line = next((l for l in lines if re.search(r"Tax Invoice:.*Invoice Date:.*Acc:", l)), None)
                if footer_line:
                    invoice_match = re.search(r"Tax Invoice:\s*(\d+)", footer_line)
                    date_match = re.search(r"Invoice Date:\s*([0-9/]+)", footer_line)
                    acc_match = re.search(r"Acc:\s*([\d.]+)", footer_line)
                    name_match = re.search(r"Acc:\s*[\d.]+\s+(.*)", footer_line)

                    if invoice_match:
                        header["Tax Invoice"] = invoice_match.group(1)
                    if date_match:
                        header["Invoice Date"] = date_match.group(1)
                    if acc_match:
                        header["Account Number"] = acc_match.group(1).split('.')[0]
                    if name_match:
                        header["Customer Name"] = name_match.group(1).strip()

            for line in lines:
                line = line.strip()
                match = re.match(r"^(\d{2}/\d{2}/\d{2})\s+([\d.]+)\s+(.+?)\s+(\d+)\s+\$([\d.,]+)\s+\$([\d.,]+)", line)
                if match:
                    date_, ref_no, description, po, price, total_ = match.groups()

                    line_item = {
                        "Invoice Number": header.get("Tax Invoice", ""),
                        "Date": date_,
                        "Ref No": ref_no,
                        "Description": description.strip(),
                        "PO": po,
                        "Qty": "1",
                        "Price": price,
                        "Total": total_,
                    }
                    all_lines.append(line_item)

                    booking_item = {
                        "Invoice Number": header.get("Tax Invoice", ""),
                        "Account Number": header.get("Account Number", ""),
                        "Service Site": header.get("Service Site", ""),
                        "Invoice Date": header.get("Invoice Date", ""),
                        "Date": date_,
                        "Ref No": ref_no,
                        "Description": description.strip(),
                        "PO": po,
                        "Qty": "1",
                        "Price": price,
                        "Total": total_,
                    }
                    all_bookings.append(booking_item)

    # --- Create DataFrames ---
    headers_df = pd.DataFrame(list(all_headers_dict.values()))
    lines_df = pd.DataFrame(all_lines)
    bookings_df = pd.DataFrame(all_bookings)

    # --- Clean numeric columns ---
    if "Total Amount" in headers_df.columns:
        headers_df["Total Amount"] = (
            headers_df["Total Amount"]
            .astype(str)
            .str.replace(r"[^\d.]", "", regex=True)
            .replace("", pd.NA)
            .astype("Float64")
        )

    for col in ["Price", "Total"]:
        if col in bookings_df.columns:
            bookings_df[col] = (
                bookings_df[col]
                .astype(str)
                .str.replace(r"[^\d.]", "", regex=True)
                .replace("", pd.NA)
                .astype("Float64")
            )

    # --- Invoice Validation with 10% GST ---
    validation_results = []
    GST_RATE = 0.10
    if not bookings_df.empty and not headers_df.empty:
        for _, header_row in headers_df.iterrows():
            invoice_no = header_row.get("Tax Invoice")
            expected_total = header_row.get("Total Amount", 0)

            invoice_bookings = bookings_df[bookings_df["Invoice Number"] == invoice_no]
            sum_total = invoice_bookings["Total"].sum() if not invoice_bookings.empty else 0

            sum_with_gst = sum_total * (1 + GST_RATE)

            is_valid = pd.isna(expected_total) or abs(sum_with_gst - expected_total) < 0.01
            validation_results.append({
                "Invoice Number": invoice_no,
                "Expected Total": expected_total,
                "Sum of Bookings": sum_total,
                "Sum with GST (10%)": sum_with_gst,
                "Valid": is_valid
            })

    validation_df = pd.DataFrame(validation_results)

    # --- Output Excel ---
    billing_periods = {h.get("Billing Period", "") for h in all_headers_dict.values() if h.get("Billing Period")}
    if billing_periods:
        safe_periods = "_".join(bp.replace(" ", "").replace("/", "-") for bp in billing_periods)
        output_file = f"Remondis_Invoice_Data_{safe_periods}.xlsx"
    else:
        output_file = "Remondis_Invoice_Data.xlsx"

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        headers_df.to_excel(writer, sheet_name="Invoice Headers", index=False)
        lines_df.to_excel(writer, sheet_name="Line Items", index=False)
        bookings_df.to_excel(writer, sheet_name="Bookings", index=False)
        validation_df.to_excel(writer, sheet_name="Validation", index=False)

    return headers_df, lines_df, bookings_df, validation_df, output, output_file


# --- STREAMLIT APP ---
st.set_page_config(page_title="Remondis Invoice Extractor", layout="wide")

st.title("📑 Remondis Invoice Extractor")
st.write("Upload a PDF Tax Invoice to extract structured data and validate totals.")

uploaded_file = st.file_uploader("Upload PDF", type="pdf")

if uploaded_file is not None:
    with st.spinner("Processing PDF..."):
        headers_df, lines_df, bookings_df, validation_df, output, output_file = extract_invoice_data(uploaded_file)

    st.success("✅ Extraction & validation complete!")

    st.subheader("Invoice Headers")
    st.dataframe(headers_df)

    st.subheader("Line Items")
    st.dataframe(lines_df)

    st.subheader("Bookings")
    st.dataframe(bookings_df)

    st.subheader("Validation Results")
    st.dataframe(validation_df)

    st.download_button(
        label="📥 Download Excel File",
        data=output.getvalue(),
        file_name=output_file,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
