import streamlit as st
import pdfplumber
import re
import pandas as pd
import io

# ========= Regex Patterns =========
footer_pattern = re.compile(r"(Powered by wastedge\.com|Page:\s*\d+|Tax Invoice:|Invoice Date:|Acc:)", re.IGNORECASE)

header_pattern = {
    "tax_invoice": r"Tax Invoice\s+(\d+)",
    "account_number": r"Account Number\s+([\d.]+)",
    "billing_period": r"Billing Period\s+([\d/]+ to [\d/]+)",
    "invoice_date": r"Invoice Date\s+([\d/]+)",
    "total": r"Total\s+([\d.,]+)"
}

site_pattern = re.compile(
    r"Services\s*/\s*Site:\s*(\d+\.\d+)\s+(.+?)\s*-\s*(.+?)\s*-\s*(.+?)\s+([A-Z]{2,3})\s*(\d+)",
    re.DOTALL
)

# Primary pattern for service lines
pattern = re.compile(
    r"^(\d{2}/\d{2}/\d{2})\s+"      # Date
    r"([\d.]+)\s+"                  # Ref No
    r"(.+?)\s+"                     # Description
    r"(\d+)\s+"                     # Qty
    r"([\d.,]+)\s+"                 # Price
    r"([\d.,]+)\s*"                 # Total
    r"(.*)$"                        # Trailing desc
)

# Alternate pattern (decimal qty)
pattern_alt = re.compile(
    r"""^
    (\d{2}/\d{2}/\d{2})\s+           
    ([\d.]+)\s+                       
    (.+?)\s+                          
    ([\d.]+)\s+                       
    ([\d.,]+)\s+                      
    ([\d.,]+)\s*                      
    (.*)$                             
    """, re.VERBOSE
)


# ========= Functions =========
def extract_pdf_text(pdf_bytes):
    text = ""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    return text


def extract_header(text):
    header_data = {}
    for key, pattern in header_pattern.items():
        match = re.search(pattern, text)
        if match:
            header_data[key] = match.group(1).strip()
    return header_data


def count_service_lines(text):
    pattern = re.compile(r"^\d{2}/\d{2}/\d{2}", re.MULTILINE)
    matches = pattern.findall(text)
    return len(matches)


def parse_invoice(text):
    rows = []
    unmatched_rows = []
    header_data = extract_header(text)

    sites = list(site_pattern.finditer(text))
    for idx, site_match in enumerate(sites):
        site_code, customer_name, address, city, region, zipcode = site_match.groups()
        start_pos = site_match.end()

        if idx + 1 < len(sites):
            end_pos = sites[idx + 1].start()
            site_block = text[start_pos:end_pos]
        else:
            site_block = text[start_pos:]

        lines = [l.strip() for l in site_block.split("\n") if l.strip()]
        i = 0

        while i < len(lines):
            if re.match(r"^\d{2}/\d{2}/\d{2}\s", lines[i]):
                booking_lines = [lines[i]]
                j = i + 1
                while j < len(lines):
                    if re.match(r"^\d{2}/\d{2}/\d{2}\s", lines[j]):
                        break
                    if re.match(r"^Sub\s+Total", lines[j], re.IGNORECASE):
                        break
                    if footer_pattern.search(lines[j]):
                        j += 1
                        continue
                    booking_lines.append(lines[j])
                    j += 1

                full_line = " ".join(booking_lines)

                m = pattern.match(full_line)
                if not m:
                    m = pattern_alt.match(full_line)

                if m:
                    date, ref_no, desc, qty, price, total, trailing_desc = m.groups()
                    description = (desc + " " + trailing_desc).strip()

                    rows.append({
                        "Tax Invoice": header_data.get("tax_invoice", ""),
                        "Site": site_code,
                        "Customer Name": customer_name.strip(),
                        "Address": address.strip(),
                        "City": city.strip(),
                        "Region": region.strip(),
                        "Zip": zipcode.strip(),
                        "Date": date.strip(),
                        "Ref No": ref_no.strip(),
                        "Description": description,
                        "PO": "",
                        "Qty": qty.strip(),
                        "Price": price.replace(",", ""),
                        "Total": total.replace(",", "")
                    })
                else:
                    unmatched_rows.append({
                        "Tax Invoice": header_data.get("tax_invoice", ""),
                        "Site": site_code,
                        "Customer Name": customer_name.strip(),
                        "Address": address.strip(),
                        "City": city.strip(),
                        "Region": region.strip(),
                        "Zip": zipcode.strip(),
                        "Raw Line": full_line
                    })

                i = j
            else:
                i += 1

    return rows, unmatched_rows


def parse_period_charges(text):
    period_rows = []
    period_blocks = re.split(r"Services / Site:", text)

    for block in period_blocks:
        if "Period Charges" in block:
            site_match = re.search(r"(\d+\.\d+)\s+Wasteflex Pty Ltd\s+-\s+(.+)", block)
            if not site_match:
                continue
            site_code = site_match.group(1)
            customer_name_full = site_match.group(2).strip()
            if " - " in customer_name_full:
                customer_name, address = customer_name_full.split(" - ", 1)
            else:
                customer_name = customer_name_full
                address = ""

            lines = block.split("\n")
            try:
                start_idx = lines.index("Period Charges") + 1
            except ValueError:
                continue

            if lines[start_idx].strip().startswith("Description"):
                start_idx += 1

            period_pattern = re.compile(
                r"^(.+?)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)$"
            )

            for line in lines[start_idx:]:
                line = line.strip()
                if not line:
                    continue
                m = period_pattern.match(line)
                if m:
                    description_text, qty, price, total = m.groups()
                    period_rows.append({
                        "Site": site_code,
                        "Customer Name": customer_name.strip(),
                        "Address": address.strip(),
                        "Description": description_text.strip(),
                        "Qty": qty.replace(",", "").strip(),
                        "Price": price.replace(",", "").strip(),
                        "Total": total.replace(",", "").strip(),
                    })
                else:
                    break
    return period_rows


# ========= Streamlit UI =========
st.title("📄 CSC Invoice Extractor")

uploaded_file = st.file_uploader("Upload a PDF invoice", type=["pdf"])

if uploaded_file is not None:
    pdf_bytes = uploaded_file.read()
    st.info("Processing...")

    pdf_text = extract_pdf_text(pdf_bytes)
    rows, unmatched_rows = parse_invoice(pdf_text)
    period_charges = parse_period_charges(pdf_text)

    raw_line_count = count_service_lines(pdf_text)
    extracted_line_count = len(rows)
    unmatched_line_count = len(unmatched_rows)

    # Save results into Excel (in-memory)
    output_file = io.BytesIO()
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, index=False, sheet_name="invoice_data")
        pd.DataFrame(unmatched_rows).to_excel(writer, index=False, sheet_name="unmatched_lines")
        pd.DataFrame(period_charges).to_excel(writer, index=False, sheet_name="Period Charges")
    output_file.seek(0)

    # Show results
    st.success("✅ Extraction complete!")
    st.write(f"Raw service lines found: **{raw_line_count}**")
    st.write(f"Extracted service lines: **{extracted_line_count}**")
    st.write(f"Unmatched lines: **{unmatched_line_count}**")
    st.write(f"Period Charges lines: **{len(period_charges)}**")

    if extracted_line_count + unmatched_line_count < raw_line_count:
        st.warning("⚠️ Some service lines may be missing!")
    else:
        st.info("✔️ All service lines accounted for in extraction or unmatched.")

    if extracted_line_count > 0:
        st.dataframe(pd.DataFrame(rows).head())

    # Download button
    st.download_button(
        label="📥 Download Extracted Excel",
        data=output_file,
        file_name="CSC_invoice_EXTRACTED.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
