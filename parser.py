import io
import re
import csv
import pdfplumber
import pandas as pd
from rapidfuzz import process, fuzz
import streamlit as st

# --- Site name corrections cache ---
site_name_corrections = {}

def save_corrections():
    with open("site_name_corrections.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["raw_name", "corrected_name"])
        for k, v in site_name_corrections.items():
            writer.writerow([k, v])

def fuzzy_match_site_name(raw_name, master_site_names, threshold=80):
    if raw_name in site_name_corrections:
        return site_name_corrections[raw_name]
    match = process.extractOne(raw_name, master_site_names, scorer=fuzz.token_sort_ratio)
    if match and match[1] >= threshold:
        corrected = match[0]
        site_name_corrections[raw_name] = corrected
        save_corrections()
        return corrected
    return raw_name

def normalize_state(raw_state):
    state_map = {
        "new south wales": "NSW", "victoria": "VIC", "queensland": "QLD",
        "south australia": "SA", "western australia": "WA", "tasmania": "TAS",
        "northern territory": "NT", "australian capital territory": "ACT",
        "nsw": "NSW", "vic": "VIC", "qld": "QLD", "sa": "SA", "wa": "WA",
        "tas": "TAS", "nt": "NT", "act": "ACT"
    }
    rs = raw_state.strip().lower()
    return state_map.get(rs, raw_state.upper())

def extract_invoice_metadata(text):
    def safe_search(pattern, txt, default=""):
        m = re.search(pattern, txt, re.IGNORECASE)
        return m.group(1).strip() if m else default
    return {
        "Tax Invoice": safe_search(r"Tax Invoice\s*(\d+)", text),
        "Account Number": safe_search(r"Account Number\s*([\d.]+)", text),
        "Billing Period": safe_search(r"Billing Period\s*([^\n]+)", text),
        "Invoice Date": safe_search(r"Invoice Date\s*([^\n]+)", text),
        "Total": safe_search(r"Total\s*([\d.,]+)", text)
    }

def extract_invoice_totals_excl_gst(text):
    matches = re.findall(r"Total\s*\(Excl\.?GST\)\s*[: ]\s*([\d,]+\.\d{2})", text, re.IGNORECASE)
    totals = [float(m.replace(",", "")) for m in matches]
    return sum(totals), totals

def parse_site_line(line, master_site_names):
    raw = line.replace("Services / Site:", "").strip()
    m = re.match(r"^(\S+)\s+(.*)$", raw)
    if not m:
        st.warning(f"WARNING: Could not extract site_code from: {line}")
        return {}

    site_code = m.group(1)
    rest = m.group(2)

    dash_split = rest.split(" - ", 1)
    if len(dash_split) == 2:
        customer_raw, address = dash_split[0].strip(), dash_split[1].strip()
    else:
        parts = rest.split()
        if len(parts) >= 3:
            customer_raw = " ".join(parts[:2])
            address = " ".join(parts[2:])
        else:
            customer_raw = parts[0]
            address = " ".join(parts[1:]) if len(parts) > 1 else ""

    customer = fuzzy_match_site_name(customer_raw, master_site_names)

    return {
        "site_code": site_code,
        "customer": customer,
        "address": address
    }

def safe_float(val):
    try:
        return float(str(val).replace(',', '').strip())
    except:
        return 0.0

def clean_description(desc):
    patterns = [
        r"\b(EPD|TH|AGR|RYD)\w*[\\/]\d+\b",
        r"\b(EPD|TH|AGR|RYD)\w*\d+(\.\d+)?\b",
        r"\b\d{5,}(\.\d+)?\b",
        r"\b\d{3}-\d{5}\b",
        r"\bNO DOCKET\b",
        r"\bN/A\b",
        r"\bNA\b",
        r"\bT\d{5}[\\/]\d\b",
        r"\bD\d{5}[\\/]\d\b"
    ]
    cleaned_desc = desc
    for pat in patterns:
        cleaned_desc = re.sub(pat, "", cleaned_desc, flags=re.IGNORECASE)
    cleaned_desc = re.sub(r"\s{2,}", " ", cleaned_desc).strip()
    return cleaned_desc

def extract_service_lines(lines, site_info, tax_invoice):
    results = []
    unmatched_booking_lines = []
    current_entry = {}
    expecting_description_continuation = False

    for line in lines:
        line = line.strip()
        is_new_main_line = bool(re.match(r"\d{2}/\d{2}/\d{2}\s+\d+\.\d+", line))

        if is_new_main_line:
            if expecting_description_continuation and current_entry:
                if current_entry.get("Total") and safe_float(current_entry["Total"]) > 0:
                    current_entry["Description"] = clean_description(current_entry["Description"])
                    results.append(current_entry.copy())
                else:
                    unmatched_booking_lines.append(" ".join(current_entry.values()))
                current_entry = {}
                expecting_description_continuation = False

        if is_new_main_line and ("Disposal Charge" in line or "Rebate" in line):
            parts = line.split()
            try:
                date = parts[0]
                ref_no = parts[1]
                tipping_match = re.search(r'(Disposal Charge|Rebate)[^\d]*(\d+\.\d+)\s+tonne', line)
                tipping = tipping_match.group(2) if tipping_match else ""
                qty = parts[-3]
                price = parts[-2]
                total = parts[-1]
                description = ' '.join(parts[2:-3])
            except:
                continue

            description = clean_description(description)
            disposal_entry = {
                "Tax Invoice": tax_invoice,
                "Services / Site": site_info.get("site_code", ""),
                "Customer": site_info.get("customer", ""),
                "Address": site_info.get("address", ""),
                "State": site_info.get("state", ""),
                "Date": date,
                "Ref No": ref_no,
                "Description": description,
                "Tipping": tipping,
                "Qty": qty,
                "Price": price,
                "Total": total,
                "Category": "Disposal"
            }
            results.append(disposal_entry)
            continue

        elif is_new_main_line:
            parts = line.split()
            try:
                date = parts[0]
                ref_no = parts[1]
                qty = parts[-3]
                price = parts[-2]
                total = parts[-1]
                description = ' '.join(parts[2:-3])
            except IndexError:
                continue

            description = clean_description(description)
            current_entry = {
                "Tax Invoice": tax_invoice,
                "Services / Site": site_info.get("site_code", ""),
                "Customer": site_info.get("customer", ""),
                "Address": site_info.get("address", ""),
                "State": site_info.get("state", ""),
                "Date": date,
                "Ref No": ref_no,
                "Description": description,
                "Tipping": "",
                "Qty": qty,
                "Price": price,
                "Total": total,
                "Category": "Booking"
            }
            expecting_description_continuation = True

        elif expecting_description_continuation:
            cleaned = line.strip()
            lower_cleaned = cleaned.lower()

            if (
                re.fullmatch(r"\d{5,}(\.\d+)?", cleaned) or
                re.fullmatch(r"[A-Z]{2,5}\d+(\.\d+)?", cleaned) or
                re.fullmatch(r"[A-Z]{2,5}\d+[\\/]\d+", cleaned) or
                re.fullmatch(r"\d{3}-\d{5}", cleaned) or
                lower_cleaned in {"no docket", "n/a", "na"} or
                re.fullmatch(r"^t\d{5}[\\/]\d$", cleaned.lower())
            ):
                continue

            elif lower_cleaned.startswith("sub total"):
                if current_entry.get("Total") and safe_float(current_entry["Total"]) > 0:
                    current_entry["Description"] = clean_description(current_entry["Description"])
                    results.append(current_entry.copy())
                else:
                    unmatched_booking_lines.append(" ".join(current_entry.values()))
                current_entry = {}
                expecting_description_continuation = False

            else:
                current_entry["Description"] += " " + cleaned

    if current_entry:
        if current_entry.get("Total") and safe_float(current_entry["Total"]) > 0:
            current_entry["Description"] = clean_description(current_entry["Description"])
            results.append(current_entry.copy())
        else:
            unmatched_booking_lines.append(" ".join(current_entry.values()))

    return results, unmatched_booking_lines

def parse_multiline_period_charges(lines, default_site_info, tax_invoice, invoice_date):
    results = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m1 = re.match(r"(\d+)\s+x\s+([\w\d]+).*?@\s*([\d.]+)\s*/\s*Lift", line, re.I)
        if m1 and i + 1 < len(lines):
            next_line = lines[i+1].strip()
            m2 = re.match(r"Site:\s*(\S+)\s+(.*)\s+(\d+)\s+([\d.]+)\s+([\d.]+)$", next_line)
            if m2:
                site_code = m2.group(1)
                description = m2.group(2).strip()
                qty2 = m2.group(3)
                price = m2.group(4)
                total = m2.group(5)
                entry = {
                    "Tax Invoice": tax_invoice,
                    "Services / Site": site_code or default_site_info.get("site_code", ""),
                    "Customer": default_site_info.get("customer", ""),
                    "Address": default_site_info.get("address", ""),
                    "State": default_site_info.get("state", ""),
                    "Date": invoice_date,
                    "Ref No": "",
                    "Description": f"{line} {description}",
                    "Tipping": "",
                    "PO": "",
                    "Qty": qty2,
                    "Price": price,
                    "Total": total,
                    "Category": "Period Charges"
                }
                results.append(entry)
                i += 2
                continue
        i += 1
    return results

def is_footer_line(line):
    line = line.strip().lower()
    return (
        "powered by wastedge" in line
        or re.match(r"^page[: ]*\d+", line)
        or re.search(r"tax invoice[: ]*\d+", line)
        or re.search(r"invoice date[: ]*\d{2}/\d{2}/\d{2}", line)
        or re.search(r"acc[: ]*\d+\.\d+", line)
        or ("tax invoice" in line and "invoice date" in line and "acc" in line)
    )

def process_invoice(pdf_io, master_site_names):
    all_data = []
    period_charges_data = []
    unmatched_lines = []
    unmatched_booking_lines = []

    with pdfplumber.open(pdf_io) as pdf:
        full_text = "\n".join(page.extract_text() for page in pdf.pages if page.extract_text())
        metadata = extract_invoice_metadata(full_text)
        tax_invoice = metadata["Tax Invoice"]
        invoice_date = metadata["Invoice Date"]

        invoice_total_excl_gst, all_invoice_totals = extract_invoice_totals_excl_gst(full_text)

        current_site_info = {}
        service_buffer = []
        period_charges_buffer = []
        parsing_services = False
        current_section = None

        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue
            raw_lines = text.split("\n")
            lines = [l for l in raw_lines if not is_footer_line(l)]

            i = 0
            while i < len(lines):
                line = lines[i].strip()
                # Detect new Tax Invoice (start of new invoice inside same PDF)
                if re.match(r"Tax Invoice\s*\d+", line, re.IGNORECASE):
                    # Flush buffers from previous invoice before resetting
                    if current_site_info and service_buffer:
                        bookings, unmatched_bookings = extract_service_lines(service_buffer, current_site_info, tax_invoice)
                        all_data.extend(bookings)
                        unmatched_booking_lines.extend(unmatched_bookings)
                        service_buffer = []

                    if period_charges_buffer:
                        multi_entries = parse_multiline_period_charges(period_charges_buffer, current_site_info, tax_invoice, invoice_date)
                        period_charges_data.extend(multi_entries)
                        period_charges_buffer = []

                    invoice_block_text = "\n".join(lines[i:i+20])  # limit search window
                    metadata = extract_invoice_metadata(invoice_block_text)
                    tax_invoice = metadata["Tax Invoice"]
                    invoice_date = metadata["Invoice Date"]
                    
                    current_site_info = {}
                    current_section = None
                    parsing_services = False
                    i += 1
                    continue

                if line.startswith("Services / Site:"):
                    if current_site_info and service_buffer:
                        bookings, unmatched_bookings = extract_service_lines(service_buffer, current_site_info, tax_invoice)
                        all_data.extend(bookings)
                        unmatched_booking_lines.extend(unmatched_bookings)
                        service_buffer = []

                    if period_charges_buffer:
                        multi_entries = parse_multiline_period_charges(period_charges_buffer, current_site_info, tax_invoice, invoice_date)
                        period_charges_data.extend(multi_entries)
                        period_charges_buffer = []

                    next_line = lines[i+1].strip() if i+1 < len(lines) else ""
                    if re.match(r"^\d{4}$", next_line):
                        i += 1

                    current_site_info = parse_site_line(line, master_site_names)
                    parsing_services = True
                    current_section = "services"
                    i += 1
                    continue

                if "Period Charges" in line:
                    if current_site_info and service_buffer:
                        bookings, unmatched_bookings = extract_service_lines(service_buffer, current_site_info, tax_invoice)
                        all_data.extend(bookings)
                        unmatched_booking_lines.extend(unmatched_bookings)
                        service_buffer = []

                    current_section = "period_charges"
                    parsing_services = False
                    period_charges_buffer = []
                    i += 1
                    continue

                if current_section == "period_charges":
                    if not line or line.lower().startswith(("services", "date ref no", "powered by", "page:")):
                        if period_charges_buffer:
                            multi_entries = parse_multiline_period_charges(period_charges_buffer, current_site_info, tax_invoice, invoice_date)
                            period_charges_data.extend(multi_entries)
                            period_charges_buffer = []
                        current_section = None
                        i += 1
                        continue
                    period_charges_buffer.append(line)
                    i += 1
                    continue

                if parsing_services:
                    if "Powered by" in line or line.lower().startswith("page:"):
                        i += 1
                        continue
                    if re.match(r"\d{2}/\d{2}/\d{2}\s+\d+\.\d+", line) or any(
                        kw in line.lower() for kw in ["bin", "exchange", "charge", "tonne", "waste", "frontlift"]
                    ):
                        service_buffer.append(line)
                    elif line.lower().startswith("services") or line.lower().startswith("date ref no"):
                        pass
                    elif current_site_info:
                        service_buffer.append(line)
                    else:
                        if line:
                            unmatched_lines.append(line)
                    i += 1
                    continue

                if line and not line.lower().startswith(("page:", "powered by")):
                    unmatched_lines.append(line)
                i += 1

        # Flush remaining buffers
        if current_site_info and service_buffer:
            bookings, unmatched_bookings = extract_service_lines(service_buffer, current_site_info, tax_invoice)
            all_data.extend(bookings)
            unmatched_booking_lines.extend(unmatched_bookings)
            service_buffer = []
        if period_charges_buffer:
            multi_entries = parse_multiline_period_charges(period_charges_buffer, current_site_info, tax_invoice, invoice_date)
            period_charges_data.extend(multi_entries)
            period_charges_buffer = []

    df_bookings = pd.DataFrame(all_data)
    df_period_charges = pd.DataFrame(period_charges_data)
    df_unmatched_bookings = pd.DataFrame({"Lines": unmatched_booking_lines})

    if "Pincode" in df_bookings.columns:
        df_bookings = df_bookings.drop(columns=["Pincode"])
    if "Pincode" in df_period_charges.columns:
        df_period_charges = df_period_charges.drop(columns=["Pincode"])

    df_bookings['Total_float'] = df_bookings['Total'].apply(safe_float) if not df_bookings.empty else pd.Series(dtype=float)
    df_period_charges['Total_float'] = df_period_charges['Total'].apply(safe_float) if not df_period_charges.empty else pd.Series(dtype=float)

    sum_bookings = df_bookings['Total_float'].sum() if not df_bookings.empty else 0
    sum_period_charges = df_period_charges['Total_float'].sum() if not df_period_charges.empty else 0
    sum_total_extracted = sum_bookings + sum_period_charges

    return {
        "metadata": metadata,
        "invoice_total_excl_gst": invoice_total_excl_gst,
        "all_invoice_totals": all_invoice_totals,
        "df_bookings": df_bookings,
        "df_period_charges": df_period_charges,
        "df_unmatched_bookings": df_unmatched_bookings,
        "sum_bookings": sum_bookings,
        "sum_period_charges": sum_period_charges,
        "sum_total_extracted": sum_total_extracted
    }


st.title("APS INVOICE DATA EXTRACTION")

pdf_file = st.file_uploader("Upload PDF Invoice", type=["pdf"])
csv_file = st.file_uploader("Upload Master Sites CSV", type=["csv"])

if st.button("Process"):
    if not pdf_file or not csv_file:
        st.warning("Please upload both a PDF and a CSV file.")
    else:
        try:
            pdf_bytes = pdf_file.read()
            pdf_io = io.BytesIO(pdf_bytes)

            csv_bytes = csv_file.read()
            csv_io = io.BytesIO(csv_bytes)
            master_sites_df = pd.read_csv(csv_io)
            if "standard_name" not in master_sites_df.columns:
                st.error("Master Sites CSV must contain a 'standard_name' column.")
            else:
                master_site_names = master_sites_df["standard_name"].dropna().tolist()

                with st.spinner("Processing invoice..."):
                    results = process_invoice(pdf_io, master_site_names)

                st.success("Processing complete!")

                st.markdown("### Invoice Metadata")
                for k, v in results["metadata"].items():
                    st.write(f"**{k}:** {v}")

                st.markdown("### Invoice Total (Excl GST):")
                st.write(results["invoice_total_excl_gst"])

                st.markdown("### Extracted Bookings ({} rows)".format(len(results["df_bookings"])))
                st.dataframe(results["df_bookings"])

                st.markdown("### Extracted Period Charges ({} rows)".format(len(results["df_period_charges"])))
                st.dataframe(results["df_period_charges"])

                st.markdown("### Unmatched Booking Lines ({} rows)".format(len(results["df_unmatched_bookings"])))
                if not results["df_unmatched_bookings"].empty:
                    st.dataframe(results["df_unmatched_bookings"])

                st.markdown("### Summary")
                st.write(f"Sum Bookings: {results['sum_bookings']}")
                st.write(f"Sum Period Charges: {results['sum_period_charges']}")
                st.write(f"Total Extracted: {results['sum_total_extracted']}")

                # Prepare Excel for download
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                    results["df_bookings"].to_excel(writer, sheet_name="Bookings", index=False)
                    results["df_period_charges"].to_excel(writer, sheet_name="Period Charges", index=False)
                    results["df_unmatched_bookings"].to_excel(writer, sheet_name="Unmatched Lines", index=False)
                output.seek(0)

                st.download_button(
                    label="Download Extracted Data as Excel",
                    data=output,
                    file_name="invoice_parsed_data.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

        except Exception as e:
            st.error(f"Error during processing: {e}")
