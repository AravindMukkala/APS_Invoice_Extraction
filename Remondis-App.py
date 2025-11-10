import streamlit as st
import pdfplumber
import pandas as pd
import re
import io
import gc
import time

def extract_invoice_data(pdf_file):
    status = st.empty()  # Streamlit status text
    progress = st.progress(0, text="Starting extraction...")

    all_headers_dict = {}
    all_lines = []
    all_bookings = []

    with pdfplumber.open(pdf_file) as pdf:
        total_pages = len(pdf.pages)
        progress.progress(0, text=f"PDF opened — {total_pages} pages detected")
        invoice_chunks = []
        current_chunk = []

        # --- Identify invoice chunks ---
        for i, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            if "Tax Invoice" in text and current_chunk:
                invoice_chunks.append(current_chunk)
                current_chunk = []
            current_chunk.append(page)
            progress.progress(int(i / total_pages * 30), text=f"Reading page {i}/{total_pages}...")
        if current_chunk:
            invoice_chunks.append(current_chunk)

    progress.progress(35, text=f"Found {len(invoice_chunks)} invoice chunks, processing...")

    for idx, chunk in enumerate(invoice_chunks, 1):
        progress.progress(int(35 + (idx / len(invoice_chunks)) * 50),
                          text=f"Processing invoice {idx}/{len(invoice_chunks)}...")

        first_page = chunk[0]
        text = first_page.extract_text() or ""
        lines = text.splitlines()
        header = {}

        # --- Extract invoice header info ---
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

        # --- Parse line items ---
        skip_next = False
        for idx_page, page in enumerate(chunk):
            text = page.extract_text() or ""
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

            for i, line in enumerate(lines):
                if skip_next:
                    skip_next = False
                    continue

                line = line.strip()

                # --- Rental / Period Charges ---
                if line.startswith("Site:"):
                    raw_text = re.split(r"\b(Total:|Totals|Page:|Tax Invoice:)", line)[0].strip()
                    qty, price, total_val = "", "", ""

                    match_inline = re.search(r"(\d+)\s*\$([\d.,]+)\s*\$([\d.,]+)", raw_text)
                    if match_inline:
                        qty, price, total_val = match_inline.groups()
                        description = raw_text[:match_inline.start()].strip()
                    else:
                        description = raw_text
                        j = i + 1
                        while j < len(lines):
                            next_line = lines[j].strip()
                            if re.match(r"(Totals|Total:|Page:|Tax Invoice:)", next_line):
                                break
                            match_rental = re.match(r"(\d+)\s*x?\s*(\d*)\s*\$([\d.,]+)\s*\$([\d.,]+)\s*(.*)", next_line)
                            if match_rental:
                                units, qty2, price, total_val, extra = match_rental.groups()
                                qty = qty2 if qty2 else units
                                if extra.strip():
                                    description += " " + extra.strip()
                                skip_next = True
                                break
                            else:
                                description += " " + next_line
                            j += 1

                    line_item = {
                        "Invoice Number": header.get("Tax Invoice", ""),
                        "Date": "",
                        "Ref No": "",
                        "Description": description.strip(),
                        "PO": "",
                        "Qty": qty,
                        "Price": price,
                        "Total": total_val,
                        "Charge Type": "Rental",
                    }
                    all_lines.append(line_item)

                    booking_item = line_item.copy()
                    booking_item.update({
                        "Account Number": header.get("Account Number", ""),
                        "Service Site": header.get("Service Site", ""),
                        "Invoice Date": header.get("Invoice Date", ""),
                    })
                    all_bookings.append(booking_item)
                    continue

                # --- Booking / Disposal Lines ---
                clean_line = re.split(r"\b(Totals|Total:|Page:|Tax Invoice:)", line)[0].strip()

                match_booking = re.match(
                    r"^(\d{2}/\d{2}/\d{2})\s+([\d.]+)\s+(.+?)\s+(\d+)\s+\$([\d.,]+)\s+\$([\d.,]+)",
                    clean_line
                )
                match_disposal = re.match(
                    r"^(\d{2}/\d{2}/\d{2})\s+([\d.]+)\s+(.+?)\s+([\d.,]+)\s+\w+\s+([\d.,]+)\s+\$([\d.,]+)\s+\$([\d.,]+)",
                    clean_line
                )

                if match_booking:
                    date_, ref_no, description, po, price, total_val = match_booking.groups()
                    qty = "1"
                    charge_type = "Booking"
                elif match_disposal:
                    date_, ref_no, description, qty1, qty2, price, total_val = match_disposal.groups()
                    po = ""
                    qty = qty2
                    charge_type = "Disposal"
                else:
                    continue

                line_item = {
                    "Invoice Number": header.get("Tax Invoice", ""),
                    "Date": date_,
                    "Ref No": ref_no,
                    "Description": description.strip(),
                    "PO": po,
                    "Qty": qty,
                    "Price": price,
                    "Total": total_val,
                    "Charge Type": charge_type,
                }
                all_lines.append(line_item)

                booking_item = line_item.copy()
                booking_item.update({
                    "Account Number": header.get("Account Number", ""),
                    "Service Site": header.get("Service Site", ""),
                    "Invoice Date": header.get("Invoice Date", ""),
                })
                all_bookings.append(booking_item)

        gc.collect()
        time.sleep(0.1)

    progress.progress(90, text="Building dataframes...")

    # --- Create DataFrames ---
    headers_df = pd.DataFrame(list(all_headers_dict.values()))
    lines_df = pd.DataFrame(all_lines)
    bookings_df = pd.DataFrame(all_bookings)

    # --- Clean numeric columns ---
    for col in ["Total Amount"]:
        if col in headers_df.columns:
            headers_df[col] = (
                headers_df[col].astype(str)
                .str.replace(r"[^\d.]", "", regex=True)
                .replace("", pd.NA)
                .astype("Float64")
            )

    for col in ["Price", "Total"]:
        if col in bookings_df.columns:
            bookings_df[col] = (
                bookings_df[col].astype(str)
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
    safe_periods = "_".join(bp.replace(" ", "").replace("/", "-") for bp in billing_periods) if billing_periods else ""
    output_file = f"Remondis_Invoice_Data_{safe_periods or 'output'}.xlsx"

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        headers_df.to_excel(writer, sheet_name="Invoice Headers", index=False)
        lines_df.to_excel(writer, sheet_name="Line Items", index=False)
        bookings_df.to_excel(writer, sheet_name="Bookings", index=False)
        validation_df.to_excel(writer, sheet_name="Validation", index=False)

    progress.progress(100, text="✅ Extraction & validation complete!")

    return headers_df, lines_df, bookings_df, validation_df, output, output_file


# --- STREAMLIT APP ---
st.set_page_config(page_title="Remondis Invoice Extractor", layout="wide")
st.title("📑 Remondis Invoice Extractor")
st.write("Upload a PDF Tax Invoice to extract structured data, including Bookings, Disposal & Rentals.")

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
