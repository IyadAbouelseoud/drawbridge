"""Spatial templates — the step that turns geometry cells into named fields.

Week 10 stopped deliberately short of this. `geometry.extract_table` returns cells with
their boxes and says nothing about which cell is the duty and which is the VAT, because
inferring that from position alone would put a layout guess between an Arabic table and a
figure that ends up on a refund claim.

A template is that missing piece, supplied rather than guessed: a declaration that on
*this* form, the column a reader meets fourth carries the declared value, and that its
heading reads القيمة. Both halves matter, and the second is what makes the first safe.

**The index is a default; the heading is the authority.** When a header row is present
`bind` locates each field by its heading text and reports the binding it actually used.
A form revision that inserts a column shifts every index after it, and a template that
trusted its indices would keep parsing — assigning quantities to the value field and
producing a claim that is arithmetically consistent and wrong. Matching on the heading
turns that into a refusal.

**Coercion failures are reported, not swallowed and not raised.** A cell that should hold
an amount and holds a dash is a fact about the document. `TemplateResult.issues` carries
it so the caller can route the declaration to the review queue, which is where a document
the machine cannot read belongs — see `services/api/src/routes/triage.py`.

The fields a template names are the *document's* vocabulary, not the schema's: a Bayan
column is headed الرسوم and means `duty_amount`, which reaches `EntryLine` as `duty_paid`.
`ColumnSpec.maps_to` records the translation instead of pretending the two are one word,
and `TemplateResult.spans_for` emits provenance keyed by the schema name so the boxes land
where `EntryLine._figures_carry_their_boxes` looks for them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from drawbridge_schemas.provenance import DocumentKind, Language, ProvenanceSpan
from services.extraction.src.arabic import normalise_digits
from services.extraction.src.native import parse_amount

if TYPE_CHECKING:
    from uuid import UUID

    from services.extraction.src.geometry import Cell, TableRow

# A heading matches when its non-letter noise is gone and the remainder is equal. Real
# forms pad headings with parentheses, colons and line numbers; none of that changes
# which column it is.
_HEADING_NOISE = re.compile(r"[\s:()\[\]/\\.،,\-_*#0-9٠-٩]+")


class FieldKind(StrEnum):
    """What kind of value a column holds, and therefore how it is coerced.

    Not a Python type: `AMOUNT` and `QUANTITY` are both Decimal and differ in what a
    failure means. A quantity of zero is a line that shipped nothing; an amount of zero
    is a line that paid no duty, and only one of those is worth a review.
    """

    TEXT = "text"
    AMOUNT = "amount"
    QUANTITY = "quantity"
    INTEGER = "integer"
    HS_CODE = "hs_code"
    DECLARATION_NUMBER = "declaration_number"


class TemplateError(RuntimeError):
    """The document does not have the shape the template describes."""


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    """One column: what it is called here, what it is called downstream, how it reads."""

    field: str
    kind: FieldKind
    column: int
    """Index in reading order — 0 is the rightmost cell of an RTL row."""

    headings: tuple[str, ...] = ()
    """Accepted heading texts. Empty means the column can only be found by index."""

    maps_to: str | None = None
    """The schema field this becomes, where the two vocabularies differ."""

    required: bool = True

    @property
    def target(self) -> str:
        return self.maps_to or self.field


@dataclass(frozen=True, slots=True)
class TableTemplate:
    """A form's line table, described well enough to read one without guessing."""

    name: str
    document_kind: DocumentKind
    language: Language
    columns: tuple[ColumnSpec, ...]
    has_header_row: bool = True

    def spec(self, field_name: str) -> ColumnSpec:
        for column in self.columns:
            if column.field == field_name:
                return column
        raise KeyError(f"{self.name} declares no field named {field_name!r}")


@dataclass(frozen=True, slots=True)
class TypedCell:
    """A cell that now knows what it is, and still knows where it came from."""

    field: str
    target: str
    kind: FieldKind
    raw: str
    value: Decimal | str
    cell: Cell

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return self.cell.bbox

    @property
    def decimal(self) -> Decimal:
        """The value as a Decimal, for the numeric kinds. Raises otherwise."""
        if not isinstance(self.value, Decimal):
            raise TypeError(f"{self.field} is {self.kind}, not a number")
        return self.value


@dataclass(frozen=True, slots=True)
class TypedRow:
    index: int
    fields: dict[str, TypedCell]

    def __getitem__(self, field_name: str) -> TypedCell:
        return self.fields[field_name]

    def get(self, field_name: str) -> TypedCell | None:
        return self.fields.get(field_name)


@dataclass(frozen=True, slots=True)
class TemplateResult:
    """What a template made of a page, including what it could not make."""

    template: str
    binding: dict[str, int]
    """Field name to the column index actually used — the heading's answer, not the
    template's assumption, whenever a header row was there to ask."""

    rows: list[TypedRow] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.issues

    def spans_for(
        self,
        row: TypedRow,
        *,
        document_id: UUID,
        document_sha256: str,
        extractor: str = "geometry-template",
    ) -> dict[str, ProvenanceSpan]:
        """Provenance for one row's numeric fields, keyed by their schema names.

        Only the numeric kinds. `EntryLine` requires a box behind every *figure* it
        states, and a description is not a figure — attaching a span to it would suggest
        a guarantee the validator does not make and does not check.
        """
        spans: dict[str, ProvenanceSpan] = {}
        for typed in row.fields.values():
            if typed.kind not in {FieldKind.AMOUNT, FieldKind.QUANTITY}:
                continue
            x0, y0, x1, y1 = typed.bbox
            spans[typed.target] = ProvenanceSpan(
                document_id=document_id,
                document_sha256=document_sha256,
                page=typed.cell.page,
                x0=x0,
                y0=y0,
                x1=x1,
                y1=y1,
                field_path=f"{self.template}.{typed.field}",
                raw_text=typed.raw,
                language=Language(typed.cell.language),
                extractor=extractor,
            )
        return spans


# --------------------------------------------------------------------------------------
# Binding columns to headings
# --------------------------------------------------------------------------------------


def _normalise_heading(text: str) -> str:
    return _HEADING_NOISE.sub("", text)


def bind(template: TableTemplate, header: TableRow | None) -> dict[str, int]:
    """Resolve each field to the column index it occupies on *this* page.

    With no header row the template's own indices are all there is, and the caller has
    accepted that risk by using a template on a page whose table has no headings. With
    one, every field that declares headings must be found among them: a column the
    template expects and the form does not have is a `TemplateError`, because the
    alternative is reading the next column along and calling it duty.
    """
    if header is None or not template.has_header_row:
        return {column.field: column.column for column in template.columns}

    seen = {_normalise_heading(cell.text): cell.column for cell in header.cells}
    binding: dict[str, int] = {}
    missing: list[str] = []

    for column in template.columns:
        if not column.headings:
            binding[column.field] = column.column
            continue
        for heading in column.headings:
            found = seen.get(_normalise_heading(heading))
            if found is not None:
                binding[column.field] = found
                break
        else:
            if column.required:
                missing.append(f"{column.field} (expected heading {column.headings[0]!r})")

    if missing:
        raise TemplateError(
            f"{template.name}: no column on this page carries the heading for " + ", ".join(missing)
        )
    return binding


# --------------------------------------------------------------------------------------
# Coercion
# --------------------------------------------------------------------------------------


def _coerce(spec: ColumnSpec, raw: str) -> Decimal | str:
    """Turn a cell's text into the value its kind promises, or raise `ValueError`.

    Digits are normalised first in every branch. Geometry already does it during
    reconstruction, but a template is also applied to text that arrived some other way,
    and an Arabic-Indic digit reaching `Decimal` raises where a `.` versus `٫` mix-up
    silently truncates.
    """
    text = normalise_digits(raw).strip()
    if not text:
        raise ValueError("empty")

    match spec.kind:
        case FieldKind.TEXT:
            return text
        case FieldKind.AMOUNT | FieldKind.QUANTITY:
            # `parse_amount` returns None rather than raising, because on its own path an
            # unparseable amount is a review signal. Here it is a broken column, so the
            # None is promoted to the error the caller collects.
            amount = parse_amount(text)
            if amount is None:
                raise ValueError(f"not a number: {text!r}")
            return amount
        case FieldKind.INTEGER:
            digits = text.replace(",", "")
            if not digits.isdigit():
                raise ValueError(f"not an integer: {text!r}")
            return Decimal(digits)
        case FieldKind.HS_CODE:
            digits = re.sub(r"\D", "", text)
            if not 6 <= len(digits) <= 12:
                raise ValueError(f"not an HS code: {text!r}")
            return digits
        case FieldKind.DECLARATION_NUMBER:
            return text.replace(" ", "")


def apply_template(template: TableTemplate, rows: list[TableRow]) -> TemplateResult:
    """Type every line row of a page against a template.

    The header row is consumed for binding and does not appear in the result. A page with
    a header and no line rows returns an empty result rather than an error: a declaration
    whose table continues overleaf is a real document, and the caller assembling pages is
    the only thing that knows whether that is a problem.
    """
    if not rows:
        return TemplateResult(template=template.name, binding={}, issues=["page has no table"])

    header = rows[0] if template.has_header_row else None
    binding = bind(template, header)
    body = rows[1:] if template.has_header_row else rows

    typed_rows: list[TypedRow] = []
    issues: list[str] = []

    for row in body:
        by_column = {cell.column: cell for cell in row.cells}
        fields: dict[str, TypedCell] = {}

        for spec in template.columns:
            cell = by_column.get(binding[spec.field])
            if cell is None:
                if spec.required:
                    issues.append(f"row {row.index}: {spec.field} has no cell")
                continue
            try:
                value = _coerce(spec, cell.text)
            except ValueError as exc:
                issues.append(f"row {row.index}: {spec.field} — {exc}")
                continue
            fields[spec.field] = TypedCell(
                field=spec.field,
                target=spec.target,
                kind=spec.kind,
                raw=cell.text,
                value=value,
                cell=cell,
            )

        if fields:
            typed_rows.append(TypedRow(index=len(typed_rows), fields=fields))

    return TemplateResult(template=template.name, binding=binding, rows=typed_rows, issues=issues)


# --------------------------------------------------------------------------------------
# The mock ZATCA declaration
# --------------------------------------------------------------------------------------

# A standard *Bayan* line table. Columns are numbered in reading order, so 0 is the
# rightmost on the page — which is also the order a Saudi form numbers its own columns,
# and the reason `geometry.extract_table` emits them that way rather than left to right.
#
# Marked a mock because it is: the column set and the headings are drawn from the ZATCA
# declaration's published structure (docs/COMPLIANCE-GCC.md §5) rather than measured off a
# scanned form, and the real one carries origin, weight and VAT columns this does not.
# Sourcing an unredacted *Bayan* is a broker-access problem, not a code one — it sits in
# the roadmap's external-acquisition bucket alongside the Fasah credentials.
BAYAN_LINE_TABLE = TableTemplate(
    name="zatca-bayan-lines-mock-v1",
    document_kind=DocumentKind.ZATCA_BAYAN,
    language=Language.MIXED,
    columns=(
        ColumnSpec(
            field="line_number",
            kind=FieldKind.INTEGER,
            column=0,
            headings=("البند", "رقم البند"),
            maps_to="line_number",
        ),
        ColumnSpec(
            field="hs_code",
            kind=FieldKind.HS_CODE,
            column=1,
            headings=("الرمز", "رمز النظام المنسق"),
            maps_to="hts_code",
        ),
        ColumnSpec(
            field="description",
            kind=FieldKind.TEXT,
            column=2,
            headings=("البيان", "وصف البضاعة"),
        ),
        ColumnSpec(
            field="quantity",
            kind=FieldKind.QUANTITY,
            column=3,
            headings=("الكمية",),
        ),
        ColumnSpec(
            field="declared_value",
            kind=FieldKind.AMOUNT,
            column=4,
            headings=("القيمة", "القيمة الإجمالية"),
            maps_to="entered_value",
        ),
        ColumnSpec(
            field="duty_amount",
            kind=FieldKind.AMOUNT,
            column=5,
            headings=("الرسوم", "الرسوم الجمركية"),
            maps_to="duty_paid",
        ),
    ),
)

TEMPLATES: dict[str, TableTemplate] = {BAYAN_LINE_TABLE.name: BAYAN_LINE_TABLE}


def template_for(document_kind: DocumentKind) -> TableTemplate | None:
    """The template registered for a document class, if there is one.

    Returns `None` rather than raising: most document kinds have no line table, and a
    caller that walks a mixed evidence bundle should not have to know which.
    """
    for template in TEMPLATES.values():
        if template.document_kind is document_kind:
            return template
    return None
