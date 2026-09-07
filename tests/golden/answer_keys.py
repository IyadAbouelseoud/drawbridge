"""Hand-verified golden answer keys.

These are *text-level* fixtures: the exact lines a document yields, paired with the field
values that must be extracted from them. Every expected value is derived by hand from the
source lines, never by running the extractor and pasting its output.

Why text and not PDFs. Generating an Arabic PDF through PyMuPDF's text shaper and then
asserting on what comes back tests MuPDF's bidi implementation, not Drawbridge's — and
MuPDF demonstrably corrupts it, reordering the Arabic decimal separator so `٤٦٨٧٥٫٠٠`
comes back as `6487.500`. Pinning the extractor against a generator that mangles its own
input pins the wrong thing. PDF-level fixtures still exist (`conftest.py`) but assert only
what they can honestly carry: whether `assess` routes a document to the native path or to
OCR.

When the real scanned corpus lands in week 3, these line lists are replaced with lines
captured from genuine documents. The test structure does not change.
"""

from __future__ import annotations

from decimal import Decimal

from services.extraction.src.field_aliases import Field

# --------------------------------------------------------------------------------------
# ZATCA Bayan — logical order
#
# What a conforming RTL PDF generator stores: Arabic labels in reading order, amounts in
# Arabic-Indic numerals with U+066B as the decimal separator and U+066C for thousands.
# --------------------------------------------------------------------------------------

BAYAN_LOGICAL: list[str] = [
    "الهيئة العامة للزكاة والضريبة والجمارك",
    "Zakat, Tax and Customs Authority",
    "رقم البيان: 20240115447821",
    "تاريخ البيان: 2024-01-15",
    "تاريخ السداد: 2024-02-08",
    "المنفذ: Jeddah Islamic Port",
    "اسم المستورد: Al Faisaliah Trading Co",
    "الرقم الضريبي: 300012345600003",
    "بلد المنشأ: CN",
    "البند الجمركي: 8471300000",
    "وصف البضاعة: Portable data processing machines",
    "الكمية: ١٢٠٠",
    "الوحدة: PCE",
    "القيمة الجمركية: ٩٣٧٥٠٠٫٠٠",
    "الرسوم الجمركية: ٤٦٨٧٥٫٠٠",
    "ضريبة القيمة المضافة: ١٤٧٦٥٦٫٢٥",
    "الإجمالي: ١٬١٣٢٬٠٣١٫٢٥",
]

# --------------------------------------------------------------------------------------
# ZATCA Bayan — visual order
#
# What a non-conforming generator stores, and what MuPDF produces: each Arabic run
# reversed, with its trailing colon migrated to the front. Same document, same answer key.
# If `to_logical_order` regresses, these diverge from BAYAN_LOGICAL and the test fails.
# --------------------------------------------------------------------------------------

BAYAN_VISUAL: list[str] = [
    "كرامجلاو ةبيرضلاو ةاكزلل ةماعلا ةئيهلا",
    "Zakat, Tax and Customs Authority",
    ":نايبلا مقر 20240115447821",
    ":نايبلا خيرات 2024-01-15",
    ":دادسلا خيرات 2024-02-08",
    ":ذفنملا Jeddah Islamic Port",
    ":دروتسملا مسا Al Faisaliah Trading Co",
    ":يبيرضلا مقرلا 300012345600003",
    ":أشنملا دلب CN",
    ":يكرمجلا دنبلا 8471300000",
    ":ةعاضبلا فصو Portable data processing machines",
    ":ةيمكلا ١٢٠٠",
    ":ةدحولا PCE",
    ":ةيكرمجلا ةميقلا ٩٣٧٥٠٠٫٠٠",
    ":ةيكرمجلا موسرلا ٤٦٨٧٥٫٠٠",
    ":ةفاضملا ةميقلا ةبيرض ١٤٧٦٥٦٫٢٥",
    ":يلامجإلا ١٬١٣٢٬٠٣١٫٢٥",
]

# --------------------------------------------------------------------------------------
# Bayan answer key
#
# Hand-derived arithmetic, checked independently of the code:
#
#   customs value                 937,500.00 SAR
#   duty        937500.00 x 0.05 = 46,875.00 SAR   (5% GCC common external tariff)
#   VAT taxable 937500 + 46875   = 984,375.00 SAR
#   VAT         984375.00 x 0.15 = 147,656.25 SAR  (KSA standard rate)
#   total       937500 + 46875 + 147656.25
#                                = 1,132,031.25 SAR
#
# Recoverable base under KSA_PROFILE is duty alone: 46,875.00. VAT is excluded — it is
# recovered through the VAT return as input tax, not through an Art. 97 drawback claim
# (docs/COMPLIANCE-GCC.md §2.4). At the GCC 100% rate the refund equals the duty.
# --------------------------------------------------------------------------------------

BAYAN_EXPECTED: dict[Field, str] = {
    Field.DECLARATION_NUMBER: "20240115447821",
    Field.DECLARATION_DATE: "2024-01-15",
    Field.DUTY_PAYMENT_DATE: "2024-02-08",
    Field.PORT: "Jeddah Islamic Port",
    Field.IMPORTER_NAME: "Al Faisaliah Trading Co",
    Field.IMPORTER_ID: "300012345600003",
    Field.COUNTRY_OF_ORIGIN: "CN",
    Field.HS_CODE: "8471300000",
    Field.GOODS_DESCRIPTION: "Portable data processing machines",
    Field.QUANTITY: "1200",
    Field.UNIT_OF_MEASURE: "PCE",
    Field.CUSTOMS_VALUE: "937500.00",
    Field.DUTY_AMOUNT: "46875.00",
    Field.VAT_AMOUNT: "147656.25",
    Field.TOTAL_AMOUNT: "1,132,031.25",
}

BAYAN_CUSTOMS_VALUE = Decimal("937500.00")
BAYAN_DUTY = Decimal("46875.00")
BAYAN_VAT = Decimal("147656.25")
BAYAN_TOTAL = Decimal("1132031.25")

# The GCC clock runs from duty payment, not from the declaration. These are 24 days apart
# on this document precisely because ZATCA permits postponement up to 30 days.
BAYAN_DECLARATION_DATE = "2024-01-15"
BAYAN_DUTY_PAYMENT_DATE = "2024-02-08"


# --------------------------------------------------------------------------------------
# CBP 7501
#
# Post-2024 shape: no ad valorem duty, the whole burden in Section 301.
#
#   entered value                250,000.00 USD
#   Section 301  250000 x 0.25 =  62,500.00 USD
#   MPF          250000 x 0.003464 = 866.00 -> capped, shown as 864.00
#   total                        313,364.00 USD  (250000 + 62500 + 864)
#
# The document carries both "Duty: 0.00" and "Customs Duty: 62500.00". Both labels
# resolve to DUTY_AMOUNT. Taking whichever appears first files 0.00 — the more specific
# label must win.
# --------------------------------------------------------------------------------------

CBP_7501_LINES: list[str] = [
    "DEPARTMENT OF HOMELAND SECURITY",
    "U.S. Customs and Border Protection",
    "ENTRY SUMMARY",
    "Entry Number: ABC-1234567-8",
    "Entry Summary Date: 2023-03-20",
    "Import Date: 2023-03-14",
    "Port of Entry: 2704",
    "Country of Origin: CN",
    "HTSUS Number: 8471300100",
    "Description of Merchandise: Portable automatic data processing machines",
    "Quantity: 1000",
    "Unit: NO",
    "Entered Value: 250000.00",
    "Duty: 0.00",
    "Customs Duty: 62500.00",
    "Total: 313364.00",
]

CBP_7501_EXPECTED: dict[Field, str] = {
    Field.DECLARATION_NUMBER: "ABC-1234567-8",
    Field.DECLARATION_DATE: "2023-03-20",
    Field.IMPORT_DATE: "2023-03-14",
    Field.PORT: "2704",
    Field.COUNTRY_OF_ORIGIN: "CN",
    Field.HS_CODE: "8471300100",
    Field.GOODS_DESCRIPTION: "Portable automatic data processing machines",
    Field.QUANTITY: "1000",
    Field.UNIT_OF_MEASURE: "NO",
    Field.CUSTOMS_VALUE: "250000.00",
    # The specific label wins over the bare "Duty: 0.00".
    Field.DUTY_AMOUNT: "62500.00",
    Field.TOTAL_AMOUNT: "313364.00",
}

CBP_ENTERED_VALUE = Decimal("250000.00")
CBP_SECTION_301 = Decimal("62500.00")
CBP_MPF = Decimal("864.00")
CBP_TOTAL = Decimal("313364.00")
