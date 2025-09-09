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
# Tokenizer (used in learning widget)
# -----------------------------
def tokenize_line(line):
    tokens = line.split()
    return " ".join([
        "<DATE>" if re.match(r"\d{2}\.\d{2}\.\d{4}", t)
        else "<NUMBER>" if re.match(r"^\d[\d,\.]*$", t)
        else "<AUD>" if t.upper() == "AUD"
        else "<TEXT>"
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

                # -----------------------------
                # Customer Line
                # -----------------------------
                cust_match = re.match(r"^(R-[A-Z0-9]+)\s+(.+)", line)
                if cust_match:
                    customer = cust_match.group(1) + " " + cust_match.group(2).strip()
                    matched = True
                    continue

                # -----------------------------
                # Rental Pattern
                # -----------------------------
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

                # -----------------------------
                # FFS - Qty/Weight (Standard)
                # -----------------------------
                ffs_match = re.match(
                    r"(\d{2}\.\d{2}\.\d{4})\s+(.+?)\s+FFS - Qty/Weight\s+([\w\d/]+)\s+([\d\.]+)\s+(\w+)\s+([\d\.]+)\s+([\w\d\.]+)\s+([\d,\.]+)\s+([\d,\.]+)\s+([\d,\.]+) AUD",
                    line
                )
                if ffs_match:
                    date, description, reference, qty, qty_unit, unit_price, unit_unit, amt_excl_gst, gst, amt_incl_gst = ffs_match.groups()
                    billed_qty_line = lines[i + 1] if i + 1 < len(lines) else ""
                    billed_qty_match = re.search(r"Billed Qty\s+([\d\.]+)\s+(\w+)", billed_qty_line)
                    billed_qty_full = f"{billed_qty_match.group(1)} {billed_qty_match.group(2)}" if billed_qty_match else ""
                    data.append({
                        "Invoice No.": invoice_no, "Customer": customer, "Date": date, "Description": description,
                        "Charge Type/Period Reference": "FFS - Qty/Weight", "Reference": reference,
                        "Billed qty": billed_qty_full, "Qty.": f"{qty} {qty_unit}", "Unit Price": f"{unit_price} {unit_unit}",
                        "Amount excl. GST": amt_excl_gst, "GST": gst, "Amount Incl. GST": amt_incl_gst
                    })
                    matched = True
                    continue

                # -----------------------------
                # FFS - Qty/Weight with TO
                # -----------------------------
                ffs_qty_to_match = re.match(
                    r"(\d{2}\.\d{2}\.\d{4})\s+(.+?)\s+FFS - Qty/Weight\s+([\w\-\/]+)\s+([\d\.]+)\s+TO\s+([\d\.]+)\s+(\w+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+) AUD",
                    line
                )
                if ffs_qty_to_match:
                    date, description, reference, val1, val2, qty_unit, billed_qty, unit_price, amt_incl_gst = ffs_qty_to_match.groups()
                    gst = str(round(float(amt_incl_gst) - float(unit_price), 2))
                    amt_excl_gst = unit_price
                    billed_qty_full = f"{billed_qty} {qty_unit}"
                    data.append({
                        "Invoice No.": invoice_no, "Customer": customer, "Date": date,
                        "Description": description, "Charge Type/Period Reference": "FFS - Qty/Weight",
                        "Reference": reference, "Billed qty": billed_qty_full, "Qty.": f"{val1} TO {val2} {qty_unit}",
                        "Unit Price": unit_price, "Amount excl. GST": amt_excl_gst,
                        "GST": gst, "Amount Incl. GST": amt_incl_gst
                    })
                    matched = True
                    continue

                # -----------------------------
                # FFS - Load Compact
                # -----------------------------
                ffs_load_compact_match = re.match(
                    r"(\d{2}\.\d{2}\.\d{4})\s+(.+?)\s+FFS - Load\s+([\w\-\.]+)\s+([\d\.]+)\s+(\w+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+) AUD",
                    line
                )
                if ffs_load_compact_match:
                    date, description, reference, qty, qty_unit, unit_price, gst, amt_incl_gst = ffs_load_compact_match.groups()
                    amt_excl_gst = str(round(float(amt_incl_gst) - float(gst), 2))
                    billed_qty_line = lines[i + 1] if i + 1 < len(lines) else ""
                    billed_qty_match = re.search(r"Billed Qty\s+([\d\.]+)\s+(\w+)", billed_qty_line)
                    billed_qty_full = f"{billed_qty_match.group(1)} {billed_qty_match.group(2)}" if billed_qty_match else ""
                    data.append({
                        "Invoice No.": invoice_no, "Customer": customer, "Date": date,
                        "Description": description.strip(), "Charge Type/Period Reference": "FFS - Load",
                        "Reference": reference.strip(), "Billed qty": billed_qty_full,
                        "Qty.": f"{qty} {qty_unit}", "Unit Price": f"{unit_price}",
                        "Amount excl. GST": amt_excl_gst, "GST": gst, "Amount Incl. GST": amt_incl_gst
                    })
                    matched = True
                    continue

                # -----------------------------
                # Standard Front Lift
                # -----------------------------
                front_lift_match = re.match(
                    r"(\d{2}\.\d{2}\.\d{4})\s+(.+?)\s+(\d{2}\.\d{2}\.\d{4} to \d{2}\.\d{2}\.\d{4})\s+(.+?)\s+([\d\.]+)\s+(\w+)\s+([\d\.]+)\s+(\w+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+) AUD",
                    line
                )
                if front_lift_match:
                    date, description, period, ref_details, qty_val, qty_unit, unit_price_val, unit_price_unit, amt_excl_gst, gst, amt_incl_gst = front_lift_match.groups()
                    billed_qty_full = f"{qty_val} {qty_unit}"
                    data.append({
                        "Invoice No.": invoice_no, "Customer": customer, "Date": date,
                        "Description": description.strip(), "Charge Type/Period Reference": period,
                        "Reference": ref_details.strip(), "Billed qty": billed_qty_full,
                        "Qty.": billed_qty_full, "Unit Price": f"{unit_price_val} {unit_price_unit}",
                        "Amount excl. GST": amt_excl_gst, "GST": gst, "Amount Incl. GST": amt_incl_gst
                    })
                    matched = True
                    continue

                # -----------------------------
                # Manual Price
                # -----------------------------
                if "Manual Price" in line:
                    try:
                        date_match = re.match(r"(\d{2}\.\d{2}\.\d{4})\s+(.+?)\s+Manual Price\s+(.+)", line)
                        if not date_match:
                            continue
                        date, desc1, desc2 = date_match.groups()
                        description_lines = [f"{desc1.strip()} {desc2.strip()}"]

                        lookahead = 1
                        while i + lookahead < len(lines) and lookahead <= 5:
                            next_line = lines[i + lookahead].strip()
                            if next_line == "":
                                break
                            if (re.search(r"\d+\.?\d*\s+(TO\s+)?\d+\.?\d*", next_line) or "AUD" in next_line or "Billed Qty" in next_line):
                                description_lines.append(next_line)
                            else:
                                break
                            lookahead += 1

                        full_block = " ".join(description_lines)

                        totals_match = re.search(r"([\d,\.]+)\s+([\d,\.]+)\s+([\d,\.]+)\s+AUD", full_block)
                        amt_excl_gst, gst, amt_incl_gst = totals_match.groups() if totals_match else ("", "", "")

                        qty_match = re.search(r"(\d+\.?\d*)\s+TO\s+([\d,\.]+)", full_block)
                        qty = f"{qty_match.group(1)} TO" if qty_match else ""
                        unit_price = qty_match.group(2) if qty_match else ""

                        billed_qty_match = re.search(r"Billed Qty\s+([\d\.]+)\s+TO", full_block)
                        billed_qty = f"{billed_qty_match.group(1)} TO" if billed_qty_match else ""

                        data.append({
                            "Invoice No.": invoice_no, "Customer": customer, "Date": date,
                            "Description": "Manual Price - " + description_lines[0],
                            "Charge Type/Period Reference": "Manual Price",
                            "Reference": "", "Billed qty": billed_qty or qty,
                            "Qty.": qty, "Unit Price": unit_price,
                            "Amount excl. GST": amt_excl_gst, "GST": gst, "Amount Incl. GST": amt_incl_gst
                        })
                        matched = True
                        continue
                    except Exception:
                        continue

                # -----------------------------
                # Learned Patterns & Plastic Roll fallback
                # -----------------------------
                if not matched and re.search(r"\d{2}\.\d{2}\.\d{4}.*AUD", line):
                    # Apply learned patterns
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

                    # Fallback - Plastic Roll
                    if not matched:
                        ffs_plastic_roll_match = re.match(
                            r"(\d{2}\.\d{2}\.\d{4})\s+(.+?)\s+FFS - Qty/Weight\s+([A-Z0-9]+)\s+([\d\.]+)\s+(\w+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+AUD",
                            line
                        )
                        if ffs_plastic_roll_match:
                            date, description, reference, qty, qty_unit, unit_price, amt_excl_gst, gst, amt_incl_gst = ffs_plastic_roll_match.groups()
                            billed_qty_full = f"{qty} {qty_unit}"
                            data.append({
                                "Invoice No.": invoice_no, "Customer": customer, "Date": date,
                                "Description": description.strip(), "Charge Type/Period Reference": "FFS - Qty/Weight",
                                "Reference": reference.strip(), "Billed qty": billed_qty_full,
                                "Qty.": billed_qty_full, "Unit Price": unit_price,
                                "Amount excl. GST": amt_excl_gst, "GST": gst, "Amount Incl. GST": amt_incl_gst
                            })
                            matched = True
                            continue

                    if not matched:
                        missed_lines.append({
                            "Page": page_num, "Line No.": i + 1, "Customer": customer, "Line": line,
                            "Note": "Potential invoice data (unparsed)"
                        })

    # -----------------------------
    # Invoice Totals
    # -----------------------------
    total_payable_matches = re.findall(
        r"Total Payable\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+AUD",
        full_text, re.IGNORECASE
    )

    totals = {}
    if total_payable_matches:
        excl_total = gst_total = incl_total = 0.0
        for excl_str, gst_str, incl_str in total_payable_matches:
            excl_total += float(excl_str.replace(",", ""))
            gst_total += float(gst_str.replace(",", ""))
            incl_total += float(incl_str.replace(",", ""))
        totals = {
            "Amount excl. GST": round(excl_total, 2),
            "GST": round(gst_total, 2),
            "Amount Incl. GST": round(incl_total, 2)
        }

    return invoice_no, data, missed_lines, totals

# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="Invoice PDF → Excel", layout="wide")
st.title("📄 OPAL Invoice PDF → Excel Extractor")

uploaded_file = st.file_uploader("Upload an Invoice PDF", type=["pdf"])

if uploaded_file:
    with st.spinner("Processing PDF... please wait ⏳"):
        file_stream = io.BytesIO(uploaded_file.read())
        invoice_no, data, missed_lines, totals = process_pdf(file_stream)

    st.success(f"✅ Extracted {len(data)} lines | ⚠️ {len(missed_lines)} unmatched")

    if data:
        st.subheader("Extracted Data (preview)")
        st.dataframe(pd.DataFrame(data).head(20))

    if missed_lines:
        st.subheader("Unmatched Lines (first 10)")
        for row in missed_lines[:10]:
            st.text(f"[Page {row['Page']} | Line {row['Line No.']}] {row['Line']}")

    if totals:
        st.subheader("Invoice Totals")
        st.json(totals)

    # --- Save to Excel ---
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
