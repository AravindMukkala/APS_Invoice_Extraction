import io
import re

import pandas as pd
import pdfplumber
import streamlit as st

# =====================================================================
# Regex patterns
# =====================================================================

# Page footer carries the invoice number for every page -> reliable way to
# tag each line with its Tax Invoice, even when a PDF holds several invoices.
PAGE_FOOTER_RE = re.compile(r"Page:\s*\d+\s+Tax Invoice:\s*(\d+)", re.IGNORECASE)

# Lines that are never data
NOISE_RE = re.compile(
    r"^(Powered by wastedge\.com|Page:\s*\d+|Date Ref No Description|Description PO Qty Price Total|Totals$)",
    re.IGNORECASE,
)

SITE_START_RE = re.compile(r"^Services\s*/\s*Site:\s*(\d+\.\d+)\s*(.*)$")
DATE_START_RE = re.compile(r"^\d{2}/\d{2}/\d{2}\b")

# Service line, anchored on the RIGHT so the description can contain any numbers
# (e.g. "Excess Disposal Charge 143 KG", "1.4 Litre Sharp Container").
# Ref No is optional (Fuel Levy lines have none) and Price may be a % (6.90%).
SERVICE_LINE_RE = re.compile(
    r"^(?P<date>\d{2}/\d{2}/\d{2})\s+"
    r"(?:(?P<ref>\d+\.\d+)\s+)?"
    r"(?P<desc>.+?)\s+"
    r"(?P<qty>-?\d[\d,]*(?:\.\d+)?)\s+"
    r"(?P<price>-?\d[\d,]*(?:\.\d+)?%?)\s+"
    r"(?P<total>-?\d[\d,]*\.\d{2})$"
)

PERIOD_LINE_RE = re.compile(
    r"^(?P<desc>Site:.+?)\s+"
    r"(?P<qty>-?\d[\d,]*(?:\.\d+)?)\s+"
    r"(?P<price>-?\d[\d,]*(?:\.\d+)?)\s+"
    r"(?P<total>-?\d[\d,]*\.\d{2})$"
)

SUBTOTAL_RE = re.compile(r"^Sub\s+Total:\s*([\d.,-]+)\s+([\d.,-]+)$", re.IGNORECASE)
SECTION_TOTAL_RE = re.compile(r"^Total:\s*([\d.,-]+)$")
EXCL_GST_RE = re.compile(r"Total\s*\(Excl\.?\s*GST\):\s*([\d.,-]+)", re.IGNORECASE)
GST_RE = re.compile(r"^GST:\s*([\d.,-]+)", re.IGNORECASE)
INCL_GST_RE = re.compile(r"Total\s*\(Inc\.?\s*GST\):\s*([\d.,-]+)", re.IGNORECASE)

HEADER_RE = re.compile(
    r"Tax Invoice\s+(\d+).*?"
    r"Account Number\s+([\d.]+).*?"
    r"Billing Period\s+([\d/]+\s+to\s+[\d/]+).*?"
    r"Invoice Date\s+([\d/]+).*?"
    r"Total\s+([\d.,]+)",
    re.DOTALL,
)

STATE_POSTCODE_RE = re.compile(r"^(.*?)\s*\b(VIC|NSW|QLD|SA|WA|TAS|NT|ACT)\s+(\d{4})\s*$")

# Used to split "325 Canterbury Rd Bayswater North" into street / suburb.
# The FIRST street-type word wins (so "77 Tareeda Way Ocean Grove" -> suburb "Ocean Grove").
STREET_TYPES = (
    "Street|St|Road|Rd|Drive|Dr|Avenue|Ave|Av|Boulevard|Blvd|Highway|Hwy|Parade|Pde|"
    "Lane|Ln|Grove|Gr|Way|Court|Ct|Place|Pl|Crescent|Cres|Close|Terrace|Tce|Circuit|Cct|"
    "Esplanade|Square|Hub|Walk|Rise|Track|Promenade"
)
STREET_SPLIT_RE = re.compile(rf"^(.*?\b(?:{STREET_TYPES})\.?)\s+(.+)$", re.IGNORECASE)


# =====================================================================
# Helpers
# =====================================================================
def to_num(value):
    if value is None or value == "":
        return None
    return float(str(value).replace(",", "").replace("%", "").strip())


def extract_pdf_lines(pdf_bytes):
    """Return a list of (tax_invoice, line) for every text line in the PDF."""
    out = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            m = PAGE_FOOTER_RE.search(text)
            page_invoice = m.group(1) if m else ""
            for line in text.split("\n"):
                line = line.strip()
                if line:
                    out.append((page_invoice, line))
    return out


def extract_headers(full_text):
    headers = []
    for m in HEADER_RE.finditer(full_text):
        tax_invoice, account, period, inv_date, total = m.groups()
        headers.append({
            "Tax Invoice": tax_invoice.strip(),
            "Account Number": account.strip(),
            "Billing Period": re.sub(r"\s+", " ", period.strip()),
            "Invoice Date": inv_date.strip(),
            "Header Total": to_num(total),
        })
    return headers


def parse_site_header(site_code, header_text):
    """
    "Wasteflex Pty Ltd - Heritage Bayswater - Heritage Gardens - 325 Canterbury Rd Bayswater North VIC 3153"
      -> customer, site name, street, suburb, state, postcode
    """
    header_text = re.sub(r"\s+", " ", header_text).strip()
    # Split on hyphens used as separators (handles "Ltd -X", "Ltd- X", "Ltd - X")
    parts = [p.strip() for p in re.split(r"\s+-\s*|\s*-\s+", header_text) if p.strip()]

    customer = parts[0] if parts else ""
    address_full = parts[-1] if len(parts) >= 2 else ""
    site_name = " - ".join(parts[1:-1]) if len(parts) >= 3 else ""

    street, suburb, state, postcode = address_full, "", "", ""
    m = STATE_POSTCODE_RE.match(address_full)
    if m:
        street_suburb, state, postcode = m.group(1).strip(), m.group(2), m.group(3)
        street = street_suburb
        sm = STREET_SPLIT_RE.match(street_suburb)
        if sm:
            street, suburb = sm.group(1).strip(), sm.group(2).strip()

    return {
        "Site": site_code,
        "Customer Name": customer,
        "Site Name": site_name,
        "Address": street,
        "City": suburb,
        "Region": state,
        "Zip": postcode,
    }


# =====================================================================
# Main parser (line-by-line state machine)
# =====================================================================
def parse_invoice(lines):
    service_rows, period_rows, unmatched_rows, subtotal_rows = [], [], [], []
    invoice_totals = {}  # tax_invoice -> dict of totals printed on the PDF

    site_info = {}
    header_buffer = None       # collecting a (possibly multi-line) site header
    pending_site_code = None
    section = None             # "services" | "period" | None
    current = None             # row currently receiving continuation lines
    group_no = 0
    group_rows = []

    def inv_totals(inv):
        return invoice_totals.setdefault(inv, {
            "Services Total (PDF)": None,
            "Period Charges Total (PDF)": None,
            "Total Excl GST (PDF)": None,
            "GST (PDF)": None,
            "Total Incl GST (PDF)": None,
        })

    for tax_invoice, line in lines:
        if NOISE_RE.match(line):
            continue  # skip footer / table headers but keep `current` open across page breaks

        # ---- New site header -------------------------------------------------
        m = SITE_START_RE.match(line)
        if m:
            current = None
            pending_site_code = m.group(1)
            header_buffer = [m.group(2)]
            section = None
            continue

        # ---- Header continuation until "Services" / "Period Charges" ---------
        if header_buffer is not None:
            if line in ("Services", "Period Charges"):
                site_info = parse_site_header(pending_site_code, " ".join(header_buffer))
                header_buffer = None
                section = "services" if line == "Services" else "period"
            else:
                header_buffer.append(line)
            continue

        # ---- Invoice-level totals block ---------------------------------------
        for regex, key in ((EXCL_GST_RE, "Total Excl GST (PDF)"),
                           (GST_RE, "GST (PDF)"),
                           (INCL_GST_RE, "Total Incl GST (PDF)")):
            mt = regex.search(line)
            if mt:
                inv_totals(tax_invoice)[key] = to_num(mt.group(1))
                current = None
                section = None
                break
        else:
            mt = None
        if mt:
            continue

        # ---- Section total ("Total: 83935.23") ------------------------------
        m = SECTION_TOTAL_RE.match(line)
        if m:
            key = "Services Total (PDF)" if section == "services" else "Period Charges Total (PDF)"
            inv_totals(tax_invoice)[key] = to_num(m.group(1))
            current = None
            continue

        if section == "services":
            # Sub Total closes a service group
            m = SUBTOTAL_RE.match(line)
            if m:
                calc = round(sum(r["Total"] for r in group_rows), 2)
                pdf_sub = to_num(m.group(2))
                subtotal_rows.append({
                    "Tax Invoice": tax_invoice,
                    "Site": site_info.get("Site", ""),
                    "Site Name": site_info.get("Site Name", ""),
                    "Group": group_no,
                    "Lines": len(group_rows),
                    "PDF Sub Total": pdf_sub,
                    "Calculated Sub Total": calc,
                    "Difference": round(pdf_sub - calc, 2),
                    "Status": "OK" if abs(pdf_sub - calc) < 0.01 else "MISMATCH",
                })
                group_rows = []
                current = None
                continue

            if DATE_START_RE.match(line):
                if not group_rows:
                    group_no += 1
                m = SERVICE_LINE_RE.match(line)
                if m:
                    price_raw = m.group("price")
                    current = {
                        "Tax Invoice": tax_invoice,
                        **site_info,
                        "Group": group_no,
                        "Date": m.group("date"),
                        "Ref No": m.group("ref") or "",
                        "Description": m.group("desc").strip(),
                        "PO": "",
                        "Qty": to_num(m.group("qty")),
                        "Price": to_num(price_raw),
                        "Price Unit": "%" if price_raw.endswith("%") else "$",
                        "Total": to_num(m.group("total")),
                    }
                    service_rows.append(current)
                    group_rows.append(current)
                else:
                    current = None
                    unmatched_rows.append({
                        "Tax Invoice": tax_invoice, **site_info,
                        "Section": "Services", "Raw Line": line,
                    })
                continue

            # Anything else inside a services block = wrapped description text
            if current is not None:
                current["Description"] += " " + line
            else:
                unmatched_rows.append({
                    "Tax Invoice": tax_invoice, **site_info,
                    "Section": "Services", "Raw Line": line,
                })
            continue

        if section == "period":
            m = PERIOD_LINE_RE.match(line)
            if m:
                current = {
                    "Tax Invoice": tax_invoice,
                    **site_info,
                    "Description": re.sub(r"^Site:\s*\d+\.\d+\s*", "", m.group("desc")).strip(),
                    "PO": "",
                    "Qty": to_num(m.group("qty")),
                    "Price": to_num(m.group("price")),
                    "Total": to_num(m.group("total")),
                }
                period_rows.append(current)
            elif current is not None and not line.startswith("Site:"):
                current["Description"] += " " + line   # e.g. "Weekly)"
            else:
                current = None
                unmatched_rows.append({
                    "Tax Invoice": tax_invoice, **site_info,
                    "Section": "Period Charges", "Raw Line": line,
                })
            continue

    return service_rows, period_rows, unmatched_rows, subtotal_rows, invoice_totals


def build_validation(headers, service_rows, period_rows, invoice_totals):
    df_s = pd.DataFrame(service_rows)
    df_p = pd.DataFrame(period_rows)
    header_map = {h["Tax Invoice"]: h for h in headers}
    invoices = sorted(set(invoice_totals) | set(header_map)
                      | set(df_s.get("Tax Invoice", pd.Series(dtype=str))))

    out = []
    for inv in invoices:
        t = invoice_totals.get(inv, {})
        s_sum = round(df_s.loc[df_s["Tax Invoice"] == inv, "Total"].sum(), 2) if not df_s.empty else 0.0
        p_sum = round(df_p.loc[df_p["Tax Invoice"] == inv, "Total"].sum(), 2) if not df_p.empty else 0.0
        excl = round(s_sum + p_sum, 2)
        gst_pdf = t.get("GST (PDF)")
        incl_calc = round(excl + gst_pdf, 2) if gst_pdf is not None else round(excl * 1.1, 2)

        def check(pdf_val, calc_val):
            if pdf_val is None:
                return "N/A"
            return "OK" if abs(pdf_val - calc_val) < 0.01 else f"MISMATCH ({pdf_val - calc_val:+,.2f})"

        out.append({
            "Tax Invoice": inv,
            "Account Number": header_map.get(inv, {}).get("Account Number", ""),
            "Billing Period": header_map.get(inv, {}).get("Billing Period", ""),
            "Services Total (Extracted)": s_sum,
            "Services Total (PDF)": t.get("Services Total (PDF)"),
            "Services Check": check(t.get("Services Total (PDF)"), s_sum),
            "Period Charges (Extracted)": p_sum,
            "Period Charges (PDF)": t.get("Period Charges Total (PDF)"),
            "Period Check": check(t.get("Period Charges Total (PDF)"), p_sum),
            "Total Excl GST (Extracted)": excl,
            "Total Excl GST (PDF)": t.get("Total Excl GST (PDF)"),
            "Excl GST Check": check(t.get("Total Excl GST (PDF)"), excl),
            "GST 10% (Calculated)": round(excl * 0.10, 2),
            "GST (PDF)": gst_pdf,
            "Total Incl GST (Extracted + PDF GST)": incl_calc,
            "Header Total (PDF)": header_map.get(inv, {}).get("Header Total"),
            "Incl GST Check": check(header_map.get(inv, {}).get("Header Total"), incl_calc),
        })
    return pd.DataFrame(out)


def to_excel(sheets):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, index=False, sheet_name=name)
            ws = writer.sheets[name]
            for col_cells in ws.columns:
                width = max((len(str(c.value)) for c in col_cells if c.value is not None), default=8)
                ws.column_dimensions[col_cells[0].column_letter].width = min(width + 2, 60)
            ws.freeze_panes = "A2"
    buf.seek(0)
    return buf


# =====================================================================
# Streamlit UI
# =====================================================================
def main():
    st.title("📄 CSC Invoice Extractor")
    uploaded_file = st.file_uploader("Upload a PDF invoice", type=["pdf"])
    if uploaded_file is None:
        return

    with st.spinner("Processing..."):
        pdf_bytes = uploaded_file.read()
        lines = extract_pdf_lines(pdf_bytes)
        full_text = "\n".join(l for _, l in lines)

        headers = extract_headers(full_text)
        services, period, unmatched, subtotals, inv_totals = parse_invoice(lines)
        df_services = pd.DataFrame(services)
        df_period = pd.DataFrame(period)
        df_unmatched = pd.DataFrame(unmatched)
        df_subtotals = pd.DataFrame(subtotals)
        df_validation = build_validation(headers, services, period, inv_totals)

    raw_line_count = sum(1 for _, l in lines if DATE_START_RE.match(l))
    st.success("✅ Extraction complete!")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Raw service lines", raw_line_count)
    c2.metric("Extracted service lines", len(df_services))
    c3.metric("Period charge lines", len(df_period))
    c4.metric("Unmatched lines", len(df_unmatched))

    st.subheader("📊 Invoice Validation")
    for _, v in df_validation.iterrows():
        st.markdown(f"**Tax Invoice {v['Tax Invoice']}**  ·  Account {v['Account Number']}  ·  {v['Billing Period']}")
        st.write(f"Services: {v['Services Total (Extracted)']:,.2f} (PDF: {v['Services Total (PDF)'] or 0:,.2f}) → {v['Services Check']}")
        st.write(f"Period Charges: {v['Period Charges (Extracted)']:,.2f} (PDF: {v['Period Charges (PDF)'] or 0:,.2f}) → {v['Period Check']}")
        st.write(f"Total excl. GST: {v['Total Excl GST (Extracted)']:,.2f} (PDF: {v['Total Excl GST (PDF)'] or 0:,.2f}) → {v['Excl GST Check']}")
        st.write(f"GST on PDF: {v['GST (PDF)'] or 0:,.2f}  ·  10% recalculated: {v['GST 10% (Calculated)']:,.2f}")
        st.write(f"Total incl. GST: {v['Total Incl GST (Extracted + PDF GST)']:,.2f} (PDF header: {v['Header Total (PDF)'] or 0:,.2f}) → {v['Incl GST Check']}")

        checks = [v["Services Check"], v["Period Check"], v["Excl GST Check"], v["Incl GST Check"]]
        if all(c in ("OK", "N/A") for c in checks):
            st.success("✅ All extracted lines reconcile to the PDF totals.")
        else:
            st.error("❌ Totals do not reconcile – check the 'subtotal_check' and 'unmatched_lines' sheets.")
        if v["GST (PDF)"] is not None and abs(v["GST (PDF)"] - v["GST 10% (Calculated)"]) >= 0.01:
            st.warning(f"ℹ️ GST on the PDF differs from a flat 10% by "
                       f"{v['GST (PDF)'] - v['GST 10% (Calculated)']:+,.2f} (supplier-side rounding).")

    if not df_subtotals.empty:
        bad = df_subtotals[df_subtotals["Status"] != "OK"]
        st.write(f"Sub Total groups checked: **{len(df_subtotals)}**, mismatches: **{len(bad)}**")
        if not bad.empty:
            st.dataframe(bad)

    if not df_unmatched.empty:
        st.subheader("⚠️ Unmatched lines")
        st.dataframe(df_unmatched)

    if not df_services.empty:
        st.subheader("Preview")
        st.dataframe(df_services.head(20))

    excel = to_excel({
        "invoice_data": df_services,
        "Period Charges": df_period,
        "unmatched_lines": df_unmatched,
        "subtotal_check": df_subtotals,
        "validation": df_validation,
    })
    inv_no = headers[0]["Tax Invoice"] if headers else "invoice"
    st.download_button(
        label="📥 Download Extracted Excel",
        data=excel,
        file_name=f"CSC_invoice_{inv_no}_EXTRACTED.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    main()
