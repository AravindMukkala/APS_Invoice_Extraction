import re
import pandas as pd
from rapidfuzz import process, fuzz

def clean_description(desc):
    patterns = [
        r"\b(EPD|TH|AGR|RYD)\w*[\\/]\d+\b",     # codes like AGR309666WW/1
        r"\b(EPD|TH|AGR|RYD)\w*\d+(\.\d+)?\b",  # codes like RYD177399486.0
        r"\bNO DOCKET\b",
        r"\b\d{5,}\.?\d*\b"                    # long numeric strings like 61467.0
    ]
    for pattern in patterns:
        desc = re.sub(pattern, '', desc).strip()
    return desc

def fuzzy_match_site(text, master_sites, threshold=85):
    match, score = process.extractOne(text, master_sites, scorer=fuzz.token_sort_ratio)
    return match if score >= threshold else text

def extract_service_lines(text, master_sites):
    lines = text.split("\n")
    bookings = []
    period_charges = []

    current_invoice = {}
    capture_lines = False
    current_section = "bookings"

    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue

        if "Tax Invoice" in line:
            current_invoice = {}
            capture_lines = False
            continue

        if re.search(r"Date\s+Description\s+Qty\s+Rate|Description\s+Qty\s+Rate", line):
            capture_lines = True
            current_section = "bookings"
            continue
        elif re.search(r"Description\s+Period\s+Qty\s+Rate", line):
            capture_lines = True
            current_section = "period_charges"
            continue
        elif any(x in line for x in ["Subtotal", "Total", "Tax", "Amount Payable"]):
            capture_lines = False
            continue

        if capture_lines:
            fields = re.split(r"\s{2,}", line)
            if len(fields) < 3:
                continue

            description = clean_description(fields[0])
            qty = rate = ""

            for field in fields[1:]:
                if re.match(r"^\d+(\.\d+)?$", field.strip()):
                    if not qty:
                        qty = field.strip()
                    else:
                        rate = field.strip()

            if len(description.split()) < 3:
                continue  # Likely invalid

            site = fuzzy_match_site(description, master_sites)

            entry = {
                "Site": site,
                "Description": description,
                "Qty": qty,
                "Rate": rate
            }

            if current_section == "bookings":
                bookings.append(entry)
            else:
                period_charges.append(entry)

    df_bookings = pd.DataFrame(bookings)
    df_period_charges = pd.DataFrame(period_charges)

    return df_bookings, df_period_charges
