"""Bilingual field label resolution.

A ZATCA *Bayan* labels the declaration number `رقم البيان` or "Declaration No." or
"Bayan Number" depending on the issuing system. Writing a regex per layout does not
scale past the third broker; resolution goes through this alias table instead.

Labels are matched after `arabic.normalise`, so entries here are stored normalised too —
alef variants folded, tashkeel stripped. Add aliases as real layouts are encountered;
week 2 seeds the common ones only (see docs/ROADMAP_ARCHIVE.md week 3 checklist).
"""

from __future__ import annotations

from enum import StrEnum

from services.extraction.src.arabic import normalise


class Field(StrEnum):
    """Canonical field names, jurisdiction-neutral."""

    DECLARATION_NUMBER = "declaration_number"
    DECLARATION_DATE = "declaration_date"
    IMPORT_DATE = "import_date"
    DUTY_PAYMENT_DATE = "duty_payment_date"
    IMPORTER_NAME = "importer_name"
    IMPORTER_ID = "importer_id"
    HS_CODE = "hs_code"
    GOODS_DESCRIPTION = "goods_description"
    COUNTRY_OF_ORIGIN = "country_of_origin"
    QUANTITY = "quantity"
    UNIT_OF_MEASURE = "unit_of_measure"
    CUSTOMS_VALUE = "customs_value"
    DUTY_AMOUNT = "duty_amount"
    VAT_AMOUNT = "vat_amount"
    EXCISE_AMOUNT = "excise_amount"
    TOTAL_AMOUNT = "total_amount"
    PORT = "port"
    LINKED_IMPORT_DECLARATION = "linked_import_declaration"
    CONSIGNMENT_ID = "consignment_id"


# Raw aliases, pre-normalisation. Arabic entries are written as they appear on documents.
_RAW_ALIASES: dict[Field, tuple[str, ...]] = {
    Field.DECLARATION_NUMBER: (
        "رقم البيان",
        "رقم البيان الجمركي",
        "رقم الإقرار",
        "Declaration No",
        "Declaration Number",
        "Bayan No",
        "Bayan Number",
        "Customs Declaration Number",
        # US
        "Entry No",
        "Entry Number",
        "Entry Summary Number",
    ),
    Field.DECLARATION_DATE: (
        "تاريخ البيان",
        "تاريخ الإقرار",
        "Declaration Date",
        "Entry Summary Date",
    ),
    Field.IMPORT_DATE: (
        "تاريخ الاستيراد",
        "تاريخ الوصول",
        "Import Date",
        "Entry Date",
        "Arrival Date",
    ),
    Field.DUTY_PAYMENT_DATE: (
        "تاريخ السداد",
        "تاريخ دفع الرسوم",
        "Payment Date",
        "Duty Payment Date",
    ),
    Field.IMPORTER_NAME: (
        "اسم المستورد",
        "المستورد",
        "Importer",
        "Importer Name",
        "Consignee",
    ),
    Field.IMPORTER_ID: (
        "الرقم الضريبي",
        "رقم السجل التجاري",
        "VAT Number",
        "Tax Identification Number",
        "Importer Number",
        "CR Number",
    ),
    Field.HS_CODE: (
        "البند الجمركي",
        "رمز النظام المنسق",
        "التعريفة الجمركية",
        "HS Code",
        "HS",
        "Tariff Code",
        "HTS",
        "HTSUS Number",
    ),
    Field.GOODS_DESCRIPTION: (
        "وصف البضاعة",
        "بيان البضاعة",
        "Goods Description",
        "Description of Goods",
        "Description of Merchandise",
    ),
    Field.COUNTRY_OF_ORIGIN: (
        "بلد المنشأ",
        "المنشأ",
        "Country of Origin",
        "Origin",
    ),
    Field.QUANTITY: ("الكمية", "العدد", "Quantity", "Qty"),
    Field.UNIT_OF_MEASURE: ("الوحدة", "وحدة القياس", "Unit", "UOM", "Unit of Measure"),
    Field.CUSTOMS_VALUE: (
        "القيمة الجمركية",
        "قيمة البضاعة",
        "Customs Value",
        "Entered Value",
        "CIF Value",
    ),
    Field.DUTY_AMOUNT: (
        "الرسوم الجمركية",
        "قيمة الرسوم",
        "Customs Duty",
        "Duty",
        "Duty Amount",
    ),
    Field.VAT_AMOUNT: (
        "ضريبة القيمة المضافة",
        "الضريبة المضافة",
        "VAT",
        "VAT Amount",
        "Value Added Tax",
    ),
    Field.EXCISE_AMOUNT: (
        "الضريبة الانتقائية",
        "Excise",
        "Excise Tax",
    ),
    Field.TOTAL_AMOUNT: ("الإجمالي", "المجموع", "Total", "Total Amount"),
    Field.PORT: ("المنفذ", "ميناء الوصول", "Port", "Port of Entry", "Port of Arrival"),
    Field.LINKED_IMPORT_DECLARATION: (
        "رقم بيان الاستيراد",
        "رقم البيان الأصلي",
        "البيان المرتبط",
        "Original Declaration Number",
        "Linked Import Declaration",
        "Import Declaration No",
    ),
    Field.CONSIGNMENT_ID: (
        "رقم الإرسالية",
        "رقم الشحنة",
        "Consignment No",
        "Shipment Number",
    ),
}


def _build_index() -> dict[str, Field]:
    index: dict[str, Field] = {}
    for field, aliases in _RAW_ALIASES.items():
        for alias in aliases:
            # Surrounding punctuation is layout noise, not part of the label.
            key = normalise(alias).casefold().strip(":.-# 	")
            index[key] = field
    return index


_INDEX: dict[str, Field] = _build_index()


def _key(label: str) -> str:
    return normalise(label).casefold().strip(":.-# 	")


def resolve(label: str) -> Field | None:
    """Map a raw document label to a canonical field, or None if unrecognised.

    Unrecognised labels are not an error — they are the signal that a new layout has
    appeared, and the caller records them for alias-table expansion rather than guessing.
    """
    return _INDEX.get(_key(label))


def specificity(label: str) -> int:
    """How specific a label is, for disambiguating competing matches on one document.

    A CBP 7501 carries both "Duty" and "Customs Duty"; both resolve to DUTY_AMOUNT, and
    they hold different numbers. Taking whichever appears first picks the wrong one
    roughly half the time. Longer labels are more specific, so they win.
    """
    return len(_key(label))


def known_aliases(field: Field) -> tuple[str, ...]:
    return _RAW_ALIASES[field]
