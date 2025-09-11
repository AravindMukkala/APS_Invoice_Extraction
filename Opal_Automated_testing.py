import pdfplumber
import pandas as pd
import re
import io
import json
import streamlit as st

# -----------------------------
# Load / Save learned patterns
# -----------------------------
def load_learned_patterns():
    try:
        with open("learned_patterns.json", "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_learned_patterns(patterns):
    with open("learned_patterns.json", "w") as f:
        json.dump(patterns, f, indent=2)

learned_patterns = load_learned_patterns()

# -----------------------------
# Tokenizer
# -----------------------------
def tokenize_line(line: str) -> str:
    tokens = line.split()
    return " ".join([
        "<DATE>" if re.match(r"\d{2}\.\d{2}\.\d{4}", t)
        else "<NUM>" if re.match(r"^[\d,\.]+$", t)
        else "<AUD>" if t.upper() == "AUD"
        else "<TXT>"
        for t in tokens
    ])

# -----------------------------
# Main PDF Processing (Updated)
# -----------------------------
def process_pdf(file_stream):
    invoice_no = ""
    data = []
    missed_lines = []
    customer = ""
    full_text = ""

    with pdfplumber.open(file_stream) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            full_text += text + "\n"

            # Extract invoice number
            if not invoice_no and "Invoice No." in text:
                match = re.search(r"Invoice No\. (\d+)", text)
                if match:
                    invoice_no = match.group(1)

            lines = text.split("\n")

            for i, line in enumerate(lines):
                matched = False
                line = line.strip()
                if not line:
                    continue

                # ---------------- Customer ----------------
                cust_match = re.match(r"^(R-[A-Z0-9]+)\s+(.+)", line)
                if cust_match:
                    customer = f"{cust_match.group(1)} {cust_match.group(2).strip()}"
                    matched = True
                    continue

                # ---------------- Rental ----------------
                rental_pattern = (
                    r"(\d{2}\.\d{2}\.\d{4})\s+(.+?)\s+"
                    r"(\d{2}\.\d{2}\.\d{4} to \d{2}\.\d{2}\.\d{4})\s+"
                    r"([\d\.]+)\s+(\w+)\s+([\d\.]+)\s+(\w+)\s+([\d,\.]+)\s+([\d,\.]+)\s+([\d,\.]+)\s+AUD"
                )
                rental_match = re.match(rental_pattern, line)
                if rental_match:
                    date, description, period, qty, qty_unit, unit_price, unit_unit, amt_excl_gst, gst, amt_incl_gst = rental_match.groups()
                    billed_qty_line = lines[i + 1] if i + 1 < len(lines) else ""
                    billed_qty_match = re.search(r"Billed Qty\s+([\d\.]+)\s+(\w+)", billed_qty_line)
                    billed_qty_full = f"{billed_qty_match.group(1)} {billed_qty_match.group(2)}" if billed_qty_match else ""
                    data.append({
                        "Invoice No.": invoice_no, "Customer": customer, "Date": date,
                        "Description": description, "Charge Type/Period Reference": period,
                        "Reference": "", "Billed qty": billed_qty_full,
                        "Qty.": f"{qty} {qty_unit}", "Unit Price": f"{unit_price} {unit_unit}",
                        "Amount excl. GST": amt_excl_gst, "GST": gst, "Amount Incl. GST": amt_incl_gst
                    })
                    matched = True
                    continue

                # ---------------- FFS - Qty/Weight ----------------
                ffs_pattern = (
                    r"(\d{2}\.\d{2}\.\d{4})\s+(.+?)\s+FFS - Qty/Weight\s+([\w\d\-/]+)\s+"
                    r"([\d\.]+)\s+(\w+)\s+([\d\.]+)\s+([\w\d\.]+)\s+([\d,\.]+)\s+([\d,\.]+)\s+([\d,\.]+) AUD"
                )
                ffs_match = re.match(ffs_pattern, line)
                if ffs_match:
                    date, description, reference, qty, qty_unit, unit_price, unit_unit, amt_excl_gst, gst, amt_incl_gst = ffs_match.groups()
                    billed_qty_line = lines[i + 1] if i + 1 < len(lines) else ""
                    billed_qty_match = re.search(r"Billed Qty\s+([\d\.]+)\s+(\w+)", billed_qty_line)
                    billed_qty_full = f"{billed_qty_match.group(1)} {billed_qty_match.group(2)}" if billed_qty_match else ""
                    data.append({
                        "Invoice No.": invoice_no, "Customer": customer, "Date": date,
                        "Description": description, "Charge Type/Period Reference": "FFS - Qty/Weight",
                        "Reference": reference, "Billed qty": billed_qty_full,
                        "Qty.": f"{qty} {qty_unit}", "Unit Price": f"{unit_price} {unit_unit}",
                        "Amount excl. GST": amt_excl_gst, "GST": gst, "Amount Incl. GST": amt_incl_gst
                    })
                    matched = True
                    continue

                # ---------------- Front Lift / Rental style ----------------
                front_lift_pattern = (
                    r"(\d{2}\.\d{2}\.\d{4})\s+(.+?)\s+"
                    r"(\d{2}\.\d{2}\.\d{4} to \d{2}\.\d{2}\.\d{4})\s+(.+?)\s+"
                    r"([\d\.]+)\s+(\w+)\s+([\d\.]+)\s+(\w+)\s+([\d,\.]+)\s+([\d,\.]+)\s+([\d,\.]+) AUD"
                )
                front_lift_match = re.match(front_lift_pattern, line)
                if front_lift_match:
                    date, description, period, ref_details, qty_val, qty_unit, unit_price_val, unit_price_unit, amt_excl_gst, gst, amt_incl_gst = front_lift_match.groups()
                    billed_qty_full = f"{qty_val} {qty_unit}"
                    data.append({
                        "Invoice No.": invoice_no, "Customer": customer, "Date": date,
                        "Description": description, "Charge Type/Period Reference": period,
                        "Reference": ref_details, "Billed qty": billed_qty_full,
                        "Qty.": billed_qty_full, "Unit Price": f"{unit_price_val} {unit_price_unit}",
                        "Amount excl. GST": amt_excl_gst, "GST": gst, "Amount Incl. GST": amt_incl_gst
                    })
                    matched = True
                    continue

                # ---------------- Manual Price ----------------
                if "Manual Price" in line:
                    try:
                        date_match = re.match(r"(\d{2}\.\d{2}\.\d{4})\s+(.+?)\s+Manual Price\s+(.+)", line)
                        if not date_match:
                            continue
                        date, desc1, desc2 = date_match.groups()
                        description_lines = [f"{desc1} {desc2}".strip()]

                        # Capture up to 5 additional lines if relevant
                        for lookahead in range(1, 6):
                            if i + lookahead >= len(lines):
                                break
                            next_line = lines[i + lookahead].strip()
                            if not next_line:
                                break
                            description_lines.append(next_line)
                        full_block = " ".join(description_lines)

                        # Extract amounts
                        totals_match = re.search(r"([\d,\.]+)\s+([\d,\.]+)\s+([\d,\.]+)\s+AUD", full_block)
                        amt_excl_gst, gst, amt_incl_gst = totals_match.groups() if totals_match else ("", "", "")

                        # Extract qty/unit price if TO exists
                        qty_match = re.search(r"([\d\.]+)\s+TO\s+([\d\.]+)", full_block)
                        qty = f"{qty_match.group(1)} TO {qty_match.group(2)}" if qty_match else ""
                        unit_price = ""  # optional

                        billed_qty_match = re.search(r"Billed Qty\s+([\d\.]+)", full_block)
                        billed_qty = billed_qty_match.group(1) if billed_qty_match else ""

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

                # ---------------- Learned Patterns ----------------
                if not matched:
                    current_token_pattern = tokenize_line(line)
                    for token_pattern, pattern_data in learned_patterns.items():
                        if current_token_pattern != token_pattern:
                            continue
                        regex = pattern_data["regex"]
                        field_map = pattern_data["field_map"]
                        charge_type = pattern_data.get("Charge Type", "")
                        match = re.match(regex, line)
                        if match:
                            groups = match.groups()
                            parsed = {
                                "Invoice No.": invoice_no,
                                "Customer": customer,
                                "Charge Type/Period Reference": charge_type,
                            }
                            # Safe group mapping
                            for field, gi in field_map.items():
                                parsed[field] = groups[gi - 1] if 0 < gi <= len(groups) else ""
                            data.append(parsed)
                            matched = True
                            break

                # ---------------- Fallback / Unmatched ----------------
                if not matched and re.search(r"\d{2}\.\d{2}\.\d{4}.*AUD", line):
                    missed_lines.append({
                        "Page": page_num, "Line No.": i + 1, "Customer": customer,
                        "Line": line, "Note": "Potential invoice data (unparsed)"
                    })

    return invoice_no, data, missed_lines, full_text

# -----------------------------
# Learning Widget
# -----------------------------
def show_learning_widget(missed_lines):
    st.subheader("🧠 Teach Me (Learning Widget)")
    if not missed_lines:
        st.info("✅ No unmatched lines found.")
        return

    field_options = {
        "Ignore": "❌ Ignore this token",
        "Date": "📅 Invoice Date",
        "Description": "📝 Description",
        "Reference": "🔖 Reference",
        "Qty": "🔢 Quantity value",
        "Qty Unit": "📦 Unit",
        "Billed qty": "📊 Billed quantity",
        "Unit Price": "💲 Unit Price",
        "Amount excl. GST": "💰 Amount excl. GST",
        "GST": "🧾 GST",
        "Amount Incl. GST": "💲 Amount Incl. GST",
        "Charge Type/Period Reference": "📅 Charge period",
        "AUD": "💲 Currency AUD"
    }

    for idx, ml in enumerate(missed_lines[:10]):
        st.markdown(f"**Page {ml['Page']} | Line {ml['Line No.']}**")
        line = ml["Line"]
        tokens = line.split()
        dropdowns = []
        cols = st.columns(len(tokens))
        for i, token in enumerate(tokens):
            with cols[i]:
                choice = st.selectbox(f"{token}", list(field_options.keys()), format_func=lambda x: field_options[x], key=f"dd_{idx}_{i}")
                dropdowns.append(choice)

        if st.button(f"💾 Save Mapping for Line {idx+1}", key=f"save_{idx}"):
            regex_parts, field_map = [], {}
            for i, label in enumerate(dropdowns):
                token = tokens[i]
                if label == "Ignore":
                    regex_parts.append(re.escape(token))
                elif label == "Date":
                    regex_parts.append(r"(\d{2}\.\d{2}\.\d{4})"); field_map["Date"] = len(regex_parts)
                elif label == "Qty":
                    regex_parts.append(r"([\d\.]+)"); field_map["Qty."] = len(regex_parts)
                elif label == "Qty Unit":
                    regex_parts.append(r"(\w+)"); field_map["Billed qty"] = len(regex_parts)
                elif label == "Unit Price":
                    regex_parts.append(r"([\d\.]+)"); field_map["Unit Price"] = len(regex_parts)
                elif label == "Amount excl. GST":
                    regex_parts.append(r"([\d,\.]+)"); field_map["Amount excl. GST"] = len(regex_parts)
                elif label == "GST":
                    regex_parts.append(r"([\d,\.]+)"); field_map["GST"] = len(regex_parts)
                elif label == "Amount Incl. GST":
                    regex_parts.append(r"([\d,\.]+)"); field_map["Amount Incl. GST"] = len(regex_parts)
                elif label == "Reference":
                    regex_parts.append(r"([\w\-\/\.]+)"); field_map["Reference"] = len(regex_parts)
                elif label == "Description":
                    regex_parts.append(r"(.+?)"); field_map["Description"] = len(regex_parts)
                elif label == "Charge Type/Period Reference":
                    regex_parts.append(r"(.+?)"); field_map["Charge Type/Period Reference"] = len(regex_parts)
                elif label == "AUD":
                    regex_parts.append(r"AUD")

            final_regex = r"\s+".join(regex_parts)
            token_pattern = tokenize_line(line)

            try:
                compiled = re.compile(final_regex)
                m = compiled.match(line)
                if not m:
                    st.warning("⚠️ Pattern didn’t match this line. Not saved.")
                else:
                    groups = m.groups()
                    if all(0 < gi <= len(groups) for gi in field_map.values()):
                        learned_patterns[token_pattern] = {
                            "regex": final_regex,
                            "field_map": field_map,
                            "Charge Type": "Auto-Learned"
                        }
                        save_learned_patterns(learned_patterns)
                        st.success("✅ Pattern saved successfully!")
                    else:
                        st.warning("⚠️ Invalid group mapping.")
            except re.error as e:
                st.error(f"❌ Regex error: {e}")

# -----------------------------
# Pattern Management
# -----------------------------
def manage_patterns():
    st.subheader("📚 Manage Learned Patterns")
    if not learned_patterns:
        st.info("No patterns saved yet.")
        return

    field_options = [
        "(ignore)", "Date", "Description", "Reference", "Charge Type/Period Reference",
        "Billed qty", "Qty.", "Unit Price", "Amount excl. GST", "GST", "Amount Incl. GST"
    ]

    for token_pattern, pattern_data in list(learned_patterns.items()):
        with st.expander(f"🔑 Token Pattern: {token_pattern}"):
            new_regex = st.text_area(f"Edit Regex", value=pattern_data.get("regex", ""), height=80, key=f"regex_{token_pattern}")
            new_charge_type = st.text_input("Charge Type", value=pattern_data.get("Charge Type", "Custom"), key=f"charge_{token_pattern}")
            field_map = pattern_data.get("field_map", {})
            new_field_map = {}
            max_groups = max(field_map.values()) if field_map else 5

            for group_index in range(1, max_groups + 1):
                current_field = next((k for k, v in field_map.items() if v == group_index), None)
                selection = st.selectbox(f"Group {group_index}", field_options, index=field_options.index(current_field) if current_field in field_options else 0, key=f"{token_pattern}_group_{group_index}")
                if selection != "(ignore)":
                    new_field_map[selection] = group_index

            test_line = st.text_input("Test line", "", key=f"testline_{token_pattern}")
            if st.button(f"▶️ Run Test ({token_pattern})"):
                try:
                    match = re.match(new_regex, test_line)
                    if match:
                        results = {field: match.group(idx) for field, idx in new_field_map.items() if idx <= len(match.groups())}
                        st.success("✅ Match Found!"); st.json(results)
                    else:
                        st.error("❌ No match.")
                except re.error as e:
                    st.error(f"⚠️ Invalid regex: {e}")

            col1, col2 = st.columns(2)
            with col1:
                if st.button(f"💾 Save ({token_pattern})"):
                    learned_patterns[token_pattern]["regex"] = new_regex
                    learned_patterns[token_pattern]["Charge Type"] = new_charge_type
                    learned_patterns[token_pattern]["field_map"] = new_field_map
                    save_learned_patterns(learned_patterns)
                    st.success("✅ Pattern updated!")
            with col2:
                if st.button(f"🗑️ Delete ({token_pattern})"):
                    del learned_patterns[token_pattern]
                    save_learned_patterns(learned_patterns)
                    st.warning("❌ Pattern deleted!")
                    st.experimental_rerun()

# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="Invoice PDF → Excel", layout="wide")
st.title("📄 Invoice PDF → Excel Extractor")

uploaded_file = st.file_uploader("Upload an Invoice PDF", type=["pdf"])
if uploaded_file:
    with st.spinner("Processing PDF..."):
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
        show_learning_widget(missed_lines)

    manage_patterns()

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
        file_name=f"Invoice_{invoice_no or 'Unknown'}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
