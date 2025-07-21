import io
import re
import pdfplumber
import pandas as pd
from rapidfuzz import process, fuzz
from IPython.display import display
# --- Widgets for file upload and process ---
pdf_upload = widgets.FileUpload(accept=".pdf", multiple=False)
csv_upload = widgets.FileUpload(accept=".csv", multiple=False)
process_button = widgets.Button(description="Process Uploaded Files")
output = widgets.Output()

display(widgets.Label("Upload PDF Invoice:"), pdf_upload)
display(widgets.Label("Upload Master Sites CSV:"), csv_upload)
display(process_button, output)

# --- Site name corrections cache ---
site_name_corrections = {}

invoice_total_sum = 0.0


def save_corrections():
    import csv
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
    # Remove label and strip extra spaces
    raw = line.replace("Services / Site:", "").strip()

    # Try to split into site code and the rest
    m = re.match(r"^(\S+)\s+(.*)$", raw)
    if not m:
        print(f"WARNING: Could not extract site_code from: {line}")
        return {}

    site_code = m.group(1)
    rest = m.group(2)

    # Try to split rest into customer and address at the first dash
    dash_split = rest.split(" - ", 1)
    if len(dash_split) == 2:
        customer_raw, address = dash_split[0].strip(), dash_split[1].strip()
    else:
        # Fallback: assume first 2 words = customer, rest = address
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


def expecting_likely_data(line):
    return bool(re.match(r"\d{2}/\d{2}/\d{2}\s+\d+\.\d+", line)) or any(
        keyword in line.lower() for keyword in ["bin", "exchange", "charge", "tonne", "waste", "frontlift"]
    )

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

def safe_float(val):
    try:
        return float(str(val).replace(',', '').strip())
    except:
        return 0.0


def clean_description(desc):
    patterns = [
        r"\b(EPD|TH|AGR|RYD)\w*[\\/]\d+\b",      # codes like AGR309666WW/1
        r"\b(EPD|TH|AGR|RYD)\w*\d+(\.\d+)?\b",   # codes like RYD177399486.0
        r"\b\d{5,}(\.\d+)?\b",                    # long numbers like 61944.0
        r"\b\d{3}-\d{5}\b",                       # PO-like numbers like 011-51414
        r"\bNO DOCKET\b",                         # "NO DOCKET" literal
        r"\bN/A\b",                              # N/A or NA
        r"\bNA\b",
        r"\bT\d{5}[\\/]\d\b",    # Specifically target patterns like T68871\1
        r"\bD\d{5}[\\/]\d\b"     # For D94787\1

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

            # Exclude unwanted trailing lines like docket refs, numbers, or codes
            if (
                re.fullmatch(r"\d{5,}(\.\d+)?", cleaned) or                           # Long numbers like 61944.0
                re.fullmatch(r"[A-Z]{2,5}\d+(\.\d+)?", cleaned) or                    # Alphanumeric codes like RYD177399486.0
                re.fullmatch(r"[A-Z]{2,5}\d+[\\/]\d+", cleaned) or                   # Ref codes like T68871\1, D94787/1
                re.fullmatch(r"\d{3}-\d{5}", cleaned) or                              # PO-like codes like 011-51414
                lower_cleaned in {"no docket", "n/a", "na"} or                       # Known bad text
                re.fullmatch(r"^t\d{5}[\\/]\d$", cleaned.lower())                    # Specific format like T68871\1
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
                    # "Pincode": default_site_info.get("pincode", ""),  # Removed
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

def is_invalid_summary_line(line):
    keywords = ["total", "gst", "receipt", "balance", "amount due", "summary", "current period"]
    line_lower = line.lower()
    return any(keyword in line_lower for keyword in keywords)

# --- Main processing function ---
def process_uploaded_files():
    with output:
        output.clear_output()
        if not pdf_upload.value or not csv_upload.value:
            print("Please upload both PDF and CSV files to proceed.")
            return
        try:
            print("Reading uploaded files...")

            # Get PDF bytes and open with pdfplumber
            pdf_bytes = pdf_upload.value[0]['content']
            pdf_file = io.BytesIO(pdf_bytes)

            # Get CSV bytes and read with pandas
            csv_bytes = csv_upload.value[0]['content']
            csv_file = io.BytesIO(csv_bytes)
            master_sites_df = pd.read_csv(csv_file)
            master_site_names = master_sites_df["standard_name"].tolist()

            print(f"Loaded master sites: {len(master_site_names)} entries")

            all_data = []
            period_charges_data = []
            unmatched_lines = []
            unmatched_booking_lines = []

            with pdfplumber.open(pdf_file) as pdf:
                full_text = "\n".join(page.extract_text() for page in pdf.pages if page.extract_text())
                metadata = extract_invoice_metadata(full_text)
                tax_invoice = metadata["Tax Invoice"]
                invoice_date = metadata["Invoice Date"]

                invoice_total_excl_gst, all_invoice_totals = extract_invoice_totals_excl_gst(full_text)
                print(f"Individual Invoice Totals Found: {all_invoice_totals}")

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

                            # Extract new invoice metadata from current and remaining lines
                            invoice_block_text = "\n".join(lines[i:i+20])  # limit search window
                            metadata = extract_invoice_metadata(invoice_block_text)
                            tax_invoice = metadata["Tax Invoice"]
                            invoice_date = metadata["Invoice Date"]
                            
                            # Reset state for the new invoice
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

                            # Fix split site line (postcode on next line)
                            next_line = lines[i+1].strip() if i+1 < len(lines) else ""
                            if re.match(r"^\d{4}$", next_line):  # likely postcode, skip now
                                # Don't append postcode to line anymore; just skip it
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
                            if re.match(r"\d{2}/\d{2}/\d{2}\s+\d+\.\d+", line) or expecting_likely_data(line):
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
                        # Detect and accumulate each 'Total (Excl.GST):' value
                        


                        # Outside recognized sections
                        if line and not line.lower().startswith(("page:", "powered by")):
                            unmatched_lines.append(line)
                        i += 1

                # Flush remaining service buffer
                if current_site_info and service_buffer:
                    bookings, unmatched_bookings = extract_service_lines(service_buffer, current_site_info, tax_invoice)
                    all_data.extend(bookings)
                    unmatched_booking_lines.extend(unmatched_bookings)
                    service_buffer = []
                
                # Flush remaining period charges buffer
                if period_charges_buffer:
                    multi_entries = parse_multiline_period_charges(period_charges_buffer, current_site_info, tax_invoice, invoice_date)
                    period_charges_data.extend(multi_entries)
                    period_charges_buffer = []

            df_bookings = pd.DataFrame(all_data)
            df_period_charges = pd.DataFrame(period_charges_data)
            df_unmatched_bookings = pd.DataFrame({"Lines": unmatched_booking_lines})

            # Drop Pincode column if present
            if "Pincode" in df_bookings.columns:
                df_bookings = df_bookings.drop(columns=["Pincode"])
            if "Pincode" in df_period_charges.columns:
                df_period_charges = df_period_charges.drop(columns=["Pincode"])


            df_bookings['Total_float'] = df_bookings['Total'].apply(safe_float) if not df_bookings.empty else pd.Series(dtype=float)
            df_period_charges['Total_float'] = df_period_charges['Total'].apply(safe_float) if not df_period_charges.empty else pd.Series(dtype=float)

            sum_bookings = df_bookings['Total_float'].sum() if not df_bookings.empty else 0
            sum_period_charges = df_period_charges['Total_float'].sum() if not df_period_charges.empty else 0
            sum_total_extracted = sum_bookings + sum_period_charges

            print(f"Invoice Total (Excl.GST) from invoice: {invoice_total_excl_gst if invoice_total_excl_gst is not None else 'Not found'}")
            print(f"Sum of extracted Booking line totals: {sum_bookings:,.2f}")
            print(f"Sum of extracted Period Charges line totals: {sum_period_charges:,.2f}")
            print(f"Sum of all extracted line totals: {sum_total_extracted:,.2f}")

            if invoice_total_excl_gst is not None:
                discrepancy = invoice_total_excl_gst - sum_total_extracted
                if abs(discrepancy) < 0.01:
                    print("✅ Invoice total matches sum of extracted line totals!")
                else:
                    print(f"⚠️ Discrepancy detected: {discrepancy:,.2f}")
            else:
                print("⚠️ Could not find 'Total (Excl.GST)' amount in invoice text.")

            if df_unmatched_bookings.empty:
                df_unmatched_bookings = pd.DataFrame({"Note": ["No unmatched booking lines found"]})

            output_file = f"APS_{tax_invoice}_parsed.xlsx"
            with pd.ExcelWriter(output_file) as writer:
                if not df_bookings.empty:
                    df_bookings.to_excel(writer, sheet_name="Bookings", index=False)
                if not df_period_charges.empty:
                    df_period_charges.to_excel(writer, sheet_name="Period Charges", index=False)
                df_unmatched_bookings.to_excel(writer, sheet_name="Unmatched Bookings", index=False)

            print(f"✅ Extracted data saved to: {output_file}")

        except Exception as e:
            print(f"Error during processing: {e}")

# --- Button click handler ---
def on_process_button_clicked(b):
    with output:
        output.clear_output()
        print("Processing started...")
    try:
        process_uploaded_files()
    except Exception as e:
        with output:
            print(f"Error during processing: {e}")

process_button.on_click(on_process_button_clicked)
