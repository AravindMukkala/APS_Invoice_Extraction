import io
import re
from decimal import Decimal, InvalidOperation

import pandas as pd
import pdfplumber
import streamlit as st


# ============================================================
# CONFIGURATION
# ============================================================

DATE_RE = r"\d{2}\.\d{2}\.\d{4}"

# Opal customer code
CUSTOMER_CODE_RE = re.compile(
    r"^(R-[A-Z0-9]+)\s+(.+?)\s*$",
    re.IGNORECASE
)

# Invoice number
INVOICE_RE = re.compile(
    r"Invoice No\.\s*(\d+)",
    re.IGNORECASE
)

# Invoice date
INVOICE_DATE_RE = re.compile(
    r"\bDate\s+(\d{2}\.\d{2}\.\d{4})\b",
    re.IGNORECASE
)

# Charge markers
FFS_QTY_RE = re.compile(r"\bFFS\s*-\s*Qty/Weight\b", re.IGNORECASE)
FFS_LOAD_RE = re.compile(r"\bFFS\s*-\s*Load\b", re.IGNORECASE)
MANUAL_RE = re.compile(r"\bManual\s+Price\b", re.IGNORECASE)

# Fuel levy
FUEL_RE = re.compile(
    r"^OPR\s+Fuel\s+Levy",
    re.IGNORECASE
)

# Billed quantity
BILLED_QTY_RE = re.compile(
    r"\bBilled\s+Qty\s+([\d,]+\.\d+)\s+([A-Za-z]+)\b",
    re.IGNORECASE
)

# Printed totals
TOTAL_PAYABLE_RE = re.compile(
    r"Total\s+Payable\s+"
    r"([\d,]+\.\d{2})\s+"
    r"([\d,]+\.\d{2})\s+"
    r"([\d,]+\.\d{2})\s+AUD",
    re.IGNORECASE
)

SUBTOTAL_RE = re.compile(
    r"SUB-TOTAL\s+"
    r"(\S+)\s+"
    r"([\d,]+\.\d+)\s+"
    r"([A-Za-z]+)\s+"
    r"([\d,]+\.\d{2})\s+"
    r"([\d,]+\.\d{2})\s+"
    r"([\d,]+\.\d{2})\s+AUD",
    re.IGNORECASE
)

CUSTOMER_TOTAL_RE = re.compile(
    r"^(R-[A-Z0-9]+)-(.+?)\s+\(TOTAL\)\s+"
    r"([\d,]+\.\d{2})\s+"
    r"([\d,]+\.\d{2})\s+"
    r"([\d,]+\.\d{2})\s+AUD",
    re.IGNORECASE
)


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def clean_line(line):
    """
    Clean PDF extraction artefacts without destroying useful data.
    """
    if line is None:
        return ""

    line = str(line).replace("\xa0", " ")
    line = re.sub(r"\s+", " ", line).strip()

    # Opal PDFs sometimes produce a standalone A between reference
    # and numeric details.
    if line.upper() == "A":
        return ""

    return line


def clean_number(value):
    """
    Convert Opal number strings such as 1,397.91 into float.
    """
    if value is None or value == "":
        return None

    try:
        return float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return None


def money(value):
    """
    Convert number to 2-decimal float.
    """
    number = clean_number(value)

    if number is None:
        return None

    return round(number, 2)


def normalise_customer_name(name):
    """
    Clean customer names while preserving the actual customer text.
    """
    if not name:
        return ""

    name = clean_line(name)

    # Do not allow obvious headers/footers to become customer names.
    bad_starts = [
        "Customer/",
        "Date",
        "Description",
        "Period/",
        "Charge Type",
        "Reference",
        "Billed Qty",
        "SUB-TOTAL",
        "Total Payable",
        "Opal Packaging",
        "ABN ",
        "Tax Invoice",
        "Biller Code",
        "Telephone",
        "Please remit",
        "Account Enquiries",
    ]

    for bad in bad_starts:
        if name.lower().startswith(bad.lower()):
            return ""

    return name


def is_customer_line(line):
    """
    Detect real customer/site lines.

    Example:
        R-AUS5019 Australian Clutch Services
        R-HAR4009 Hart Sport Brendale (Wasteflex)
    """

    if not line:
        return False

    # Never treat customer total lines as customer headers.
    if "(TOTAL)" in line.upper():
        return False

    # Never treat invoice account headers as customer headers.
    if line.lower().startswith("account no."):
        return False

    return CUSTOMER_CODE_RE.match(line) is not None


def parse_customer_line(line):
    match = CUSTOMER_CODE_RE.match(line)

    if not match:
        return None, None

    code = match.group(1).strip()
    name = normalise_customer_name(match.group(2))

    if not name:
        return None, None

    return code, name


def is_date_start(line):
    return re.match(
        rf"^{DATE_RE}\b",
        line
    ) is not None


def is_header_or_footer(line):
    """
    Lines which should never be treated as invoice charge lines.
    """

    upper = line.upper()

    blocked = [
        "CUSTOMER/",
        "DESCRIPTION PERIOD",
        "CHARGE TYPE",
        "REFERENCE QTY",
        "BILLED QTY",
        "SUB-TOTAL",
        "TOTAL PAYABLE",
        "AMOUNT DUE",
        "PAYMENT TERMS",
        "BILLER CODE",
        "TELEPHONE & INTERNET BANKING",
        "PLEASE REMIT PAYMENT",
        "ACCOUNT ENQUIRIES",
        "ALL DEALINGS WITH",
        "OPAL PACKAGING AUSTRALIA",
        "OPAL RECYCLING",
        "TAX INVOICE",
        "ABN ",
        "PAGE ",
    ]

    return any(x in upper for x in blocked)


def get_billed_qty(lines, start_index, lookahead=4):
    """
    Look ahead for:
        Billed Qty 5.000 BL
    """

    end = min(
        len(lines),
        start_index + lookahead + 1
    )

    for j in range(start_index, end):

        line = clean_line(lines[j])

        if not line:
            continue

        match = BILLED_QTY_RE.search(line)

        if match:
            qty = match.group(1)
            unit = match.group(2)

            return {
                "Billed qty": f"{qty} {unit}",
                "Billed Qty Value": clean_number(qty),
                "Billed Qty Unit": unit
            }

    return {
        "Billed qty": "",
        "Billed Qty Value": None,
        "Billed Qty Unit": ""
    }


def find_numeric_line(lines, start_index, max_lookahead=5):
    """
    Find the actual numeric charge line after a charge description.

    This is important because Opal's PDF extraction can produce:

        01.09.2026 ... FFS - Qty/Weight 50001003546705
        A
         1.635 TO 9.01 BL ...

    or:

        16.09.2026 ... Manual Price 24944
        QR26-1590
         1.000 FL 150.00 EA ...

    """

    end = min(
        len(lines),
        start_index + max_lookahead + 1
    )

    for j in range(start_index, end):

        line = clean_line(lines[j])

        if not line:
            continue

        # Do not accidentally consume another charge.
        if FUEL_RE.match(line):
            return None, None

        if is_date_start(line):
            return None, None

        # Numeric detail lines normally contain AUD.
        if "AUD" in line.upper():
            return j, line

        # Some PDF extraction variants may omit AUD.
        # Require several numeric tokens before accepting.
        numeric_count = len(
            re.findall(
                r"\b\d[\d,]*\.\d+\b",
                line
            )
        )

        if numeric_count >= 4:
            return j, line

    return None, None


# ============================================================
# MAIN CHARGE PARSERS
# ============================================================

def parse_qty_weight_numeric_line(line):
    """
    Parse:

    1.635 TO 9.01 BL 45.05 47.50 4.75 52.25 AUD

    or:

    1.000 FL 27.00 FL 27.00 28.50 2.85 31.35 AUD

    Structure:

        Qty
        Qty Unit
        Unit Price
        Price Unit
        Item Total
        Ex GST
        GST
        Inc GST
        AUD
    """

    line = clean_line(line)

    # Remove trailing PDF artefacts.
    line = re.sub(
        r"\s+A\s*$",
        "",
        line,
        flags=re.IGNORECASE
    )

    pattern = re.compile(
        r"^"
        r"([\d,]+\.\d+)\s+"       # qty
        r"([A-Za-z]+)\s+"         # qty unit
        r"([\d,]+\.\d+)\s+"       # unit price
        r"([A-Za-z]+)\s+"         # price unit
        r"([\d,]+\.\d+)\s+"       # item total
        r"([\d,]+\.\d+)\s+"       # ex GST
        r"([\d,]+\.\d+)\s+"       # GST
        r"([\d,]+\.\d+)"
        r"(?:\s+AUD)?$",
        re.IGNORECASE
    )

    match = pattern.match(line)

    if not match:
        return None

    (
        qty,
        qty_unit,
        unit_price,
        price_unit,
        item_total,
        ex_gst,
        gst,
        inc_gst
    ) = match.groups()

    return {
        "Qty.": f"{qty} {qty_unit}",
        "Qty Value": clean_number(qty),
        "Qty Unit": qty_unit,
        "Unit Price": f"{unit_price} {price_unit}",
        "Unit Price Value": clean_number(unit_price),
        "Price Unit": price_unit,
        "Item Total": money(item_total),
        "Amount excl. GST": money(ex_gst),
        "GST": money(gst),
        "Amount Incl. GST": money(inc_gst),
    }


def parse_load_numeric_line(line):
    """
    Parse:

        1.000 OT 240.00 253.30 25.33 278.63 AUD

    Structure:

        Qty
        Qty Unit
        Unit Price
        Ex GST
        GST
        Inc GST
        AUD
    """

    line = clean_line(line)

    line = re.sub(
        r"\s+A\s*$",
        "",
        line,
        flags=re.IGNORECASE
    )

    pattern = re.compile(
        r"^"
        r"([\d,]+\.\d+)\s+"       # qty
        r"([A-Za-z]+)\s+"         # qty unit
        r"([\d,]+\.\d+)\s+"       # unit price
        r"([\d,]+\.\d+)\s+"       # ex GST
        r"([\d,]+\.\d+)\s+"       # GST
        r"([\d,]+\.\d+)"
        r"(?:\s+AUD)?$",
        re.IGNORECASE
    )

    match = pattern.match(line)

    if not match:
        return None

    (
        qty,
        qty_unit,
        unit_price,
        ex_gst,
        gst,
        inc_gst
    ) = match.groups()

    return {
        "Qty.": f"{qty} {qty_unit}",
        "Qty Value": clean_number(qty),
        "Qty Unit": qty_unit,
        "Unit Price": clean_number(unit_price),
        "Price Unit": "",
        "Item Total": money(unit_price),
        "Amount excl. GST": money(ex_gst),
        "GST": money(gst),
        "Amount Incl. GST": money(inc_gst),
    }


# ============================================================
# FUEL LEVY PARSER
# ============================================================

def parse_fuel_numeric_line(line):
    """
    Examples:

        1.635 TO 0.49 BL 2.45

        1.000 FL 1.50 FL 1.50

        1.000 OT 13.30

    Returns the fuel levy amount.
    """

    line = clean_line(line)

    line = re.sub(
        r"\s+A\s*$",
        "",
        line,
        flags=re.IGNORECASE
    )

    # --------------------------------------------------------
    # Standard form with rate unit:
    #
    # 1.635 TO 0.49 BL 2.45
    # --------------------------------------------------------

    pattern_with_rate_unit = re.compile(
        r"^"
        r"([\d,]+\.\d+)\s+"
        r"([A-Za-z]+)\s+"
        r"([\d,]+\.\d+)\s+"
        r"([A-Za-z]+)\s+"
        r"([\d,]+\.\d+)"
        r"(?:\s+AUD)?$",
        re.IGNORECASE
    )

    match = pattern_with_rate_unit.match(line)

    if match:
        (
            qty,
            qty_unit,
            rate,
            rate_unit,
            amount
        ) = match.groups()

        return {
            "Qty.": f"{qty} {qty_unit}",
            "Qty Value": clean_number(qty),
            "Qty Unit": qty_unit,
            "Unit Price": f"{rate} {rate_unit}",
            "Unit Price Value": clean_number(rate),
            "Price Unit": rate_unit,
            "Item Total": money(amount),
            "Fuel Levy Amount": money(amount)
        }

    # --------------------------------------------------------
    # Load style:
    #
    # 1.000 OT 13.30
    # --------------------------------------------------------

    pattern_simple = re.compile(
        r"^"
        r"([\d,]+\.\d+)\s+"
        r"([A-Za-z]+)\s+"
        r"([\d,]+\.\d+)"
        r"(?:\s+AUD)?$",
        re.IGNORECASE
    )

    match = pattern_simple.match(line)

    if match:
        qty, qty_unit, amount = match.groups()

        return {
            "Qty.": f"{qty} {qty_unit}",
            "Qty Value": clean_number(qty),
            "Qty Unit": qty_unit,
            "Unit Price": "",
            "Unit Price Value": None,
            "Price Unit": "",
            "Item Total": money(amount),
            "Fuel Levy Amount": money(amount)
        }

    return None


# ============================================================
# MAIN CHARGE HEADER PARSER
# ============================================================

def parse_charge_header(line):
    """
    Parse the first part of an Opal charge.

    Example:

        01.09.2026 Old Corrugated Cartons
        FFS - Qty/Weight 50001003546705

    Returns:
        date
        description
        charge_type
        reference
    """

    date_match = re.match(
        rf"^({DATE_RE})\s+(.*)$",
        line
    )

    if not date_match:
        return None

    date = date_match.group(1)
    remainder = date_match.group(2).strip()

    # --------------------------------------------------------
    # FFS - Qty/Weight
    # --------------------------------------------------------

    marker = FFS_QTY_RE.search(remainder)

    if marker:

        description = remainder[:marker.start()].strip()
        after = remainder[marker.end():].strip()

        reference = ""

        if after:
            # First token is normally the reference.
            reference = after.split()[0]

        return {
            "Date": date,
            "Description": description,
            "Charge Type": "FFS - Qty/Weight",
            "Reference": reference,
            "Header Remainder": after
        }

    # --------------------------------------------------------
    # FFS - Load
    # --------------------------------------------------------

    marker = FFS_LOAD_RE.search(remainder)

    if marker:

        description = remainder[:marker.start()].strip()
        after = remainder[marker.end():].strip()

        reference = ""

        if after:
            reference = after.split()[0]

        return {
            "Date": date,
            "Description": description,
            "Charge Type": "FFS - Load",
            "Reference": reference,
            "Header Remainder": after
        }

    # --------------------------------------------------------
    # Manual Price on same line
    # --------------------------------------------------------

    marker = MANUAL_RE.search(remainder)

    if marker:

        description = remainder[:marker.start()].strip()
        after = remainder[marker.end():].strip()

        reference = ""

        if after:
            # Only use a token as reference when it looks like
            # an actual reference rather than ordinary text.
            first_token = after.split()[0]

            if re.match(
                r"^[A-Za-z0-9][A-Za-z0-9\-/]*$",
                first_token
            ):
                reference = first_token

        return {
            "Date": date,
            "Description": description,
            "Charge Type": "Manual Price",
            "Reference": reference,
            "Header Remainder": after
        }

    return None


# ============================================================
# MULTI-LINE MANUAL PRICE
# ============================================================

def parse_manual_multiline(lines, start_index):
    """
    Handles cases such as:

        18.09.2026 UNDERWEIGHT BINS AUGUST
        26
        Manual Price UNDERWEIGHT
        BINS AUGUST 26
        11.000 EA 200.00 EA 0.00 2,200.00 220.00 2,420.00 AUD

    """

    first_line = clean_line(lines[start_index])

    date_match = re.match(
        rf"^({DATE_RE})\s+(.*)$",
        first_line
    )

    if not date_match:
        return None, start_index

    date = date_match.group(1)
    first_text = date_match.group(2).strip()

    collected = [first_text]

    manual_index = None
    numeric_index = None

    # Look forward for Manual Price.
    end = min(
        len(lines),
        start_index + 5
    )

    for j in range(start_index + 1, end):

        candidate = clean_line(lines[j])

        if not candidate:
            continue

        if MANUAL_RE.search(candidate):
            manual_index = j
            collected.append(candidate)
            break

        # Stop if we hit a new date/charge.
        if is_date_start(candidate):
            break

    if manual_index is None:
        return None, start_index

    # Find numeric line after Manual Price.
    numeric_index, numeric_line = find_numeric_line(
        lines,
        manual_index + 1,
        max_lookahead=4
    )

    if numeric_index is None:
        return None, start_index

    numeric = parse_qty_weight_numeric_line(numeric_line)

    if not numeric:
        return None, start_index

    # Collect text between date and numeric line.
    text_parts = []

    for j in range(start_index, numeric_index):

        candidate = clean_line(lines[j])

        if not candidate:
            continue

        # Don't include standalone A.
        if candidate.upper() == "A":
            continue

        # Don't include numeric line.
        if j == numeric_index:
            continue

        text_parts.append(candidate)

    # Remove date from first element.
    if text_parts:

        first = text_parts[0]

        first = re.sub(
            rf"^{DATE_RE}\s*",
            "",
            first
        )

        text_parts[0] = first.strip()

    # Remove "Manual Price" marker from description.
    description_parts = []

    for part in text_parts:

        cleaned = MANUAL_RE.sub(
            "",
            part,
            count=1
        ).strip()

        if cleaned:
            description_parts.append(cleaned)

    description = " ".join(description_parts)

    # Try to identify a useful reference.
    reference = ""

    # Search the text for something resembling a reference.
    for part in description_parts:

        # Skip ordinary long words.
        tokens = part.split()

        for token in tokens:

            if re.fullmatch(
                r"[A-Z0-9][A-Z0-9\-/]{3,}",
                token,
                re.IGNORECASE
            ):
                # Don't treat normal words as reference.
                if token.upper() not in {
                    "UNDERWEIGHT",
                    "BINS",
                    "AUGUST",
                    "PRICE"
                }:
                    reference = token
                    break

        if reference:
            break

    billed = get_billed_qty(
        lines,
        numeric_index + 1,
        lookahead=3
    )

    row = {
        "Date": date,
        "Description": description,
        "Charge Type": "Manual Price",
        "Reference": reference,
        "Billed qty": billed["Billed qty"],
        **numeric,
    }

    return row, numeric_index


# ============================================================
# INVOICE PROCESSOR
# ============================================================

def process_pdf(file_stream):

    extracted_rows = []
    unmatched_lines = []

    customer_totals = []
    subtotal_records = []
    invoice_totals = []

    current_invoice = ""
    current_invoice_date = ""

    current_customer_code = ""
    current_customer_name = ""

    full_text_pages = []

    with pdfplumber.open(file_stream) as pdf:

        for page_num, page in enumerate(
            pdf.pages,
            start=1
        ):

            page_text = page.extract_text()

            if not page_text:
                continue

            full_text_pages.append(page_text)

            raw_lines = page_text.splitlines()

            lines = [
                clean_line(x)
                for x in raw_lines
            ]

            # ------------------------------------------------
            # Find invoice header on this page.
            # ------------------------------------------------

            page_invoice_match = INVOICE_RE.search(
                page_text
            )

            if page_invoice_match:
                current_invoice = page_invoice_match.group(1)

            page_date_match = INVOICE_DATE_RE.search(
                page_text
            )

            if page_date_match:
                current_invoice_date = page_date_match.group(1)

            # ------------------------------------------------
            # Page line parser
            # ------------------------------------------------

            i = 0

            while i < len(lines):

                line = clean_line(lines[i])

                if not line:
                    i += 1
                    continue

                # ============================================
                # Invoice number
                # ============================================

                invoice_match = INVOICE_RE.search(line)

                if invoice_match:
                    current_invoice = invoice_match.group(1)
                    i += 1
                    continue

                # ============================================
                # Customer total
                # ============================================

                customer_total_match = CUSTOMER_TOTAL_RE.match(
                    line
                )

                if customer_total_match:

                    (
                        customer_code,
                        customer_name,
                        ex_gst,
                        gst,
                        inc_gst
                    ) = customer_total_match.groups()

                    customer_totals.append({
                        "Invoice No.": current_invoice,
                        "Customer Code": customer_code,
                        "Customer": customer_name.strip(),
                        "Amount excl. GST": money(ex_gst),
                        "GST": money(gst),
                        "Amount Incl. GST": money(inc_gst),
                        "Source": "Customer Total"
                    })

                    i += 1
                    continue

                # ============================================
                # Customer header
                # ============================================

                if is_customer_line(line):

                    code, name = parse_customer_line(line)

                    if code and name:

                        current_customer_code = code
                        current_customer_name = name

                    i += 1
                    continue

                # ============================================
                # SUB-TOTAL
                # ============================================

                subtotal_match = SUBTOTAL_RE.search(line)

                if subtotal_match:

                    (
                        charge_code,
                        qty,
                        qty_unit,
                        ex_gst,
                        gst,
                        inc_gst
                    ) = subtotal_match.groups()

                    subtotal_records.append({
                        "Invoice No.": current_invoice,
                        "Customer Code": current_customer_code,
                        "Customer": current_customer_name,
                        "Charge Code": charge_code,
                        "Qty": f"{qty} {qty_unit}",
                        "Amount excl. GST": money(ex_gst),
                        "GST": money(gst),
                        "Amount Incl. GST": money(inc_gst),
                    })

                    i += 1
                    continue

                # ============================================
                # TOTAL PAYABLE
                # ============================================

                total_match = TOTAL_PAYABLE_RE.search(line)

                if total_match:

                    (
                        ex_gst,
                        gst,
                        inc_gst
                    ) = total_match.groups()

                    invoice_totals.append({
                        "Invoice No.": current_invoice,
                        "Amount excl. GST": money(ex_gst),
                        "GST": money(gst),
                        "Amount Incl. GST": money(inc_gst),
                    })

                    i += 1
                    continue

                # ============================================
                # FUEL LEVY
                # ============================================

                if FUEL_RE.match(line):

                    fuel_type = (
                        "Fuel Levy - Load"
                        if "LOAD" in line.upper()
                        else "Fuel Levy - Qty/Weight"
                    )

                    # Reference normally follows fuel levy.
                    after = re.sub(
                        r"^OPR\s+Fuel\s+Levy-[^ ]+\s*",
                        "",
                        line,
                        flags=re.IGNORECASE
                    )

                    # More reliable extraction of reference:
                    fuel_reference = ""

                    parts = line.split()

                    # Find token after Qty/Wt or Load.
                    for idx, token in enumerate(parts):

                        if token.lower() in {
                            "qty/wt",
                            "load"
                        }:

                            if idx + 1 < len(parts):
                                fuel_reference = parts[idx + 1]

                            break

                    # Numeric line may be next line because of
                    # PDF line wrapping.
                    numeric_index = i
                    numeric_line = None

                    # Check whether numbers are already on line.
                    after_marker_match = re.search(
                        r"(?:Qty/Wt|Load)\s+.+?\s+"
                        r"([\d,]+\.\d+)",
                        line,
                        re.IGNORECASE
                    )

                    if after_marker_match:
                        numeric_line = line[
                            after_marker_match.start():
                        ]

                    else:
                        numeric_index, numeric_line = find_numeric_line(
                            lines,
                            i + 1,
                            max_lookahead=3
                        )

                    fuel_numeric = None

                    if numeric_line:

                        if numeric_line == line:
                            # Remove text before first numeric sequence.
                            first_number = re.search(
                                r"\d[\d,]*\.\d+",
                                numeric_line
                            )

                            if first_number:
                                numeric_line_for_parse = (
                                    numeric_line[first_number.start():]
                                )
                            else:
                                numeric_line_for_parse = numeric_line
                        else:
                            numeric_line_for_parse = numeric_line

                        fuel_numeric = parse_fuel_numeric_line(
                            numeric_line_for_parse
                        )

                    if fuel_numeric:

                        billed = get_billed_qty(
                            lines,
                            (
                                numeric_index + 1
                                if numeric_index is not None
                                else i + 1
                            ),
                            lookahead=3
                        )

                        row = {
                            "Invoice No.": current_invoice,
                            "Customer Code": current_customer_code,
                            "Customer": current_customer_name,
                            "Date": "",
                            "Description": "OPR Fuel Levy",
                            "Charge Type": fuel_type,
                            "Reference": fuel_reference,
                            "Billed qty": billed["Billed qty"],
                            "Qty.": fuel_numeric["Qty."],
                            "Qty Value": fuel_numeric["Qty Value"],
                            "Qty Unit": fuel_numeric["Qty Unit"],
                            "Unit Price": fuel_numeric["Unit Price"],
                            "Unit Price Value": fuel_numeric[
                                "Unit Price Value"
                            ],
                            "Price Unit": fuel_numeric["Price Unit"],
                            "Item Total": fuel_numeric["Item Total"],
                            "Amount excl. GST": None,
                            "GST": None,
                            "Amount Incl. GST": None,
                            "Fuel Levy Amount": fuel_numeric[
                                "Fuel Levy Amount"
                            ],
                            "Is Fuel Levy": True,
                            "Source Page": page_num,
                        }

                        extracted_rows.append(row)

                        if numeric_index is not None and numeric_index > i:
                            i = numeric_index + 1
                        else:
                            i += 1

                        continue

                # ============================================
                # MAIN CHARGE
                # ============================================

                if is_date_start(line):

                    charge_header = parse_charge_header(line)

                    if charge_header:

                        numeric_index, numeric_line = find_numeric_line(
                            lines,
                            i + 1,
                            max_lookahead=5
                        )

                        if numeric_index is not None:

                            if (
                                charge_header["Charge Type"]
                                == "FFS - Load"
                            ):
                                numeric = parse_load_numeric_line(
                                    numeric_line
                                )
                            else:
                                numeric = parse_qty_weight_numeric_line(
                                    numeric_line
                                )

                            if numeric:

                                billed = get_billed_qty(
                                    lines,
                                    numeric_index + 1,
                                    lookahead=3
                                )

                                row = {
                                    "Invoice No.": current_invoice,
                                    "Customer Code": current_customer_code,
                                    "Customer": current_customer_name,
                                    "Date": charge_header["Date"],
                                    "Description": charge_header[
                                        "Description"
                                    ],
                                    "Charge Type": charge_header[
                                        "Charge Type"
                                    ],
                                    "Reference": charge_header[
                                        "Reference"
                                    ],
                                    "Billed qty": billed[
                                        "Billed qty"
                                    ],
                                    **numeric,
                                    "Fuel Levy Amount": None,
                                    "Is Fuel Levy": False,
                                    "Source Page": page_num,
                                }

                                extracted_rows.append(row)

                                i = numeric_index + 1
                                continue

                        # ------------------------------------------------
                        # If standard charge failed, mark for review.
                        # ------------------------------------------------

                        unmatched_lines.append({
                            "Page": page_num,
                            "Line No.": i + 1,
                            "Invoice No.": current_invoice,
                            "Customer Code": current_customer_code,
                            "Customer": current_customer_name,
                            "Line": line,
                            "Reason": (
                                "Charge detected but numeric "
                                "detail line could not be parsed"
                            )
                        })

                        i += 1
                        continue

                    # ------------------------------------------------
                    # Multi-line Manual Price
                    # ------------------------------------------------

                    if i + 1 < len(lines):

                        manual_row, manual_end = (
                            parse_manual_multiline(
                                lines,
                                i
                            )
                        )

                        if manual_row:

                            row = {
                                "Invoice No.": current_invoice,
                                "Customer Code": current_customer_code,
                                "Customer": current_customer_name,
                                **manual_row,
                                "Fuel Levy Amount": None,
                                "Is Fuel Levy": False,
                                "Source Page": page_num,
                            }

                            extracted_rows.append(row)

                            i = manual_end + 1
                            continue

                # ============================================
                # Standalone Manual Price line
                # ============================================

                if MANUAL_RE.search(line):

                    numeric_index, numeric_line = find_numeric_line(
                        lines,
                        i + 1,
                        max_lookahead=4
                    )

                    if numeric_index is not None:

                        numeric = parse_qty_weight_numeric_line(
                            numeric_line
                        )

                        if numeric:

                            description = MANUAL_RE.sub(
                                "",
                                line
                            ).strip()

                            billed = get_billed_qty(
                                lines,
                                numeric_index + 1,
                                lookahead=3
                            )

                            extracted_rows.append({
                                "Invoice No.": current_invoice,
                                "Customer Code": current_customer_code,
                                "Customer": current_customer_name,
                                "Date": "",
                                "Description": description,
                                "Charge Type": "Manual Price",
                                "Reference": "",
                                "Billed qty": billed[
                                    "Billed qty"
                                ],
                                **numeric,
                                "Fuel Levy Amount": None,
                                "Is Fuel Levy": False,
                                "Source Page": page_num,
                            })

                            i = numeric_index + 1
                            continue

                # ============================================
                # Potential unmatched invoice lines
                # ============================================

                if (
                    DATE_RE
                    and (
                        "AUD" in line.upper()
                        or FFS_QTY_RE.search(line)
                        or FFS_LOAD_RE.search(line)
                        or MANUAL_RE.search(line)
                    )
                ):

                    unmatched_lines.append({
                        "Page": page_num,
                        "Line No.": i + 1,
                        "Invoice No.": current_invoice,
                        "Customer Code": current_customer_code,
                        "Customer": current_customer_name,
                        "Line": line,
                        "Reason": "Potential invoice line not parsed"
                    })

                i += 1

    # ========================================================
    # BUILD DATAFRAMES
    # ========================================================

    invoice_df = pd.DataFrame(extracted_rows)

    if invoice_df.empty:
        invoice_df = pd.DataFrame(
            columns=[
                "Invoice No.",
                "Customer Code",
                "Customer",
                "Date",
                "Description",
                "Charge Type",
                "Reference",
                "Billed qty",
                "Qty.",
                "Qty Value",
                "Qty Unit",
                "Unit Price",
                "Unit Price Value",
                "Price Unit",
                "Item Total",
                "Amount excl. GST",
                "GST",
                "Amount Incl. GST",
                "Fuel Levy Amount",
                "Is Fuel Levy",
                "Source Page",
            ]
        )

    validation_df = build_validation(
        invoice_df,
        invoice_totals
    )

    customer_totals_df = build_customer_totals(
        invoice_df,
        customer_totals,
        subtotal_records
    )

    unmatched_df = pd.DataFrame(
        unmatched_lines
    )

    return (
        invoice_df,
        validation_df,
        customer_totals_df,
        unmatched_df
    )


# ============================================================
# VALIDATION
# ============================================================

def build_validation(
    invoice_df,
    printed_invoice_totals
):

    if invoice_df.empty:
        extracted = pd.DataFrame(
            columns=[
                "Invoice No.",
                "Extracted Ex GST",
                "Extracted GST",
                "Extracted Inc GST"
            ]
        )
    else:

        # IMPORTANT:
        #
        # Fuel levy rows are excluded because Opal has already
        # included the fuel levy inside the main charge's
        # Ex GST / GST / Inc GST values.
        #
        # Example:
        #
        # Main charge:
        #   Item Total = 45.05
        #   Ex GST     = 47.50
        #
        # Fuel levy:
        #   2.45
        #
        # 45.05 + 2.45 = 47.50
        #
        # Therefore adding fuel levy again would double count it.

        main_rows = invoice_df[
            invoice_df["Is Fuel Levy"] != True
        ].copy()

        extracted = (
            main_rows
            .groupby("Invoice No.", dropna=False)
            .agg(
                **{
                    "Extracted Ex GST": (
                        "Amount excl. GST",
                        "sum"
                    ),
                    "Extracted GST": (
                        "GST",
                        "sum"
                    ),
                    "Extracted Inc GST": (
                        "Amount Incl. GST",
                        "sum"
                    ),
                    "Main Charge Lines": (
                        "Description",
                        "count"
                    )
                }
            )
            .reset_index()
        )

    printed_df = pd.DataFrame(
        printed_invoice_totals
    )

    if printed_df.empty:

        result = extracted.copy()

        if not result.empty:

            result["Invoice Ex GST"] = None
            result["Invoice GST"] = None
            result["Invoice Inc GST"] = None
            result["Difference Ex GST"] = None
            result["Difference GST"] = None
            result["Difference Inc GST"] = None
            result["Validation"] = "No printed total found"

        return result

    printed_df = (
        printed_df
        .drop_duplicates(
            subset=["Invoice No."]
        )
    )

    result = pd.merge(
        extracted,
        printed_df,
        on="Invoice No.",
        how="outer"
    )

    result["Difference Ex GST"] = (
        result["Extracted Ex GST"].fillna(0)
        - result["Amount excl. GST"].fillna(0)
    ).round(2)

    result["Difference GST"] = (
        result["Extracted GST"].fillna(0)
        - result["GST"].fillna(0)
    ).round(2)

    result["Difference Inc GST"] = (
        result["Extracted Inc GST"].fillna(0)
        - result["Amount Incl. GST"].fillna(0)
    ).round(2)

    tolerance = 0.02

    result["Validation"] = result.apply(
        lambda row: (
            "PASS"
            if (
                abs(row["Difference Ex GST"]) <= tolerance
                and abs(row["Difference GST"]) <= tolerance
                and abs(row["Difference Inc GST"]) <= tolerance
            )
            else "FAIL"
        ),
        axis=1
    )

    result = result.rename(
        columns={
            "Amount excl. GST": "Invoice Ex GST",
            "GST": "Invoice GST",
            "Amount Incl. GST": "Invoice Inc GST"
        }
    )

    return result


# ============================================================
# CUSTOMER / SITE VALIDATION
# ============================================================

def build_customer_totals(
    invoice_df,
    printed_customer_totals,
    subtotal_records
):

    if invoice_df.empty:
        extracted = pd.DataFrame()
    else:

        main_rows = invoice_df[
            invoice_df["Is Fuel Levy"] != True
        ].copy()

        extracted = (
            main_rows
            .groupby(
                [
                    "Invoice No.",
                    "Customer Code",
                    "Customer"
                ],
                dropna=False
            )
            .agg(
                **{
                    "Extracted Ex GST": (
                        "Amount excl. GST",
                        "sum"
                    ),
                    "Extracted GST": (
                        "GST",
                        "sum"
                    ),
                    "Extracted Inc GST": (
                        "Amount Incl. GST",
                        "sum"
                    ),
                    "Main Charge Lines": (
                        "Description",
                        "count"
                    )
                }
            )
            .reset_index()
        )

    printed = pd.DataFrame(
        printed_customer_totals
    )

    if not printed.empty:

        printed = (
            printed
            .drop_duplicates(
                subset=[
                    "Invoice No.",
                    "Customer Code"
                ]
            )
            .rename(
                columns={
                    "Amount excl. GST": "Printed Ex GST",
                    "GST": "Printed GST",
                    "Amount Incl. GST": "Printed Inc GST"
                }
            )
        )

    else:

        printed = pd.DataFrame(
            columns=[
                "Invoice No.",
                "Customer Code",
                "Customer",
                "Printed Ex GST",
                "Printed GST",
                "Printed Inc GST"
            ]
        )

    if extracted.empty:
        result = printed.copy()

        if not result.empty:
            result["Difference Ex GST"] = None
            result["Difference GST"] = None
            result["Difference Inc GST"] = None
            result["Validation"] = "No extracted data"

        return result

    if printed.empty:

        result = extracted.copy()

        result["Printed Ex GST"] = None
        result["Printed GST"] = None
        result["Printed Inc GST"] = None
        result["Difference Ex GST"] = None
        result["Difference GST"] = None
        result["Difference Inc GST"] = None
        result["Validation"] = "No printed customer total"

        return result

    result = pd.merge(
        extracted,
        printed[
            [
                "Invoice No.",
                "Customer Code",
                "Customer",
                "Printed Ex GST",
                "Printed GST",
                "Printed Inc GST"
            ]
        ],
        on=[
            "Invoice No.",
            "Customer Code"
        ],
        how="outer",
        suffixes=("_Extracted", "_Printed")
    )

    # Prefer extracted customer name when available.
    result["Customer"] = (
        result["Customer_Extracted"]
        .fillna(result["Customer_Printed"])
    )

    result["Difference Ex GST"] = (
        result["Extracted Ex GST"].fillna(0)
        - result["Printed Ex GST"].fillna(0)
    ).round(2)

    result["Difference GST"] = (
        result["Extracted GST"].fillna(0)
        - result["Printed GST"].fillna(0)
    ).round(2)

    result["Difference Inc GST"] = (
        result["Extracted Inc GST"].fillna(0)
        - result["Printed Inc GST"].fillna(0)
    ).round(2)

    tolerance = 0.02

    result["Validation"] = result.apply(
        lambda row: (
            "PASS"
            if (
                abs(row["Difference Ex GST"]) <= tolerance
                and abs(row["Difference GST"]) <= tolerance
                and abs(row["Difference Inc GST"]) <= tolerance
            )
            else "FAIL"
        ),
        axis=1
    )

    # Remove duplicate name columns.
    for col in [
        "Customer_Extracted",
        "Customer_Printed"
    ]:
        if col in result.columns:
            result.drop(
                columns=[col],
                inplace=True
            )

    return result


# ============================================================
# EXCEL EXPORT
# ============================================================

def create_excel(
    invoice_df,
    validation_df,
    customer_totals_df,
    unmatched_df
):

    output = io.BytesIO()

    with pd.ExcelWriter(
        output,
        engine="openpyxl"
    ) as writer:

        invoice_df.to_excel(
            writer,
            sheet_name="Invoice Data",
            index=False
        )

        validation_df.to_excel(
            writer,
            sheet_name="Validation",
            index=False
        )

        customer_totals_df.to_excel(
            writer,
            sheet_name="Customer Totals",
            index=False
        )

        unmatched_df.to_excel(
            writer,
            sheet_name="Unmatched Lines",
            index=False
        )

        # ----------------------------------------------------
        # Formatting
        # ----------------------------------------------------

        workbook = writer.book

        for sheet_name in workbook.sheetnames:

            worksheet = workbook[sheet_name]

            worksheet.freeze_panes = "A2"

            for column_cells in worksheet.columns:

                max_length = 0

                column_letter = (
                    column_cells[0].column_letter
                )

                for cell in column_cells:

                    try:
                        value_length = len(
                            str(cell.value)
                        )
                    except Exception:
                        value_length = 0

                    max_length = max(
                        max_length,
                        value_length
                    )

                worksheet.column_dimensions[
                    column_letter
                ].width = min(
                    max(max_length + 2, 12),
                    45
                )

    output.seek(0)

    return output.getvalue()


# ============================================================
# STREAMLIT UI
# ============================================================

st.set_page_config(
    page_title="Opal Invoice PDF → Excel",
    page_icon="📄",
    layout="wide"
)

st.title(
    "📄 Opal Invoice PDF → Excel Extractor"
)

st.caption(
    "Extracts Opal Packaging / Opal Recycling invoice "
    "charges, fuel levies and validation totals."
)

uploaded_file = st.file_uploader(
    "Upload an Opal Invoice PDF",
    type=["pdf"]
)


if uploaded_file:

    st.divider()

    with st.spinner(
        "Processing Opal invoice PDF... please wait ⏳"
    ):

        file_stream = io.BytesIO(
            uploaded_file.read()
        )

        try:

            (
                invoice_df,
                validation_df,
                customer_totals_df,
                unmatched_df
            ) = process_pdf(
                file_stream
            )

        except Exception as e:

            st.error(
                f"❌ Error while processing PDF: {e}"
            )

            st.exception(e)

            st.stop()

    # ========================================================
    # SUMMARY
    # ========================================================

    total_rows = len(invoice_df)

    main_rows = (
        invoice_df[
            invoice_df["Is Fuel Levy"] != True
        ]
        if not invoice_df.empty
        else pd.DataFrame()
    )

    fuel_rows = (
        invoice_df[
            invoice_df["Is Fuel Levy"] == True
        ]
        if not invoice_df.empty
        else pd.DataFrame()
    )

    invoice_count = (
        invoice_df["Invoice No."]
        .nunique()
        if not invoice_df.empty
        else 0
    )

    validation_pass = (
        (
            validation_df["Validation"] == "PASS"
        ).sum()
        if not validation_df.empty
        and "Validation" in validation_df.columns
        else 0
    )

    validation_fail = (
        (
            validation_df["Validation"] == "FAIL"
        ).sum()
        if not validation_df.empty
        and "Validation" in validation_df.columns
        else 0
    )

    unmatched_count = len(
        unmatched_df
    )

    # ========================================================
    # METRICS
    # ========================================================

    col1, col2, col3, col4, col5 = st.columns(5)

    col1.metric(
        "Invoices",
        invoice_count
    )

    col2.metric(
        "Main Charges",
        len(main_rows)
    )

    col3.metric(
        "Fuel Levy Lines",
        len(fuel_rows)
    )

    col4.metric(
        "Validation PASS",
        validation_pass
    )

    col5.metric(
        "Unmatched",
        unmatched_count
    )

    # ========================================================
    # VALIDATION STATUS
    # ========================================================

    st.divider()

    if validation_fail == 0 and validation_pass > 0:

        st.success(
            f"✅ All {validation_pass} invoice(s) "
            "passed validation."
        )

    elif validation_fail > 0:

        st.error(
            f"⚠️ {validation_fail} invoice(s) "
            "failed validation."
        )

    else:

        st.warning(
            "⚠️ No invoice validation records were found."
        )

    # ========================================================
    # TABS
    # ========================================================

    (
        tab1,
        tab2,
        tab3,
        tab4
    ) = st.tabs(
        [
            "📄 Invoice Data",
            "✅ Validation",
            "🏢 Customer Totals",
            "⚠️ Unmatched Lines"
        ]
    )

    # ========================================================
    # TAB 1
    # ========================================================

    with tab1:

        st.subheader(
            "Extracted Invoice Data"
        )

        if not invoice_df.empty:

            st.dataframe(
                invoice_df,
                use_container_width=True,
                height=600
            )

        else:

            st.warning(
                "No invoice lines were extracted."
            )

    # ========================================================
    # TAB 2
    # ========================================================

    with tab2:

        st.subheader(
            "Invoice Validation"
        )

        if not validation_df.empty:

            st.dataframe(
                validation_df,
                use_container_width=True
            )

        else:

            st.info(
                "No validation data available."
            )

    # ========================================================
    # TAB 3
    # ========================================================

    with tab3:

        st.subheader(
            "Customer / Site Validation"
        )

        if not customer_totals_df.empty:

            st.dataframe(
                customer_totals_df,
                use_container_width=True,
                height=600
            )

        else:

            st.info(
                "No customer totals were found."
            )

    # ========================================================
    # TAB 4
    # ========================================================

    with tab4:

        st.subheader(
            "Unmatched / Review Lines"
        )

        if not unmatched_df.empty:

            st.warning(
                f"{len(unmatched_df)} line(s) require review."
            )

            st.dataframe(
                unmatched_df,
                use_container_width=True,
                height=600
            )

        else:

            st.success(
                "✅ No unmatched invoice lines."
            )

    # ========================================================
    # DOWNLOAD
    # ========================================================

    st.divider()

    excel_bytes = create_excel(
        invoice_df,
        validation_df,
        customer_totals_df,
        unmatched_df
    )

    invoice_numbers = (
        invoice_df["Invoice No."]
        .dropna()
        .astype(str)
        .unique()
        if not invoice_df.empty
        else []
    )

    if len(invoice_numbers) == 1:

        file_name = (
            f"Opal_Invoice_"
            f"{invoice_numbers[0]}.xlsx"
        )

    else:

        file_name = (
            "Opal_Invoice_Extract.xlsx"
        )

    st.download_button(
        label="📥 Download Excel",
        data=excel_bytes,
        file_name=file_name,
        mime=(
            "application/vnd.openxmlformats-"
            "officedocument.spreadsheetml.sheet"
        )
    )
