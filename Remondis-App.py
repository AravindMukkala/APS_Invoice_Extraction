import streamlit as st
import pdfplumber
import pandas as pd
import re
import io
import gc

# ============================================================
# 🔧 OPTIMIZED INVOICE EXTRACTOR (no logic loss, faster parsing)
# ============================================================

def extract_invoice_data(pdf_file):
    status = st.empty()
    progress = st.progress(0, text="Starting extraction...")

    all_headers_dict = {}
    all_lines = []
    all_bookings = []

    # --- STEP 1: Read & Chunk PDF ---
    with pdfplumber.open(pdf_file) as pdf:
        total_pages = len(pdf.pages)
        status.text(f"📖 PDF opened ({total_pages} pages)...")

        invoice_chunks = []
        current_chunk = []

        for i, page in enumerate(pdf.pages, 1):
            text = page.extract_text(layout=False) or ""
            if "Tax Invoice" in text and current_chunk:
                invoice_chunks.append(current_chunk)
                current_chunk = []
            current_chunk.append(text)  # store text only (not page object)
            if i % 10 == 0 or i == total_pages:
                progress.progress(int((i / total_pages) * 25), text=f"Reading pages ({i}/{total_pages})...")

        if current_chunk:
            invoice_chunks.append(current_chunk)

    progress.progress(30, text=f"Found {len(invoice_chunks)} invoice chunks. Parsing details...")

    # --- Pre-compile regex for speed ---
    re_footer = re.compile(r"Tax Invoice:.*Invoice Date:.*Acc:")
    re_invoice = re.compile(r"Tax Invoice:\s*(\d+)")
    re_invdate = re.compile(r"Invoice Date:\s*([0-9/]+)")
    re_acc = re.compile(r"Acc:\s*([\d.]+)")
    re_name = re.compile(r"Acc:\s*[\d.]+\s+(.*)")
    re_bill = re.compile(r"Billing Period\s+([0-9/]+ to [0-9/]+)")
    re_total = re.compile(r"Total\s+\$([0-9.,]+)")
    re_site = re.compile(r"Services\s*/\s*Site:\s+([A-Za-z0-9.]+)")
    re_booking = re.compile(r"^(\d{2}/\d{2}/\d{2})\s+([\d.]+)\s+(.+?)\s+(\d+)\s+\$([\d.,]+)\s+\$([\d.,]+)")
    re_disposal = re.compile(r"^(\d{2}/\d{2}/\d{2})\s+([\d.]+)\s+(.+?)\s+([\d.,]+)\s+\w+\s+([\d.,]+)\s+\$([\d.,]+)\s+\$([\d.,]+)")
    re_inline = re.compile(r"(\d+)\s*\$([\d.,]+)\s*\$([\d.,]+)")

    # --- STEP 2: Process Each Invoice ---
    total_chunks = len(invoice_chunks)
    update_every = max(1, total_chunks // 20)

    for idx, chunk_texts in enumerate(invoice_chunks, 1):
        text = chunk_texts[0]
        lines = text.splitlines()
        header = {}

        footer_line = next((l for l in lines if re_footer.search(l)), None)
        if footer_line:
            if (m := re_invoice.search(footer_line)): header["Tax Invoice"] = m.group(1)
            if (m := re_invdate.search(footer_line)): header["Invoice Date"] = m.group(1)
            if (m := re_acc.search(footer_line)): header["Account Number"] = m.group(1).split('.')[0]
            if (m := re_name.search(footer_line)): header["Customer Name"] = m.group(1).strip()

        try:
            cust_idx = next(
                i for i, l in enumerate(lines)
                if ("PTY LTD" in l or "UNIT TRUST" in l)
                and "REMONDIS" not in l and not l.strip().startswith("Page:")
            )
            header["Customer Name"] = lines[cust_idx].strip()
        except StopIteration:
            header.setdefault("Customer Name", "")

        if "Tax Invoice" not in header:
            if (m := re_invoice.search(text)): header["Tax Invoice"] = m.group(1)

        if (m := re_acc.search(text)): header["Account Number"] = m.group(1).split('.')[0]
        if (m := re_bill.search(text)): header["Billing Period"] = m.group(1)
        if (m := re_invdate.search(text)): header["Invoice Date"] = m.group(1)
        if (m := re_total.search(text)): header["Total Amount"] = m.group(1)
        if (m := re_site.search(text)): header["Service Site"] = m.group(1)

        inv_no = header.get("Tax Invoice", f"INV_{idx}")
        all_headers_dict.setdefault(inv_no, header)

        # --- LINE EXTRACTION ---
        for page_text in chunk_texts:
            lines = page_text.splitlines()
            skip_next = False
            for i, line in enumerate(lines):
                if skip_next:
                    skip_next = False
                    continue
                line = line.strip()
                if not line:
                    continue

                if line.startswith("Site:"):
                    raw_text = re.split(r"\b(Total:|Totals|Page:|Tax Invoice:)", line)[0].strip()
                    qty, price, total_val = "", "", ""
                    description = raw_text

                    if (m := re_inline.search(raw_text)):
                        qty, price, total_val = m.groups()
                        description = raw_text[:m.start()].strip()
                    else:
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

                    record = {
                        "Invoice Number": inv_no,
                        "Account Number": header.get("Account Number", ""),
                        "Service Site": header.get("Service Site", ""),
                        "Invoice Date": header.get("Invoice Date", ""),
                        "Date": "",
                        "Ref No": "",
                        "Description": description.strip(),
                        "PO": "",
                        "Qty": qty,
                        "Price": price,
                        "Total": total_val,
                        "Charge Type": "Rental",
                    }
                    all_lines.append(record)
                    all_bookings.append(record)
                    continue

                clean_line = re.split(r"\b(Totals|Total:|Page:|Tax Invoice:)", line)[0].strip()
                if (m := re_booking.match(clean_line)):
                    date_, ref_no, desc, po, price, total_val = m.groups()
                    qty = "1"
                    ctype = "Booking"
                elif (m := re_disposal.match(clean_line)):
                    date_, ref_no, desc, qty1, qty2, price, total_val = m.groups()
                    po = ""
                    qty = qty2
                    ctype = "Disposal"
                else:
                    continue

                record = {
                    "Invoice Number": inv_no,
                    "Account Number": header.get("Account Number", ""),
                    "Service Site": header.get("Service Site", ""),
                    "Invoice Date": header.get("Invoice Date", ""),
                    "Date": date_,
                    "Ref No": ref_no,
                    "Description": desc.strip(),
                    "PO": po,
                    "Qty": qty,
                    "Price": price,
                    "Total": total_val,
                    "Charge Type": ctype,
                }
                all_lines.append(record)
                all_bookings.append(record)

        if idx % update_every == 0 or idx == total_chunks:
            progress.progress(30 + int((idx / total_chunks) * 60),
                              text=f"Processing invoice {idx}/{total_chunks}...")

    # --- STEP 3: Build DataFrames ---
    headers_df = pd.DataFrame(list(all_headers_dict.values()))
    lines_df = pd.DataFrame(all_lines)
    bookings_df = pd.DataFrame(all_bookings)

    # --- Clean numeric fields ---
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

    # --- Validation ---
    GST = 0.10
    validation = []
    if not bookings_df.empty and not headers_df.empty:
        for _, h in headers_df.iterrows():
            inv = h.get("Tax Invoice")
            total = h.get("Total Amount", 0)
            inv_bookings = bookings_df[bookings_df["Invoice Number"] == inv]
            sum_total = inv_bookings["Total"].sum() if not inv_bookings.empty else 0
            sum_with_gst = sum_total * (1 + GST)
            valid = pd.isna(total) or abs(sum_with_gst - total) < 0.01
            validation.append({
                "Invoice Number": inv,
                "Expected Total": total,
                "Sum of Bookings": sum_total,
                "Sum with GST (10%)": sum_with_gst,
                "Valid": valid
            })
    validation_df = pd.DataFrame(validation)

    progress.progress(95, text="Building Excel output...")

    # --- STEP 4: Output Excel ---
    billing_periods = {h.get("Billing Period", "") for h in all_headers_dict.values() if h.get("Billing Period")}
    safe_periods = "_".join(bp.replace(" ", "").replace("/", "-") for bp in billing_periods) if billing_periods else "Unknown"
    output_file = f"Remondis_Invoice_Data_{safe_periods}.xlsx"

    temp_path = "/tmp/" + output_file
    with pd.ExcelWriter(temp_path, engine="openpyxl") as writer:
        headers_df.to_excel(writer, sheet_name="Invoice Headers", index=False)
        lines_df.to_excel(writer, sheet_name="Line Items", index=False)
        bookings_df.to_excel(writer, sheet_name="Bookings", index=False)
        validation_df.to_excel(writer, sheet_name="Validation", index=False)

    gc.collect()
    progress.progress(100, text="✅ Done!")

    return headers_df, lines_df, bookings_df, validation_df, temp_path, output_file


# ============================================================
# 🖥️ STREAMLIT APP LAYOUT
# ============================================================

st.set_page_config(page_title="Remondis Invoice Extractor", layout="wide")
st.title("📑 Remondis Invoice Extractor")
st.write("Upload a PDF Tax Invoice to extract structured data (Bookings, Disposal, Rentals).")

uploaded_file = st.file_uploader("Upload PDF", type="pdf")

if uploaded_file:
    with st.spinner("Processing PDF..."):
        headers_df, lines_df, bookings_df, validation_df, temp_path, output_file = extract_invoice_data(uploaded_file)

    st.success("✅ Extraction complete!")

    with st.expander("📋 Invoice Headers", expanded=False):
        st.dataframe(headers_df.head(20))

    with st.expander("📋 Bookings", expanded=False):
        st.dataframe(bookings_df.head(50))

    with st.expander("📊 Validation Results", expanded=True):
        st.dataframe(validation_df)

    with open(temp_path, "rb") as f:
        st.download_button(
            label="📥 Download Excel File",
            data=f,
            file_name=output_file,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
