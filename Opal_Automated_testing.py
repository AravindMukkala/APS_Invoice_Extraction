import pdfplumber
import pandas as pd
import re
import io
import json
import streamlit as st

# -----------------------------
# Load learned patterns
# -----------------------------
def load_learned_patterns():
    try:
        with open("learned_patterns.json", "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_learned_patterns(patterns):
    with open("learned_patterns.json", "w") as f:
        json.dump(patterns, f, indent=2)

learned_patterns = load_learned_patterns()

# -----------------------------
# Tokenizer
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

            # Extract invoice number
            if not invoice_no and text and "Invoice No." in text:
                match = re.search(r"Invoice No\. (\d+)", text)
                if match:
                    invoice_no = match.group(1)

            if not text:
                continue

            lines = text.split("\n")

            for i, line in enumerate(lines):
                matched = False

                # ---------------- Customer ----------------
                cust_match = re.match(r"^(R-[A-Z0-9]+)\s+(.+)", line)
                if cust_match:
                    customer = cust_match.group(1) + " " + cust_match.group(2).strip()
                    matched = True
                    continue

                # ---------------- Rental ----------------
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

                # ---------------- FFS - Qty/Weight ----------------
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

                # ---------------- FFS with TO ----------------
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

                # ---------------- FFS - Load Compact ----------------
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

                # ---------------- Front Lift / Rental style ----------------
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

                # ---------------- Manual Price ----------------
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

                # ---------------- Learned Patterns ----------------
                if not matched:
                    for token_pattern, pattern_data in learned_patterns.items():
                        regex = pattern_data["regex"]
                        field_map = pattern_data["field_map"]
                        charge_type = pattern_data.get("Charge Type", "")
                        current_token_pattern = tokenize_line(line)

                        if current_token_pattern == token_pattern:
                            match = re.match(regex, line)
                            if match:
                                groups = match.groups()
                                parsed = {
                                    "Invoice No.": invoice_no,
                                    "Customer": customer,
                                    "Charge Type/Period Reference": charge_type,
                                }

                                # ✅ Safe group lookup
                                for field, group_index in field_map.items():
                                    if 0 < group_index <= len(groups):
                                        parsed[field] = groups[group_index - 1]
                                    else:
                                        parsed[field] = ""  # fallback empty if invalid mapping

                                data.append(parsed)
                                matched = True
                                break

                # ---------------- Fallback Plastic Rolls ----------------
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

                # ---------------- Unmatched ----------------
                if not matched and re.search(r"\d{2}\.\d{2}\.\d{4}.*AUD", line):
                    missed_lines.append({
                        "Page": page_num, "Line No.": i + 1, "Customer": customer,
                        "Line": line, "Note": "Potential invoice data (unparsed)"
                    })

    # ---------------- Totals ----------------
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


#invoice totals

def show_invoice_totals(extracted_lines, invoice_totals, tolerance=0.05):
    """
    Compare extracted line item totals with invoice totals.

    Parameters:
        extracted_lines (list[dict]): Each line dict should include
            'Amount excl. GST', 'GST', 'Amount Incl. GST'
        invoice_totals (dict): Expected totals from the invoice footer
        tolerance (float): Allowed difference before marking as mismatch
    """
    st.subheader("📊 Invoice Totals Check")

    # --- Step 1: Calculate sums from extracted lines ---
    df_lines = pd.DataFrame(extracted_lines)
    line_sums = {
        "Amount excl. GST": df_lines.get("Amount excl. GST", pd.Series(dtype=float)).sum(),
        "GST": df_lines.get("GST", pd.Series(dtype=float)).sum(),
        "Amount Incl. GST": df_lines.get("Amount Incl. GST", pd.Series(dtype=float)).sum()
    }

    # --- Step 2: Build comparison table ---
    rows = []
    fields = ["Amount excl. GST", "GST", "Amount Incl. GST"]
    all_match = True

    for field in fields:
        extracted = line_sums.get(field, 0.0)
        expected = invoice_totals.get(field, 0.0)
        diff = extracted - expected
        status = "✅ Match" if abs(diff) <= tolerance else "❌ Mismatch"
        if status == "❌ Mismatch":
            all_match = False
        rows.append({
            "Field": field,
            "Sum of Lines": f"{extracted:,.2f}",
            "Invoice Total": f"{expected:,.2f}",
            "Difference": f"{diff:,.2f}",
            "Status": status
        })

    df = pd.DataFrame(rows)

    # --- Step 3: Visual summary card ---
    if all_match:
        st.markdown(
            f"""
            <div style="padding:1em; background:#e6ffed; border:2px solid #2ecc71; border-radius:10px; text-align:center; font-size:1.2em;">
                ✅ All extracted totals match invoice (within ±{tolerance:.2f}).
            </div>
            """,
            unsafe_allow_html=True
        )
    else:
        st.markdown(
            f"""
            <div style="padding:1em; background:#ffecec; border:2px solid #e74c3c; border-radius:10px; text-align:center; font-size:1.2em;">
                ⚠️ Differences found! Some values differ by more than ±{tolerance:.2f}.
            </div>
            """,
            unsafe_allow_html=True
        )

    # --- Step 4: Detailed comparison table ---
    st.dataframe(df, use_container_width=True)

# -----------------------------
# Learning widget
# -----------------------------

def tokenize_line(line: str) -> str:
    """Convert a line into a token pattern for matching."""
    tokens = line.split()
    return " ".join(["<NUM>" if t.replace(".", "").isdigit() else "<TXT>" for t in tokens])

# -----------------------------
# Learning widget - Improved UX
# -----------------------------
import re, json
import streamlit as st

# -----------------------------
# Tokenizer
# -----------------------------
def tokenize_line(line: str) -> str:
    """Convert a line into a token pattern for matching."""
    tokens = line.split()
    return " ".join(
        ["<NUM>" if t.replace(".", "").isdigit() else "<TXT>" for t in tokens]
    )

# -----------------------------
# Heuristic guesser
# -----------------------------
def guess_field(token: str) -> str:
    """Heuristic rules to auto-suggest field labels based on token shape."""
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", token):
        return "Date"
    if token.upper() == "AUD":
        return "AUD"
    if re.fullmatch(r"\d+(\.\d+)?", token):
        return "Qty."
    if re.fullmatch(r"\d{1,3}(?:,\d{3})*(\.\d{2})?", token):
        return "Amount excl. GST"
    if re.fullmatch(r"\d+(\.\d{2})?", token):
        return "GST"
    if re.fullmatch(r"[A-Za-z0-9\-\/\.]+", token) and any(c.isalpha() for c in token):
        return "Reference"
    return "Description"

# -----------------------------
# Field options (Excel headers)
# -----------------------------
FIELD_OPTIONS = {
    "Ignore": "❌ Ignore",
    "Date": "📅 Invoice Date",
    "Description": "📝 Service / Item description",
    "Reference": "🔖 Reference",
    "Charge Type/Period Reference": "📆 Charge Type / Period Reference",
    "Billed qty": "📊 Billed Quantity",
    "Qty.": "🔢 Quantity",
    "Qty Unit": "📦 Unit of measure (EA, KG, etc.)",
    "Unit Price": "💲 Price per unit",
    "Amount excl. GST": "💰 Net Amount",
    "GST": "🧾 GST",
    "Amount Incl. GST": "💲 Total Amount (incl. GST)",
    "AUD": "💲 Currency AUD"
}

# -----------------------------
# Load/save patterns
# -----------------------------
def load_patterns():
    try:
        with open("learned_patterns.json", "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_patterns(patterns):
    with open("learned_patterns.json", "w") as f:
        json.dump(patterns, f, indent=2)

# -----------------------------
# Learning Widget
# -----------------------------
def show_learning_widget(missed_lines):
    st.subheader("🧠 Teach Me (Learning Widget)")

    if not missed_lines:
        st.info("✅ No unmatched lines found. Nothing to train.")
        return

    st.markdown("### Map tokens to invoice fields")

    learned_patterns = load_patterns()

    for idx, ml in enumerate(missed_lines[:10]):
        st.markdown(f"**📄 Page {ml['Page']}, Line {ml['Line No.']}**")
        line = ml["Line"].strip()
        tokens = line.split()
        token_pattern = tokenize_line(line)

        # --- Prefill ---
        suggested_fields, confidence = [], []
        if token_pattern in learned_patterns:
            field_map = learned_patterns[token_pattern].get("field_map", {})
            group_to_field = {v: k for k, v in field_map.items()}
            for i in range(len(tokens)):
                suggested_fields.append(group_to_field.get(i + 1, "Ignore"))
                confidence.append("✅ Saved")
        else:
            for tok in tokens:
                suggested_fields.append(guess_field(tok))
                confidence.append("🔵 Guessed")

        # --- Dropdowns ---
        dropdowns = []
        cols = st.columns(len(tokens))
        for i, token in enumerate(tokens):
            with cols[i]:
                choice = st.selectbox(
                    f"{token} {confidence[i]}",
                    list(FIELD_OPTIONS.keys()),
                    index=list(FIELD_OPTIONS.keys()).index(suggested_fields[i])
                    if suggested_fields[i] in FIELD_OPTIONS
                    else 0,
                    format_func=lambda x: FIELD_OPTIONS[x],
                    key=f"dd_{idx}_{i}"
                )
                dropdowns.append(choice)

        # --- Save mapping ---
        if st.button(f"💾 Save Mapping for Line {idx+1}", key=f"save_{idx}"):
            regex_parts = []
            field_map = {}
            group_index = 0

            for i, label in enumerate(dropdowns):
                token = tokens[i]
                if label == "Ignore":
                    regex_parts.append(re.escape(token))
                elif label == "Date":
                    regex_parts.append(r"(\d{2}\.\d{2}\.\d{4})")
                    group_index += 1
                    field_map["Date"] = group_index
                elif label == "Qty.":
                    regex_parts.append(r"([\d\.]+)")
                    group_index += 1
                    field_map["Qty."] = group_index
                elif label == "Qty Unit":
                    regex_parts.append(r"(\w+)")
                    group_index += 1
                    field_map["Qty Unit"] = group_index
                elif label in ["Unit Price", "Amount excl. GST", "GST", "Amount Incl. GST"]:
                    regex_parts.append(r"([\d,\.]+)")
                    group_index += 1
                    field_map[label] = group_index
                elif label == "Reference":
                    regex_parts.append(r"([\w\-\/\.]+)")
                    group_index += 1
                    field_map["Reference"] = group_index
                elif label in ["Description", "Charge Type/Period Reference"]:
                    regex_parts.append(r"(.+?)")
                    group_index += 1
                    field_map[label] = group_index
                elif label == "AUD":
                    regex_parts.append(r"AUD")  # literal

            final_regex = r"\s+".join(regex_parts)

            try:
                compiled = re.compile(final_regex)
                m = compiled.match(line)
                if not m:
                    st.warning("⚠️ Pattern didn’t match this example line. Not saved.")
                else:
                    learned_patterns[token_pattern] = {
                        "regex": final_regex,
                        "field_map": field_map,
                        "Charge Type": "Auto-Learned"
                    }
                    save_patterns(learned_patterns)
                    st.success("✅ Pattern saved successfully!")
                    st.json({f: m.group(i) for f, i in field_map.items()})
            except re.error as e:
                st.error(f"❌ Regex error: {e}")
# Pattern Management
# -----------------------------
# -----------------------------
# Pattern Management with Search & Filter
# -----------------------------
def manage_patterns():
    st.subheader("📚 Manage Learned Patterns")

    learned_patterns = load_patterns()
    if not learned_patterns:
        st.info("No saved patterns yet.")
        return

    field_options = list(FIELD_OPTIONS.keys())

    # --- Search + Filter controls ---
    st.markdown("### 🔍 Search & Filter Patterns")
    col1, col2 = st.columns([2,1])
    with col1:
        search_text = st.text_input("Search by token pattern / charge type:", "")
    with col2:
        filter_field = st.selectbox("Filter by field mapping:", ["(all)"] + field_options)

    # Apply filters
    filtered_patterns = {}
    for token_pattern, pdata in learned_patterns.items():
        regex = pdata.get("regex", "")
        ctype = pdata.get("Charge Type", "")
        fmap = pdata.get("field_map", {})

        # Search filter
        if search_text and (search_text.lower() not in token_pattern.lower() and search_text.lower() not in ctype.lower() and search_text.lower() not in regex.lower()):
            continue

        # Field filter
        if filter_field != "(all)" and filter_field not in fmap:
            continue

        filtered_patterns[token_pattern] = pdata

    st.caption(f"Showing {len(filtered_patterns)} of {len(learned_patterns)} patterns")

    # --- Display patterns ---
    for token_pattern, pattern_data in list(filtered_patterns.items()):
        with st.expander(f"🔑 Token Pattern: {token_pattern}"):
            # Regex editor
            new_regex = st.text_area(
                "📝 Current Regex Pattern",
                value=pattern_data.get("regex", ""),
                height=80,
                key=f"regex_{token_pattern}"
            )

            # Charge type
            new_charge_type = st.text_input(
                "Charge Type / Period Reference",
                value=pattern_data.get("Charge Type", "Custom"),
                key=f"charge_{token_pattern}"
            )

            # Field mapping
            st.markdown("**🔗 Field Mapping (groups → invoice fields):**")
            field_map = pattern_data.get("field_map", {})
            new_field_map = {}
            max_groups = max(field_map.values()) if field_map else 5
            for group_index in range(1, max_groups + 1):
                current_field = next((k for k, v in field_map.items() if v == group_index), "(ignore)")
                selection = st.selectbox(
                    f"Group {group_index}",
                    field_options,
                    index=field_options.index(current_field) if current_field in field_options else 0,
                    key=f"{token_pattern}_group_{group_index}"
                )
                if selection != "(ignore)":
                    new_field_map[selection] = group_index

            # Test regex
            st.markdown("**🧪 Test Regex**")
            test_line = st.text_input(
                "Paste a sample invoice line",
                "",
                key=f"test_{token_pattern}"
            )
            if st.button(f"▶️ Run Test ({token_pattern})"):
                try:
                    match = re.match(new_regex, test_line)
                    if match:
                        results = {f: match.group(i) for f, i in new_field_map.items() if i <= len(match.groups())}
                        if results:
                            st.success("✅ Match Found!")
                            st.json(results)
                        else:
                            st.warning("⚠️ Regex matched, but no fields mapped.")
                    else:
                        st.error("❌ No match. Check your regex or sample line.")
                except re.error as e:
                    st.error(f"⚠️ Invalid regex: {e}")

            # Save / Delete
            colA, colB = st.columns(2)
            with colA:
                if st.button(f"💾 Save Changes ({token_pattern})"):
                    learned_patterns[token_pattern]["regex"] = new_regex
                    learned_patterns[token_pattern]["Charge Type"] = new_charge_type
                    learned_patterns[token_pattern]["field_map"] = new_field_map
                    save_patterns(learned_patterns)
                    st.success("✅ Pattern updated!")
            with colB:
                if st.button(f"🗑️ Delete Pattern ({token_pattern})"):
                    del learned_patterns[token_pattern]
                    save_patterns(learned_patterns)
                    st.warning("❌ Pattern deleted!")
                    st.experimental_rerun()

# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="Invoice PDF → Excel", layout="wide")
st.title("📄 Invoice PDF → Excel Extractor")
tab1, tab2, tab3 = st.tabs(["📂 Upload & Extract", "🧠 Teach Me", "📚 Manage Patterns"])


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

        # Learning widget
        show_learning_widget(missed_lines)

    # Pattern manager always available
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
        file_name=f"Opal_Invoice_{invoice_no or 'Unknown'}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
