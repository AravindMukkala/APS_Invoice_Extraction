import pdfplumber
import pandas as pd
import re
import io
import streamlit as st
from collections import defaultdict


# ============================================================
# CONFIG
# ============================================================

DATE_RE = r"\d{2}\.\d{2}\.\d{4}"
NUMBER_RE = r"[\d,]+\.\d+"

CUSTOMER_RE = re.compile(
    r"^(R-[A-Z0-9]+)\s+(.+)$",
    re.IGNORECASE
)

INVOICE_RE = re.compile(
    r"Invoice No\.\s*(\d+)",
    re.IGNORECASE
)

BILLED_QTY_RE = re.compile(
    r"Billed Qty\s+([\d,]+\.\d+)\s+([A-Za-z]+)",
    re.IGNORECASE
)

TOTAL_PAYABLE_RE = re.compile(
    r"Total Payable\s+"
    r"([\d,]+\.\d{2})\s+"
    r"([\d,]+\.\d{2})\s+"
    r"([\d,]+\.\d{2})\s+AUD",
    re.IGNORECASE
)

CUSTOMER_TOTAL_RE = re.compile(
    r"^(R-[A-Z0-9]+).*?\(TOTAL\)\s+"
    r"([\d,]+\.\d{2})\s+"
    r"([\d,]+\.\d{2})\s+"
    r"([\d,]+\.\d{2})\s+AUD",
    re.IGNORECASE
)

SUBTOTAL_RE = re.compile(
    r"SUB-TOTAL\s+\S+\s+"
    r"([\d,]+\.\d+)\s+"
    r"[A-Za-z]+\s+"
    r"([\d,]+\.\d{2})\s+"
    r"([\d,]+\.\d{2})\s+"
    r"([\d,]+\.\d{2})\s+AUD",
    re.IGNORECASE
)


# ============================================================
# HELPERS
# ============================================================

def clean_number(value):
    """Convert 1,234.56 -> 1234.56"""
    if value is None or value == "":
        return None

    try:
        return float(str(value).replace(",", ""))
    except Exception:
        return None


def normalise_line(line):
    """
    Clean PDF extraction artefacts while preserving meaningful text.
    """
    if not line:
        return ""

    line = line.replace("\xa0", " ")
    line = line.replace("\u200b", "")
    line = re.sub(r"\s+", " ", line)

    return line.strip()


def is_date_line(line):
    return bool(re.match(rf"^{DATE_RE}\b", line))


def extract_date(line):
    match = re.match(rf"^({DATE_RE})\b", line)
    return match.group(1) if match else ""


def remove_date(line):
    return re.sub(rf"^{DATE_RE}\s*", "", line, count=1)


def extract_invoice_number(line):
    match = INVOICE_RE.search(line)
    return match.group(1) if match else ""


def parse_money_tail(line):
    """
    Parse the numeric section at the end of an Opal charge line.

    Two common structures exist.

    Qty/Weight / Manual:
        qty unit
        unit_price price_unit
        item_total
        ex_gst
        gst
        inc_gst
        AUD

    Load:
        qty unit
        unit_price
        ex_gst
        gst
        inc_gst
        AUD
    """

    # --------------------------------------------------------
    # Pattern with PRICE UNIT + 4 monetary values
    # --------------------------------------------------------

    pattern_4 = re.compile(
        rf"(?P<qty>{NUMBER_RE})\s+"
        rf"(?P<qty_unit>[A-Za-z]+)\s+"
        rf"(?P<unit_price>{NUMBER_RE})\s+"
        rf"(?P<price_unit>[A-Za-z]+)\s+"
        rf"(?P<item_total>{NUMBER_RE})\s+"
        rf"(?P<ex_gst>{NUMBER_RE})\s+"
        rf"(?P<gst>{NUMBER_RE})\s+"
        rf"(?P<inc_gst>{NUMBER_RE})\s+AUD$",
        re.IGNORECASE
    )

    match = pattern_4.search(line)

    if match:
        result = match.groupdict()
        result["format"] = "WITH_ITEM_TOTAL"
        return result

    # --------------------------------------------------------
    # Pattern without PRICE UNIT + 3 monetary values
    # --------------------------------------------------------

    pattern_3 = re.compile(
        rf"(?P<qty>{NUMBER_RE})\s+"
        rf"(?P<qty_unit>[A-Za-z]+)\s+"
        rf"(?P<unit_price>{NUMBER_RE})\s+"
        rf"(?P<ex_gst>{NUMBER_RE})\s+"
        rf"(?P<gst>{NUMBER_RE})\s+"
        rf"(?P<inc_gst>{NUMBER_RE})\s+AUD$",
        re.IGNORECASE
    )

    match = pattern_3.search(line)

    if match:
        result = match.groupdict()
        result["price_unit"] = ""
        result["item_total"] = ""

        result["format"] = "WITHOUT_ITEM_TOTAL"

        return result

    return None


def parse_fuel_levy_line(line):
    """
    Parse:

    OPR Fuel Levy-Qty/Wt 24099263
    1.000 FL 1.50 FL 1.50

    or:

    OPR Fuel Levy-Load 409524
    1.000 OT 13.30
    """

    fuel_match = re.search(
        r"^(?P<description>OPR Fuel Levy-(?:Qty/Wt|Load))\s+"
        r"(?P<reference>\S+)\s+"
        rf"(?P<qty>{NUMBER_RE})\s+"
        r"(?P<qty_unit>[A-Za-z]+)\s+"
        rf"(?P<rate>{NUMBER_RE})"
        r"(?:\s+(?P<rate_unit>[A-Za-z]+))?"
        rf"\s+(?P<amount>{NUMBER_RE})"
        r"(?:\s+AUD)?$",
        line,
        re.IGNORECASE
    )

    if not fuel_match:
        return None

    result = fuel_match.groupdict()

    return result


def extract_billed_qty(lines, start_index, max_lookahead=3):
    """
    Look immediately after a charge line for:

        Billed Qty 1.000 FL
    """

    for offset in range(1, max_lookahead + 1):

        index = start_index + offset

        if index >= len(lines):
            break

        line = lines[index]

        match = BILLED_QTY_RE.search(line)

        if match:

            return {
                "billed_qty": match.group(1),
                "billed_qty_unit": match.group(2),
                "line_index": index
            }

        # Don't search through another actual dated charge.
        if is_date_line(line):
            break

    return {
        "billed_qty": "",
        "billed_qty_unit": "",
        "line_index": None
    }


def is_structural_line(line):

    structural_patterns = [
        r"^SUB-TOTAL\b",
        r"^\(?R-[A-Z0-9]+.*\(TOTAL\)",
        r"^Total Payable\b",
        r"^Biller Code:",
        r"^Ref:",
        r"^Telephone & Internet Banking",
        r"^Please remit",
        r"^Account Enquiries:",
        r"^Page \d+ of \d+",
        r"^Customer/Date",
        r"^Description Period/",
        r"^Reference Qty",
        r"^Amount Ex\. GST",
    ]

    return any(
        re.search(pattern, line, re.IGNORECASE)
        for pattern in structural_patterns
    )


def looks_like_charge(line):

    charge_keywords = [
        "FFS - Qty/Weight",
        "FFS - Load",
        "Manual Price",
        "OPR Fuel Levy-Qty/Wt",
        "OPR Fuel Levy-Load",
    ]

    return any(
        keyword.lower() in line.lower()
        for keyword in charge_keywords
    )


def looks_like_customer(line):

    return bool(CUSTOMER_RE.match(line))


# ============================================================
# MAIN PARSER
# ============================================================

def process_pdf(file_stream):

    data = []
    missed_lines = []

    invoice_summaries = []

    current_invoice = ""
    current_invoice_date = ""

    current_customer_code = ""
    current_customer_name = ""

    current_date = ""

    full_text = ""

    # --------------------------------------------------------
    # State used for validation
    # --------------------------------------------------------

    invoice_total_payable = defaultdict(
        lambda: {
            "ex_gst": 0.0,
            "gst": 0.0,
            "inc_gst": 0.0
        }
    )

    customer_totals = {}

    with pdfplumber.open(file_stream) as pdf:

        for page_num, page in enumerate(pdf.pages, start=1):

            text = page.extract_text() or ""

            full_text += text + "\n"

            if not text:
                continue

            raw_lines = text.split("\n")

            # Clean lines
            lines = [
                normalise_line(x)
                for x in raw_lines
            ]

            i = 0

            while i < len(lines):

                line = lines[i]

                if not line:
                    i += 1
                    continue

                # ====================================================
                # INVOICE NUMBER
                # ====================================================

                invoice_number = extract_invoice_number(line)

                if invoice_number:

                    if invoice_number != current_invoice:

                        current_invoice = invoice_number

                        # Look for invoice date on this page
                        for header_line in lines[:25]:

                            date_match = re.search(
                                rf"\b({DATE_RE})\b",
                                header_line
                            )

                            if date_match:

                                current_invoice_date = date_match.group(1)
                                break

                        # Reset customer when new invoice starts
                        current_customer_code = ""
                        current_customer_name = ""

                    i += 1
                    continue

                # ====================================================
                # CUSTOMER / SITE
                # ====================================================

                customer_match = CUSTOMER_RE.match(line)

                if customer_match:

                    current_customer_code = customer_match.group(1)

                    current_customer_name = (
                        customer_match.group(2).strip()
                    )

                    i += 1
                    continue

                # ====================================================
                # CUSTOMER NAME CONTINUATION
                # ====================================================

                # Some customer names wrap over multiple PDF lines.
                if (
                    current_customer_code
                    and not is_date_line(line)
                    and not looks_like_charge(line)
                    and not is_structural_line(line)
                    and not line.startswith("Billed Qty")
                    and not line.startswith("OPR Fuel")
                ):

                    # Only append obvious customer continuation text.
                    if (
                        len(line) < 80
                        and not re.search(r"\d+\.\d+", line)
                    ):

                        current_customer_name += " " + line

                        i += 1
                        continue

                # ====================================================
                # DATE
                # ====================================================

                if is_date_line(line):

                    current_date = extract_date(line)

                # ====================================================
                # FUEL LEVY
                # ====================================================

                fuel = parse_fuel_levy_line(line)

                if fuel:

                    billed = extract_billed_qty(
                        lines,
                        i
                    )

                    data.append({

                        "Invoice No.": current_invoice,

                        "Invoice Date": current_invoice_date,

                        "Customer Code": current_customer_code,

                        "Customer": current_customer_name,

                        "Date": current_date,

                        "Description": fuel["description"],

                        "Charge Type": (
                            "Fuel Levy - Qty/Weight"
                            if "Qty/Wt" in fuel["description"]
                            else "Fuel Levy - Load"
                        ),

                        "Reference": fuel["reference"],

                        "Qty": fuel["qty"],

                        "Qty Unit": fuel["qty_unit"],

                        "Unit Price": fuel["rate"],

                        "Price Unit": fuel.get("rate_unit") or "",

                        "Item Total": "",

                        "Amount Ex GST": fuel["amount"],

                        "GST": "",

                        "Amount Inc GST": "",

                        "Billed Qty": billed["billed_qty"],

                        "Billed Qty Unit": billed["billed_qty_unit"],

                        "Is Fuel Levy": True,

                        "Page": page_num,

                        "Parse Status": "Parsed"

                    })

                    i += 1
                    continue

                # ====================================================
                # MAIN CHARGE LINE
                # ====================================================

                if looks_like_charge(line):

                    charge_line = line

                    # ------------------------------------------------
                    # If line has date, update date
                    # ------------------------------------------------

                    if is_date_line(charge_line):

                        current_date = extract_date(charge_line)

                    # ------------------------------------------------
                    # Gather continuation lines
                    # ------------------------------------------------

                    block_lines = [charge_line]

                    lookahead = 1

                    while (
                        i + lookahead < len(lines)
                        and lookahead <= 5
                    ):

                        next_line = lines[i + lookahead]

                        if not next_line:
                            break

                        if is_date_line(next_line):
                            break

                        if looks_like_customer(next_line):
                            break

                        if is_structural_line(next_line):
                            break

                        if (
                            "Billed Qty" in next_line
                            or "AUD" in next_line
                            or re.search(
                                rf"{NUMBER_RE}\s+{NUMBER_RE}\s+{NUMBER_RE}",
                                next_line
                            )
                        ):

                            block_lines.append(next_line)

                        else:

                            # Useful for multi-line descriptions
                            block_lines.append(next_line)

                        # Stop once we have the AUD line
                        if "AUD" in next_line:

                            break

                        lookahead += 1

                    full_block = " ".join(block_lines)

                    full_block = normalise_line(full_block)

                    # ------------------------------------------------
                    # Determine charge type
                    # ------------------------------------------------

                    if "FFS - Qty/Weight" in full_block:

                        charge_type = "FFS - Qty/Weight"

                    elif "FFS - Load" in full_block:

                        charge_type = "FFS - Load"

                    elif "Manual Price" in full_block:

                        charge_type = "Manual Price"

                    else:

                        charge_type = "Other"

                    # ------------------------------------------------
                    # Parse monetary/quantity tail
                    # ------------------------------------------------

                    parsed = parse_money_tail(full_block)

                    if parsed:

                        # --------------------------------------------
                        # Remove date
                        # --------------------------------------------

                        description_part = remove_date(full_block)

                        # --------------------------------------------
                        # Remove numeric tail
                        # --------------------------------------------

                        numeric_match = re.search(
                            rf"{NUMBER_RE}\s+[A-Za-z]+\s+"
                            rf"{NUMBER_RE}.*AUD$",
                            description_part,
                            re.IGNORECASE
                        )

                        if numeric_match:

                            description_part = (
                                description_part[
                                    :numeric_match.start()
                                ].strip()
                            )

                        # --------------------------------------------
                        # Extract charge type and reference
                        # --------------------------------------------

                        reference = ""

                        if charge_type in (
                            "FFS - Qty/Weight",
                            "FFS - Load"
                        ):

                            marker = charge_type

                            if marker in description_part:

                                before, after = (
                                    description_part.split(
                                        marker,
                                        1
                                    )
                                )

                                description = before.strip()

                                after = after.strip()

                                # Reference is first token
                                # after charge type.
                                ref_match = re.match(
                                    r"^(\S+)",
                                    after
                                )

                                if ref_match:

                                    reference = (
                                        ref_match.group(1)
                                    )

                        elif charge_type == "Manual Price":

                            before, after = (
                                description_part.split(
                                    "Manual Price",
                                    1
                                )
                            )

                            description = before.strip()

                            # Everything after Manual Price may
                            # contain reference / description.
                            reference_match = re.match(
                                r"^(\S+)",
                                after.strip()
                            )

                            if reference_match:

                                reference = (
                                    reference_match.group(1)
                                )

                        else:

                            description = description_part.strip()

                        # ------------------------------------------------
                        # Billed Qty
                        # ------------------------------------------------

                        billed = extract_billed_qty(
                            lines,
                            i + lookahead
                            if i + lookahead < len(lines)
                            else i
                        )

                        # Also search from current line
                        if not billed["billed_qty"]:

                            billed = extract_billed_qty(
                                lines,
                                i
                            )

                        # ------------------------------------------------
                        # Create record
                        # ------------------------------------------------

                        record = {

                            "Invoice No.": current_invoice,

                            "Invoice Date": current_invoice_date,

                            "Customer Code": current_customer_code,

                            "Customer": current_customer_name,

                            "Date": current_date,

                            "Description": description,

                            "Charge Type": charge_type,

                            "Reference": reference,

                            "Qty": parsed["qty"],

                            "Qty Unit": parsed["qty_unit"],

                            "Unit Price": parsed["unit_price"],

                            "Price Unit": parsed.get(
                                "price_unit",
                                ""
                            ),

                            "Item Total": parsed.get(
                                "item_total",
                                ""
                            ),

                            "Amount Ex GST": parsed["ex_gst"],

                            "GST": parsed["gst"],

                            "Amount Inc GST": parsed["inc_gst"],

                            "Billed Qty": billed[
                                "billed_qty"
                            ],

                            "Billed Qty Unit": billed[
                                "billed_qty_unit"
                            ],

                            "Is Fuel Levy": False,

                            "Page": page_num,

                            "Parse Status": "Parsed"

                        }

                        data.append(record)

                        i += max(
                            1,
                            lookahead
                        )

                        continue

                    else:

                        missed_lines.append({

                            "Invoice No.": current_invoice,

                            "Page": page_num,

                            "Line No.": i + 1,

                            "Customer Code":
                                current_customer_code,

                            "Customer":
                                current_customer_name,

                            "Line": full_block,

                            "Reason":
                                "Charge detected but numeric pattern not recognised"

                        })

                        i += 1
                        continue

                # ====================================================
                # SUBTOTAL
                # ====================================================

                subtotal_match = SUBTOTAL_RE.search(line)

                if subtotal_match:

                    customer_totals_key = (
                        current_invoice,
                        current_customer_code
                    )

                    customer_totals[
                        customer_totals_key
                    ] = {

                        "invoice_no":
                            current_invoice,

                        "customer_code":
                            current_customer_code,

                        "customer":
                            current_customer_name,

                        "ex_gst":
                            clean_number(
                                subtotal_match.group(2)
                            ),

                        "gst":
                            clean_number(
                                subtotal_match.group(3)
                            ),

                        "inc_gst":
                            clean_number(
                                subtotal_match.group(4)
                            ),

                        "source":
                            "SUB-TOTAL",

                        "page":
                            page_num
                    }

                    i += 1
                    continue

                # ====================================================
                # CUSTOMER TOTAL
                # ====================================================

                customer_total_match = (
                    CUSTOMER_TOTAL_RE.match(line)
                )

                if customer_total_match:

                    customer_totals_key = (
                        current_invoice,
                        customer_total_match.group(1)
                    )

                    customer_totals[
                        customer_totals_key
                    ] = {

                        "invoice_no":
                            current_invoice,

                        "customer_code":
                            customer_total_match.group(1),

                        "customer":
                            current_customer_name,

                        "ex_gst":
                            clean_number(
                                customer_total_match.group(2)
                            ),

                        "gst":
                            clean_number(
                                customer_total_match.group(3)
                            ),

                        "inc_gst":
                            clean_number(
                                customer_total_match.group(4)
                            ),

                        "source":
                            "CUSTOMER TOTAL",

                        "page":
                            page_num
                    }

                    i += 1
                    continue

                # ====================================================
                # TOTAL PAYABLE
                # ====================================================

                total_match = TOTAL_PAYABLE_RE.search(line)

                if total_match:

                    invoice_total_payable[
                        current_invoice
                    ] = {

                        "ex_gst":
                            clean_number(
                                total_match.group(1)
                            ),

                        "gst":
                            clean_number(
                                total_match.group(2)
                            ),

                        "inc_gst":
                            clean_number(
                                total_match.group(3)
                            ),

                        "page":
                            page_num
                    }

                    i += 1
                    continue

                # ====================================================
                # OTHERWISE IGNORE HEADER / FOOTER
                # ====================================================

                ignored_patterns = [
                    "Opal Packaging Australia",
                    "ABN ",
                    "Tax Invoice",
                    "Payment Terms",
                    "AMOUNT DUE",
                    "Invoice to:",
                    "Biller Code",
                    "Telephone & Internet Banking",
                    "Please remit",
                    "Account Enquiries",
                    "Customer Service",
                    "NSW:",
                    "VIC:",
                    "QLD:",
                    "SA/WA:",
                    "Page "
                ]

                if any(
                    x.lower() in line.lower()
                    for x in ignored_patterns
                ):

                    i += 1
                    continue

                # ====================================================
                # POTENTIAL MISSED DATA
                # ====================================================

                if (
                    re.search(r"\d+\.\d+", line)
                    and (
                        "AUD" in line
                        or "FFS" in line
                        or "Manual" in line
                    )
                ):

                    missed_lines.append({

                        "Invoice No.": current_invoice,

                        "Page": page_num,

                        "Line No.": i + 1,

                        "Customer Code":
                            current_customer_code,

                        "Customer":
                            current_customer_name,

                        "Line": line,

                        "Reason":
                            "Potential invoice data not parsed"

                    })

                i += 1

    # ============================================================
    # DATAFRAME
    # ============================================================

    df = pd.DataFrame(data)

    if not df.empty:

        # Convert numeric columns
        numeric_columns = [
            "Qty",
            "Unit Price",
            "Item Total",
            "Amount Ex GST",
            "GST",
            "Amount Inc GST",
            "Billed Qty"
        ]

        for column in numeric_columns:

            if column in df.columns:

                df[column] = pd.to_numeric(
                    df[column],
                    errors="coerce"
                )

        # --------------------------------------------------------
        # IMPORTANT:
        # Fuel levy is already included in the main charge's
        # Amount Ex GST on the Opal invoice.
        #
        # Therefore do NOT add fuel levy rows again when
        # validating invoice totals.
        # --------------------------------------------------------

        df["Validation Ex GST"] = df.apply(

            lambda row:
                0.0
                if row["Is Fuel Levy"]
                else (
                    row["Amount Ex GST"]
                    if pd.notna(row["Amount Ex GST"])
                    else 0.0
                ),

            axis=1
        )

        df["Validation GST"] = df.apply(

            lambda row:
                0.0
                if row["Is Fuel Levy"]
                else (
                    row["GST"]
                    if pd.notna(row["GST"])
                    else 0.0
                ),

            axis=1
        )

        df["Validation Inc GST"] = df.apply(

            lambda row:
                0.0
                if row["Is Fuel Levy"]
                else (
                    row["Amount Inc GST"]
                    if pd.notna(row["Amount Inc GST"])
                    else 0.0
                ),

            axis=1
        )

    # ============================================================
    # VALIDATION
    # ============================================================

    validation_rows = []

    if not df.empty:

        for invoice_no, invoice_group in df.groupby(
            "Invoice No."
        ):

            extracted_ex = round(
                invoice_group[
                    "Validation Ex GST"
                ].sum(),
                2
            )

            extracted_gst = round(
                invoice_group[
                    "Validation GST"
                ].sum(),
                2
            )

            extracted_inc = round(
                invoice_group[
                    "Validation Inc GST"
                ].sum(),
                2
            )

            expected = invoice_total_payable.get(
                invoice_no,
                {}
            )

            expected_ex = expected.get(
                "ex_gst"
            )

            expected_gst = expected.get(
                "gst"
            )

            expected_inc = expected.get(
                "inc_gst"
            )

            ex_difference = (
                round(
                    extracted_ex - expected_ex,
                    2
                )
                if expected_ex is not None
                else None
            )

            gst_difference = (
                round(
                    extracted_gst - expected_gst,
                    2
                )
                if expected_gst is not None
                else None
            )

            inc_difference = (
                round(
                    extracted_inc - expected_inc,
                    2
                )
                if expected_inc is not None
                else None
            )

            passed = (
                expected_ex is not None
                and expected_gst is not None
                and expected_inc is not None
                and abs(ex_difference) <= 0.02
                and abs(gst_difference) <= 0.02
                and abs(inc_difference) <= 0.02
            )

            validation_rows.append({

                "Invoice No.": invoice_no,

                "Extracted Ex GST":
                    extracted_ex,

                "Invoice Ex GST":
                    expected_ex,

                "Ex GST Difference":
                    ex_difference,

                "Extracted GST":
                    extracted_gst,

                "Invoice GST":
                    expected_gst,

                "GST Difference":
                    gst_difference,

                "Extracted Inc GST":
                    extracted_inc,

                "Invoice Inc GST":
                    expected_inc,

                "Inc GST Difference":
                    inc_difference,

                "Validation":
                    "PASS" if passed else "FAIL"

            })

    validation_df = pd.DataFrame(
        validation_rows
    )

    return (
        df,
        pd.DataFrame(missed_lines),
        validation_df,
        invoice_total_payable,
        customer_totals
    )


# ============================================================
# STREAMLIT UI
# ============================================================

st.set_page_config(
    page_title="Opal Invoice PDF → Excel",
    layout="wide"
)

st.title(
    "📄 Opal Invoice PDF → Excel Extractor"
)

st.caption(
    "Robust parser for Opal Packaging / Opal Recycling invoice format"
)

uploaded_file = st.file_uploader(
    "Upload an Opal Invoice PDF",
    type=["pdf"]
)


if uploaded_file:

    with st.spinner(
        "Processing Opal invoice PDF... please wait ⏳"
    ):

        file_stream = io.BytesIO(
            uploaded_file.read()
        )

        (
            df,
            missed_df,
            validation_df,
            invoice_totals,
            customer_totals
        ) = process_pdf(
            file_stream
        )

    # ========================================================
    # SUMMARY
    # ========================================================

    invoice_count = (
        df["Invoice No."].nunique()
        if not df.empty
        else 0
    )

    fuel_count = (
        int(df["Is Fuel Levy"].sum())
        if not df.empty
        else 0
    )

    st.success(
        f"✅ Extracted {len(df):,} charge rows "
        f"from {invoice_count} invoice(s)"
    )

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric(
            "Invoices",
            invoice_count
        )

    with col2:
        st.metric(
            "Charge Lines",
            len(df)
        )

    with col3:
        st.metric(
            "Fuel Levy Lines",
            fuel_count
        )

    with col4:
        st.metric(
            "Unmatched",
            len(missed_df)
        )

    # ========================================================
    # VALIDATION
    # ========================================================

    st.subheader(
        "🔎 Invoice Validation"
    )

    if not validation_df.empty:

        st.dataframe(
            validation_df,
            use_container_width=True
        )

        failed = validation_df[
            validation_df["Validation"] == "FAIL"
        ]

        if failed.empty:

            st.success(
                "✅ All invoice totals passed validation."
            )

        else:

            st.error(
                f"⚠️ {len(failed)} invoice(s) failed validation."
            )

    # ========================================================
    # EXTRACTED DATA
    # ========================================================

    st.subheader(
        "📋 Extracted Charge Lines"
    )

    if not df.empty:

        st.dataframe(
            df,
            use_container_width=True,
            height=600
        )

    # ========================================================
    # UNMATCHED
    # ========================================================

    if not missed_df.empty:

        st.subheader(
            "⚠️ Unmatched / Review Lines"
        )

        st.warning(
            f"{len(missed_df)} lines require review."
        )

        st.dataframe(
            missed_df,
            use_container_width=True,
            height=400
        )

    # ========================================================
    # EXCEL EXPORT
    # ========================================================

    output = io.BytesIO()

    with pd.ExcelWriter(
        output,
        engine="openpyxl"
    ) as writer:

        if not df.empty:

            df.to_excel(
                writer,
                sheet_name="Invoice Data",
                index=False
            )

        if not validation_df.empty:

            validation_df.to_excel(
                writer,
                sheet_name="Validation",
                index=False
            )

        if not missed_df.empty:

            missed_df.to_excel(
                writer,
                sheet_name="Unmatched Lines",
                index=False
            )

        if customer_totals:

            customer_validation_df = pd.DataFrame(
                customer_totals.values()
            )

            customer_validation_df.to_excel(
                writer,
                sheet_name="Customer Totals",
                index=False
            )

    st.download_button(

        label="📥 Download Excel",

        data=output.getvalue(),

        file_name=(
            "Opal_Invoice_Extract.xlsx"
        ),

        mime=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        )
    )
