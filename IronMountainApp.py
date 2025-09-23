import io
import pdfplumber
import pandas as pd
import re
import streamlit as st

# ----------------------------
# Streamlit Page Config
# ----------------------------
st.set_page_config(page_title="Invoice PDF Parser", layout="wide")

st.title("📄 Iron Mountain Invoice Parser")
st.markdown(
    """
    Upload an **invoice PDF** and this app will:
    - Extract charges, accounts, and subtotals  
    - Highlight mismatches between parsed totals and invoice subtotals  
    - Show any unparsed `SS:` lines separately  
    - Let you **download an Excel file** with all results  

    👉 Start by uploading your PDF below.
    """
)

uploaded_file = st.file_uploader("Upload an Invoice PDF", type=["pdf"])

# ----------------------------
# Function to parse PDF
# ----------------------------
def parse_invoice(pdf_bytes):
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
        r"(.+?)\s+(\d{2}/\d{2}/\d{4})?\s+([A-Z]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)"
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

    # Convert to DataFrame
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

    return df, unmatched_df, invoice_subtotals


# ----------------------------
# Run parser after upload
# ----------------------------
if uploaded_file is not None:
    pdf_bytes = uploaded_file.read()
    df, unmatched_df, invoice_subtotals = parse_invoice(pdf_bytes)

    st.success(f"✅ Extraction complete. {len(df)} rows parsed.")

    # Invoice Totals Section
    with st.expander("📑 Invoice Totals Check", expanded=True):
        for inv, subtotal in invoice_subtotals.items():
            parsed_inv_total = df[df['Invoice Number'] == inv]['Amount'].sum()
            if abs(parsed_inv_total - subtotal) > 0.01:
                st.error(f"Invoice {inv}: Parsed = {parsed_inv_total}, Expected = {subtotal}")
            else:
                st.success(f"Invoice {inv}: ✅ Totals match ({subtotal})")

    # Tabs for results
    tab1, tab2, tab3 = st.tabs(["📊 Parsed Data", "⚠️ Unmatched Lines", "📥 Download"])

    with tab1:
        if not df.empty:
            st.dataframe(df, use_container_width=True)
        else:
            st.info("No parsed data found.")

    with tab2:
        if not unmatched_df.empty:
            st.dataframe(unmatched_df, use_container_width=True)
        else:
            st.success("No unmatched lines 🎉")

    with tab3:
        output_file = io.BytesIO()
        with pd.ExcelWriter(output_file, engine="xlsxwriter") as writer:
            df.to_excel(writer, sheet_name="Parsed Data", index=False)
            unmatched_df.to_excel(writer, sheet_name="Unmatched Lines", index=False)
        output_file.seek(0)

        st.download_button(
            label="📥 Download Excel",
            data=output_file,
            file_name="invoice_data_ironMountain.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

# ----------------------------
# Hide Streamlit branding
# ----------------------------
hide_st_style = """
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    </style>
"""
st.markdown(hide_st_style, unsafe_allow_html=True)
