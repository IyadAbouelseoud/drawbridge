"""PDF construction for filing packets, over pypdf.

Two modes, and which one applies depends on something outside this repository:

**Template fill.** CBP publishes 7551 and 7552 as fillable AcroForm PDFs. Where a tenant
has the official template on file, `fill_template` writes the values into its existing
fields and changes nothing else. That is the correct output — it is literally CBP's form.

**Standalone render.** The official templates are CBP-published artefacts and are not
redistributed in this repository. Without one, `Document` renders the same field set as a
paginated AcroForm document: same field names, same values, machine-readable by the same
`get_fields()` call. It is a faithful transcription rather than a facsimile, and it says so
on its face — a packet that quietly *looked* like CBP's form while not being it would be
worse than one that does not pretend.

Values live in real AcroForm fields with generated appearance streams, not as flat drawn
text. A filer opening the packet can correct a figure in place, and a downstream reader can
pull the values back out without parsing a layout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# US Letter, in points. CBP forms are Letter; nothing here assumes it beyond the default.
PAGE_WIDTH = 612.0
PAGE_HEIGHT = 792.0
MARGIN = 48.0

BODY_SIZE = 9.0
LABEL_SIZE = 7.5
HEADING_SIZE = 13.0
SECTION_SIZE = 10.0
LINE_HEIGHT = 12.0

# Adobe's Helvetica AFM widths, in 1/1000 em, for the printable ASCII range 32-126. Needed
# because a value that overruns its box on a customs form is a defect, not a cosmetic
# issue — the reader sees a truncated entry number and the claim comes back.
_HELVETICA_WIDTHS = (
    "278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 278 278 584 584 584 556 "
    "1015 667 667 722 722 667 611 778 722 278 500 667 556 833 722 778 "
    "667 778 722 667 611 722 667 944 667 667 611 278 278 278 469 556 "
    "333 556 556 500 556 556 278 556 556 222 222 500 222 833 556 556 "
    "556 556 333 500 278 556 500 722 500 500 500 334 260 334 584"
)
_WIDTHS = [int(w) for w in _HELVETICA_WIDTHS.split()]

# Helvetica-Bold runs wider than Helvetica at the same size. Measuring bold text against
# the regular table would under-estimate and overflow, so the regular width is inflated.
# Deliberately generous: an early wrap is invisible, an overrun is not.
_BOLD_FACTOR = 1.09


def text_width(value: str, size: float, *, bold: bool = False) -> float:
    """Rendered width in points.

    Characters outside the measured ASCII range fall back to the width of a lowercase
    'n', which is mid-table — the alternative, treating them as zero-width, is what makes
    an accented name silently overrun its box.
    """
    total = 0
    fallback = _WIDTHS[ord("n") - 32]
    for char in value:
        index = ord(char) - 32
        total += _WIDTHS[index] if 0 <= index < len(_WIDTHS) else fallback
    width = total * size / 1000.0
    return width * _BOLD_FACTOR if bold else width


def truncate(value: str, max_width: float, size: float, *, bold: bool = False) -> str:
    """Fit a value to a box, marking the cut.

    An ellipsis rather than a silent chop: a truncated description that looks complete is
    a misdescription of the goods, while one that visibly ends in '…' is a prompt to open
    the underlying record.
    """
    if text_width(value, size, bold=bold) <= max_width:
        return value
    ellipsis = "..."
    budget = max_width - text_width(ellipsis, size, bold=bold)
    out: list[str] = []
    used = 0.0
    for char in value:
        char_width = text_width(char, size, bold=bold)
        if used + char_width > budget:
            break
        out.append(char)
        used += char_width
    return "".join(out) + ellipsis


def escape(value: str) -> bytes:
    """Encode a string as a PDF literal, WinAnsi, with the three reserved bytes escaped.

    Characters WinAnsi cannot represent become '?'. This path renders Latin-script content
    only; the KSA output is JSON, where Arabic survives intact, which is why the Arabic
    never has to come through here.
    """
    encoded = value.encode("cp1252", errors="replace")
    for target, replacement in ((b"\\", b"\\\\"), (b"(", b"\\("), (b")", b"\\)")):
        encoded = encoded.replace(target, replacement)
    return encoded


@dataclass(frozen=True, slots=True)
class Field:
    """One labelled value on the form.

    `name` is the AcroForm field name and is the stable contract: a consumer reads values
    by name, never by position, so re-laying out a page cannot change what a field means.
    """

    name: str
    label: str
    value: str
    width: float = 1.0
    """Fraction of the content width this field occupies. Fields sharing a row sum to 1."""


@dataclass(frozen=True, slots=True)
class Section:
    """A titled group of fields, laid out in rows."""

    title: str
    rows: Sequence[Sequence[Field]]
    note: str | None = None


@dataclass(slots=True)
class _Page:
    """Accumulated drawing operations and field widgets for one page.

    A widget is held beside its appearance stream rather than inside it: a
    `DictionaryObject` accepts only PDF names as keys, so there is nowhere in the widget
    to stash the stream until the writer exists to make it an indirect reference.
    """

    operations: list[bytes] = field(default_factory=list)
    widgets: list[tuple[DictionaryObject, DecodedStreamObject]] = field(default_factory=list)


class Document:
    """A paginated AcroForm document.

    Layout is top-down and single-pass: content is emitted in order and a new page starts
    when the cursor runs past the bottom margin. No reflow, no floating — a customs form
    is a sequence of blocks, and a layout engine that could move them is a layout engine
    that could put a figure under the wrong heading.
    """

    def __init__(self, title: str, subtitle: str = "") -> None:
        self.title = title
        self.subtitle = subtitle
        self._pages: list[_Page] = [_Page()]
        self._y = PAGE_HEIGHT - MARGIN
        self._field_names: set[str] = set()
        self._heading(title, subtitle)

    # ------------------------------------------------------------------- primitives

    @property
    def _page(self) -> _Page:
        return self._pages[-1]

    @property
    def content_width(self) -> float:
        return PAGE_WIDTH - 2 * MARGIN

    def _new_page(self) -> None:
        self._pages.append(_Page())
        self._y = PAGE_HEIGHT - MARGIN

    def _ensure(self, needed: float) -> None:
        if self._y - needed < MARGIN:
            self._new_page()

    def _draw(self, value: str, x: float, y: float, size: float, *, bold: bool = False) -> None:
        font = "/FB" if bold else "/F1"
        self._page.operations.append(
            b"BT " + font.encode() + b" " + str(size).encode() + b" Tf "
            b"" + f"{x:.2f} {y:.2f}".encode() + b" Td (" + escape(value) + b") Tj ET"
        )

    def _rule(self, y: float, *, width: float = 0.5) -> None:
        self._page.operations.append(
            f"{width} w {MARGIN:.2f} {y:.2f} m {PAGE_WIDTH - MARGIN:.2f} {y:.2f} l S".encode()
        )

    def _box(self, x: float, y: float, width: float, height: float) -> None:
        self._page.operations.append(
            f"0.75 g {x:.2f} {y:.2f} {width:.2f} {height:.2f} re f 0 g".encode()
        )

    # ---------------------------------------------------------------------- content

    def _heading(self, title: str, subtitle: str) -> None:
        self._draw(title, MARGIN, self._y - HEADING_SIZE, HEADING_SIZE, bold=True)
        self._y -= HEADING_SIZE + 4
        if subtitle:
            self._draw(subtitle, MARGIN, self._y - LABEL_SIZE, LABEL_SIZE)
            self._y -= LABEL_SIZE + 4
        self._rule(self._y)
        self._y -= 14

    def paragraph(self, body: str, *, size: float = LABEL_SIZE) -> None:
        """Wrapped prose. Used for certifications and for the provenance note."""
        words = body.split()
        line: list[str] = []
        for word in words:
            trial = " ".join([*line, word])
            if text_width(trial, size) > self.content_width and line:
                self._ensure(LINE_HEIGHT)
                self._draw(" ".join(line), MARGIN, self._y - size, size)
                self._y -= LINE_HEIGHT
                line = [word]
            else:
                line.append(word)
        if line:
            self._ensure(LINE_HEIGHT)
            self._draw(" ".join(line), MARGIN, self._y - size, size)
            self._y -= LINE_HEIGHT
        self._y -= 4

    def section(self, section: Section) -> None:
        """A titled block of fields.

        The title and its first row are placed together: a section heading stranded at the
        foot of a page, with its fields overleaf, reads as a heading over nothing.
        """
        first_row_height = 30.0
        self._ensure(SECTION_SIZE + 10 + first_row_height)

        self._draw(section.title, MARGIN, self._y - SECTION_SIZE, SECTION_SIZE, bold=True)
        self._y -= SECTION_SIZE + 4
        self._rule(self._y, width=0.25)
        self._y -= 10

        for row in section.rows:
            self._row(row)

        if section.note:
            self.paragraph(section.note)
        self._y -= 6

    def _row(self, row: Sequence[Field]) -> None:
        height = 26.0
        self._ensure(height)
        x = MARGIN
        gutter = 8.0
        total_gutter = gutter * (len(row) - 1)
        usable = self.content_width - total_gutter

        for item in row:
            width = usable * item.width
            self._draw(item.label.upper(), x, self._y - LABEL_SIZE, LABEL_SIZE)
            box_top = self._y - LABEL_SIZE - 3
            box_height = 13.0
            self._add_widget(item, x, box_top - box_height, width, box_height)
            x += width + gutter

        self._y -= height

    def table(
        self,
        headers: Sequence[str],
        widths: Sequence[float],
        rows: Sequence[Sequence[str]],
    ) -> None:
        """A fixed-column table of already-formatted strings.

        Values are drawn rather than made into fields: a line-item grid can run to hundreds
        of rows, and an AcroForm field per cell makes a document no viewer will open
        quickly. The per-claim totals above it are fields; the itemisation is a schedule.
        """
        gutter = 6.0
        usable = self.content_width - gutter * (len(headers) - 1)

        def emit_headers() -> None:
            self._ensure(LINE_HEIGHT * 2)
            self._box(MARGIN, self._y - LABEL_SIZE - 3, self.content_width, LABEL_SIZE + 5)
            x = MARGIN
            for header, share in zip(headers, widths, strict=True):
                self._draw(header.upper(), x + 2, self._y - LABEL_SIZE, LABEL_SIZE, bold=True)
                x += usable * share + gutter
            self._y -= LINE_HEIGHT + 2

        emit_headers()
        for row in rows:
            if self._y - LINE_HEIGHT < MARGIN:
                self._new_page()
                emit_headers()
            x = MARGIN
            for cell, share in zip(row, widths, strict=True):
                width = usable * share
                self._draw(
                    truncate(cell, width - 4, BODY_SIZE), x + 2, self._y - BODY_SIZE, BODY_SIZE
                )
                x += width + gutter
            self._y -= LINE_HEIGHT
        self._y -= 6

    # ----------------------------------------------------------------- form widgets

    def _add_widget(self, item: Field, x: float, y: float, width: float, height: float) -> None:
        """A text field with a generated appearance stream.

        The appearance is generated here rather than left to `/NeedAppearances`: viewers
        disagree about that flag, and a packet whose figures are invisible in one reader
        is a packet that gets filed blank.
        """
        if item.name in self._field_names:
            msg = (
                f"duplicate form field {item.name!r}; values are read back by name, so "
                "two fields sharing one would make the packet ambiguous"
            )
            raise ValueError(msg)
        self._field_names.add(item.name)

        shown = truncate(item.value, width - 6, BODY_SIZE)

        appearance = DecodedStreamObject()
        appearance.set_data(
            b"/Tx BMC q BT /F1 "
            + str(BODY_SIZE).encode()
            + b" Tf 2 "
            + f"{(height - BODY_SIZE) / 2 + 1:.2f}".encode()
            + b" Td ("
            + escape(shown)
            + b") Tj ET Q EMC"
        )
        appearance[NameObject("/Type")] = NameObject("/XObject")
        appearance[NameObject("/Subtype")] = NameObject("/Form")
        appearance[NameObject("/BBox")] = ArrayObject(
            [FloatObject(0), FloatObject(0), FloatObject(width), FloatObject(height)]
        )

        widget = DictionaryObject()
        widget[NameObject("/Type")] = NameObject("/Annot")
        widget[NameObject("/Subtype")] = NameObject("/Widget")
        widget[NameObject("/FT")] = NameObject("/Tx")
        widget[NameObject("/T")] = TextStringObject(item.name)
        widget[NameObject("/TU")] = TextStringObject(item.label)
        widget[NameObject("/V")] = TextStringObject(item.value)
        widget[NameObject("/DA")] = TextStringObject(f"/F1 {BODY_SIZE} Tf 0 g")
        widget[NameObject("/F")] = NumberObject(4)  # print
        widget[NameObject("/Rect")] = ArrayObject(
            [
                FloatObject(x),
                FloatObject(y),
                FloatObject(x + width),
                FloatObject(y + height),
            ]
        )
        self._page.widgets.append((widget, appearance))

    # ------------------------------------------------------------------------ output

    def render(self) -> bytes:
        """Serialise to PDF bytes."""
        writer = PdfWriter()
        fields = ArrayObject()

        regular = DictionaryObject()
        regular[NameObject("/Type")] = NameObject("/Font")
        regular[NameObject("/Subtype")] = NameObject("/Type1")
        regular[NameObject("/BaseFont")] = NameObject("/Helvetica")
        regular[NameObject("/Encoding")] = NameObject("/WinAnsiEncoding")
        regular_ref = writer._add_object(regular)

        bold = DictionaryObject()
        bold[NameObject("/Type")] = NameObject("/Font")
        bold[NameObject("/Subtype")] = NameObject("/Type1")
        bold[NameObject("/BaseFont")] = NameObject("/Helvetica-Bold")
        bold[NameObject("/Encoding")] = NameObject("/WinAnsiEncoding")
        bold_ref = writer._add_object(bold)

        font_resources = DictionaryObject()
        font_resources[NameObject("/F1")] = regular_ref
        font_resources[NameObject("/FB")] = bold_ref
        resources = DictionaryObject()
        resources[NameObject("/Font")] = font_resources
        resources_ref = writer._add_object(resources)

        for source in self._pages:
            page = writer.add_blank_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
            stream = DecodedStreamObject()
            stream.set_data(b"\n".join(source.operations))
            page[NameObject("/Contents")] = writer._add_object(stream)
            page[NameObject("/Resources")] = resources_ref

            annotations = ArrayObject()
            for widget, appearance in source.widgets:
                appearance[NameObject("/Resources")] = resources_ref
                normal = DictionaryObject()
                normal[NameObject("/N")] = writer._add_object(appearance)
                widget[NameObject("/AP")] = normal
                widget[NameObject("/P")] = page.indirect_reference
                reference = writer._add_object(widget)
                annotations.append(reference)
                fields.append(reference)
            page[NameObject("/Annots")] = annotations

        acroform = DictionaryObject()
        acroform[NameObject("/Fields")] = fields
        acroform[NameObject("/DA")] = TextStringObject(f"/F1 {BODY_SIZE} Tf 0 g")
        acroform[NameObject("/DR")] = resources_ref
        acroform[NameObject("/NeedAppearances")] = BooleanObject(False)
        writer._root_object[NameObject("/AcroForm")] = writer._add_object(acroform)

        buffer = BytesIO()
        writer.write(buffer)
        return buffer.getvalue()


def fill_template(template: Path, values: Mapping[str, str]) -> bytes:
    """Write values into an official CBP AcroForm template.

    Refuses on an unknown field name rather than dropping it. A silently-ignored field is
    a figure missing from a filed form, and the missing figure is discovered by CBP rather
    than by us.
    """
    reader = PdfReader(str(template))
    available = set(reader.get_fields() or {})
    unknown = sorted(set(values) - available)
    if unknown:
        msg = (
            f"template {template.name} has no fields named {unknown}; "
            f"it exposes {sorted(available)[:12]}..."
        )
        raise KeyError(msg)

    writer = PdfWriter(clone_from=reader)
    writer.set_need_appearances_writer(True)
    for page in writer.pages:
        writer.update_page_form_field_values(page, dict(values))

    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
