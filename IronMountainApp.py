import io
import pdfplumber
import pandas as pd
import re
import streamlit as st

# ----------------------------
# Streamlit UI
# ----------------------------
st.set_page_config(page_title="Invoice PDF Parser", layout="wide")
st.title("📄 Iron Mountain Invoice Parser")

uploaded_file = st.file_uploader("Upload an Invoice PDF", type=["pdf"])

# ----------------------------
# Function to parse PDF
# ----------------------------
def parse_invoice(pdf_bytes):
    st.info("Parsing PDF... Please wait.")

    parsed_data = []
    all_lines = []
    parsed_lines = set()
    invoice_subtotals = {}

    # Regex patterns
    account_id_pattern = re.compile(r"Account ID:\s*(\d+)")
    invoice_number_pattern = re.compile(r"Invoice Number:\s*([A-Z0-9]+)")
    level2_account_pattern = re.compile(r"Level 2 Account:\s*(\d+).*?Level 2 Account Name:\s*([A-Za-z\s&]+)")
    service_address_pattern = re.compile(r"Service Address:\s*(.+)")
    order_no_pattern = re.compile(r"IM Order No\.\:\s*([A-Z0-9]+)")
    charge_line_pattern = re.compile(
        r"(SS:.*?)\s+(\d{2}/\d{2}/\d{4})?\s+([A-Z]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)"
    )
    subtotal_pattern = re.compile(r"SUBTOTAL:\s*\$?([\d,]+\.\d{2})", re.IGNORECASE)

    # Context variables
    account_id = None
    invoice_number = None
    level2_account = None
    level2_name = None
    service_address = None
    order_no = None
    ignore_ss_after_list_of_charges = False

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue

            lines = text.split("\n")
            all_lines.extend(lines)

            for line in lines:
                # Detect start of summary/total section
                if "List of Charges" in line:
                    ignore_ss_after_list_of_charges = True

                # Detect Invoice Number (reset invoice context EXCEPT account_id)
                m = invoice_number_pattern.search(line)
                if m:
                    invoice_number = m.group(1).strip()
                    level2_account = None
                    level2_name = None
                    service_address = None
                    order_no = None
                    ignore_ss_after_list_of_charges = False

                # Detect Account ID
                m = account_id_pattern.search(line)
                if m:
                    account_id = m.group(1).strip()

                # Detect Level 2 Account
                m = level2_account_pattern.search(line)
                if m:
                    level2_account = m.group(1).strip()
                    level2_name = m.group(2).strip()
                    service_address = None
                    order_no = None

                # Detect Service Address
                if "Service Address:" in line:
                    sm = service_address_pattern.search(line)
                    if sm:
                        service_address = sm.group(1).strip()

                # Detect Order No
                if "IM Order No.:" in line:
                    om = order_no_pattern.search(line)
                    if om:
                        order_no = om.group(1).strip()

                # Detect charge line
                m = charge_line_pattern.search(line)
                if m and (account_id or level2_account) and not ignore_ss_after_list_of_charges:
                    charge_desc = m.group(1).strip()
                    charge_date = m.group(2).strip() if m.group(2) else ""
                    uom = m.group(3).strip()
                    price = m.group(4).strip()
                    qty = m.group(5).strip()
                    amount = m.group(6).strip()

                    parsed_data.append({
                        "Account ID": account_id,
                        "Invoice Number": invoice_number,
                        "Level 2 Account": level2_account,
                        "Level 2 Account Name": level2_name,
                        "Service Address": service_address,
                        "IM Order No.": order_no,
                        "Charge Description": charge_desc,
                        "Charge Period / Date": charge_date,
                        "UOM": uom,
                        "Price": price,
                        "Quantity": qty,
                        "Amount": amount
                    })

                    parsed_lines.add(line)

                # Detect SUBTOTAL for the current invoice
                m = subtotal_pattern.search(line)
                if m and invoice_number:
                    invoice_subtotals[invoice_number] = float(m.group(1).replace(",", ""))
                    ignore_ss_after_list_of_charges = False

    # ----------------------------
    # Convert to DataFrame
    # ----------------------------
    df = pd.DataFrame(parsed_data)
    if not df.empty:
        df["Amount"] = df["Amount"].astype(float)
        df['Invoice Subtotal'] = df['Invoice Number'].map(invoice_subtotals)

    # Collect unmatched SS: lines
    unmatched_lines = [
        line for line in all_lines
        if line.startswith("SS:") and line not in parsed_lines and
           ("Account ID:" in "\n".join(all_lines[:all_lines.index(line)]) or
            "Level 2 Account" in "\n".join(all_lines[:all_lines.index(line)])) and
           "List of Charges" not in "\n".join(all_lines[:all_lines.index(line)])
    ]
    unmatched_df = pd.DataFrame(unmatched_lines, columns=["Unparsed Line"])

    # ----------------------------
    # Display results
    # ----------------------------
    st.success(f"Extraction complete. {len(df)} rows parsed.")

    # Show summary
    for inv, subtotal in invoice_subtotals.items():
        parsed_inv_total = df[df['Invoice Number'] == inv]['Amount'].sum()
        st.write(f"**Invoice {inv}**: Parsed Total = {parsed_inv_total}, Subtotal = {subtotal}")
        if abs(parsed_inv_total - subtotal) > 0.01:
            st.warning(f"⚠️ WARNING: Invoice {inv} total mismatch!")

    if not unmatched_df.empty:
        st.warning(f"{len(unmatched_df)} potential charge lines were not parsed.")

    # Download Excel
    output_file = io.BytesIO()
    with pd.ExcelWriter(output_file, engine="xlsxwriter") as writer:
        df.to_excel(writer, sheet_name="Parsed Data", index=False)
        unmatched_df.to_excel(writer, sheet_name="Unmatched Lines", index=False)
    output_file.seek(0)

    st.download_button(
        label="📥 Download Parsed Excel",
        data=output_file,
        file_name="invoice_data_ironMountain.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    # Display tables
    if not df.empty:
        st.subheader("Parsed Data Preview")
        st.dataframe(df.head(50))

    if not unmatched_df.empty:
        st.subheader("Unmatched Lines Preview")
        st.dataframe(unmatched_df.head(50))


# ----------------------------
# Run parser after upload
# ----------------------------
if uploaded_file is not None:
    pdf_bytes = uploaded_file.read()
    parse_invoice(pdf_bytes)
