import streamlit as st
import pandas as pd
import io
import pdfplumber
from aps import extract_service_lines

st.set_page_config(page_title="Invoice Parser", layout="centered")
st.title("📄 Automated Invoice PDF Parser")

uploaded_pdf = st.file_uploader("Upload PDF Invoice File", type="pdf")
uploaded_csv = st.file_uploader("Upload Site Mapping CSV File", type="csv")

if uploaded_pdf and uploaded_csv:
    with st.spinner("Processing invoice..."):
        pdf = pdfplumber.open(io.BytesIO(uploaded_pdf.read()))
        text = "\n".join([page.extract_text() for page in pdf.pages if page.extract_text()])
        df_sites = pd.read_csv(uploaded_csv)
        master_sites = df_sites["standard_name"].tolist()

        df_bookings, df_period_charges = extract_service_lines(text, master_sites)

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            df_bookings.to_excel(writer, sheet_name="Bookings", index=False)
            df_period_charges.to_excel(writer, sheet_name="Period Charges", index=False)

        st.success("✅ Extraction complete!")
        st.download_button(
            label="📥 Download Excel",
            data=output.getvalue(),
            file_name="extracted_invoice_data.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
