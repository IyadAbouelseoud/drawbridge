"""Bilingual OCR fallback.

Reached only when `native.assess` rejects the text layer. Everything here is slower and
less certain than the native path, and the confidence scores reflect that honestly rather
than flattering the result.

Engine choice: Tesseract with `ara+eng` is the default because the Arabic traineddata
ships in Debian and the language pair can be requested in one pass. PaddleOCR's Arabic
model is more accurate on low-quality scans but pulls a much larger image; it is selected
per-tenant where scan quality justifies the size.

**Week 7: recognition and acceptance are now separate steps.** `recognise` reads;
`gate` decides what may leave the extraction service. Nothing between the OCR engine and
the matcher used to say no, which meant a 43%-confidence digit could become a filed
figure. Three thresholds now stand between them, and they are deliberately different:

- **`OCR_CONFIDENCE_FLOOR` (0.90)** — a token below this cannot carry a figure. Raised
  from the week 2 placeholder of 0.70 on the reasoning below.
- **`OCR_FIELD_FLOOR` (0.85)** — the *weighted mean* over a field's tokens. A field can
  fail on aggregate even when no single token does, which is the ordinary shape of a bad
  scan: everything slightly uncertain rather than one character obviously wrong.
- **`OCR_NUMERIC_FLOOR` (0.95)** — numeric tokens only. A misread letter in a goods
  description is a cosmetic defect an analyst corrects; a misread digit in a duty amount
  is a wrong number filed with a customs authority, and the two do not deserve the same
  tolerance.

**Why 0.90 rather than a tuned number.** Tuning needs a labelled scanned *Bayan* corpus,
which does not exist yet (roadmap, week 8). The honest move is not to guess a precise
value but to pick a conservative one and say so: at 0.90 the pipeline over-rejects, and
over-rejection costs analyst time while under-rejection costs a misfiled claim. When the
corpus arrives the floors move to measured error rates, and `OcrConfig` already carries
them per-tenant so that is a config change rather than a code change.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from services.extraction.src.arabic import detect_language, normalise
from services.extraction.src.native import TextSpan

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# Below this, a recognised token is not trustworthy enough to carry a figure into a
# claim. Tokens under the floor still surface — flagged — so an analyst sees them
# rather than the field silently going missing.
OCR_CONFIDENCE_FLOOR = 0.90

# Weighted-mean floor across a field's tokens. Lower than the per-token floor on purpose:
# a field of many good tokens and one mediocre one is still readable, while a field where
# every token sits at 0.88 is not, and only an aggregate catches the second case.
OCR_FIELD_FLOOR = 0.85

# Numeric tokens. A misread digit in a duty amount is a wrong figure filed with a customs
# authority; a misread letter in a description is a cosmetic defect. Different stakes,
# different floor.
OCR_NUMERIC_FLOOR = 0.95

# A field recognised from fewer than this many characters is not evidence, whatever its
# confidence. Tesseract reports high confidence on short spurious fragments, so length is
# an independent check rather than a redundant one.
MIN_FIELD_CHARS = 2

# Rasterise at this DPI before OCR. 300 is the accuracy/latency knee for Tesseract on
# customs paperwork; below ~200 Arabic diacritics start dropping.
RASTER_DPI = 300


class OcrEngine(StrEnum):
    TESSERACT = "tesseract"
    PADDLE = "paddleocr"


class OcrLanguage(StrEnum):
    """Language packs, in Tesseract's naming.

    ARA_ENG is the default for the GCC lane: a Bayan carries Arabic labels alongside
    Latin HS codes and Latin-script trade names, and running Arabic alone misreads the
    Latin runs.
    """

    ENG = "eng"
    ARA = "ara"
    ARA_ENG = "ara+eng"


@dataclass(frozen=True, slots=True)
class OcrConfig:
    """Per-tenant OCR settings.

    Defaults target the GCC lane. A US-only tenant sets `language=ENG`, which is both
    faster and more accurate than asking Tesseract to consider Arabic.
    """

    engine: OcrEngine = OcrEngine.TESSERACT
    language: OcrLanguage = OcrLanguage.ARA_ENG
    dpi: int = RASTER_DPI
    confidence_floor: float = OCR_CONFIDENCE_FLOOR
    field_floor: float = OCR_FIELD_FLOOR
    numeric_floor: float = OCR_NUMERIC_FLOOR
    min_field_chars: int = MIN_FIELD_CHARS

    # Tesseract page segmentation mode. 6 = "assume a single uniform block of text",
    # which beats the default on form-shaped documents like a Bayan or a 7501.
    psm: int = 6

    @property
    def tesseract_config(self) -> str:
        return f"--psm {self.psm} --oem 1"


@dataclass(frozen=True, slots=True)
class OcrSpan:
    """A recognised token with its confidence and the engine that produced it."""

    span: TextSpan
    confidence: float
    engine: OcrEngine

    @property
    def is_numeric(self) -> bool:
        """Whether this token carries digits.

        Checked after Arabic-Indic digits have been folded to ASCII by `normalise`, so an
        Arabic numeral in a Bayan is recognised as numeric rather than treated as prose
        and gated at the looser floor.
        """
        return any(char.isdigit() for char in self.span.text)

    @property
    def required_floor(self) -> float:
        return OCR_NUMERIC_FLOOR if self.is_numeric else OCR_CONFIDENCE_FLOOR

    @property
    def below_floor(self) -> bool:
        return self.confidence < self.required_floor

    def below_configured_floor(self, config: OcrConfig) -> bool:
        """Whether this token fails *this tenant's* floor.

        `below_floor` reads the module constants and is what most callers want.
        A tenant that has measured its own scan quality overrides them on `OcrConfig`,
        and the gate uses this instead so the override is actually honoured.
        """
        floor = config.numeric_floor if self.is_numeric else config.confidence_floor
        return self.confidence < floor


class OcrUnavailableError(RuntimeError):
    """Raised when the configured engine is not installed.

    Deliberately fatal rather than silently degrading: an extraction service that cannot
    read Arabic must fail loudly, not return empty results that look like a clean document
    with no fields. The extraction Dockerfile asserts `ara` and `eng` at build time for
    the same reason.
    """


def engine_available(config: OcrConfig) -> bool:
    if config.engine is OcrEngine.TESSERACT:
        return shutil.which("tesseract") is not None
    try:
        import paddleocr  # noqa: F401
    except ImportError:
        return False
    return True


def assert_available(config: OcrConfig) -> None:
    if not engine_available(config):
        msg = (
            f"OCR engine {config.engine} is not available. The extraction image must "
            f"provide it; see services/extraction/Dockerfile."
        )
        raise OcrUnavailableError(msg)


def recognise(pdf_path: Path, config: OcrConfig | None = None) -> list[OcrSpan]:
    """OCR a document to confidence-scored spans.

    Week 2 wires the path, the config surface, and the failure semantics. Tuning the
    confidence floor needs a real scanned Bayan corpus, which is the week 3 entry item —
    picking a number now without documents to validate against would be a guess wearing
    a constant's clothing.
    """
    config = config or OcrConfig()
    assert_available(config)

    if config.engine is OcrEngine.TESSERACT:
        return _recognise_tesseract(pdf_path, config)
    return _recognise_paddle(pdf_path, config)


def _recognise_tesseract(pdf_path: Path, config: OcrConfig) -> list[OcrSpan]:
    import pymupdf
    import pytesseract
    from PIL import Image

    spans: list[OcrSpan] = []
    with pymupdf.open(pdf_path) as doc:
        for page_index, page in enumerate(doc, start=1):
            pixmap = page.get_pixmap(dpi=config.dpi)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            # Scale OCR pixel coordinates back to PDF points so spans stay comparable
            # with native-path bboxes.
            scale = 72.0 / config.dpi

            data = pytesseract.image_to_data(
                image,
                lang=config.language.value,
                config=config.tesseract_config,
                output_type=pytesseract.Output.DICT,
            )
            for i, raw in enumerate(data["text"]):
                text = normalise(raw)
                if not text:
                    continue
                # Tesseract reports -1 for non-text regions.
                raw_conf = float(data["conf"][i])
                if raw_conf < 0:
                    continue
                x, y = data["left"][i] * scale, data["top"][i] * scale
                w, h = data["width"][i] * scale, data["height"][i] * scale
                spans.append(
                    OcrSpan(
                        span=TextSpan(
                            page=page_index,
                            bbox=(x, y, x + w, y + h),
                            text=text,
                            language=detect_language(text),
                        ),
                        confidence=raw_conf / 100.0,
                        engine=OcrEngine.TESSERACT,
                    )
                )
    return spans


def _recognise_paddle(pdf_path: Path, config: OcrConfig) -> list[OcrSpan]:
    msg = (
        "PaddleOCR path is configured but not implemented; it lands with the week 3 "
        "scanned corpus. Use OcrEngine.TESSERACT."
    )
    raise NotImplementedError(msg)


# ---------------------------------------------------------------------------- gating


class GateVerdict(StrEnum):
    """What may be done with a recognised field."""

    ACCEPT = "accept"
    """Every threshold cleared. The value may carry a figure into a claim."""

    REVIEW = "review"
    """Readable but not trustworthy. Surfaces to the analyst queue with its evidence."""

    REJECT = "reject"
    """Not evidence of anything. Never reaches the matcher, in any form."""


@dataclass(frozen=True, slots=True)
class FieldReading:
    """One extracted field, with every number behind the decision to accept it.

    The rejected case carries as much detail as the accepted one. An analyst asked why a
    duty amount is missing needs the confidences that caused it, not a blank.
    """

    label: str
    text: str
    spans: tuple[OcrSpan, ...]
    verdict: GateVerdict
    reason: str
    mean_confidence: float
    min_confidence: float
    is_numeric: bool

    @property
    def usable(self) -> bool:
        """Whether this value may reach the matcher.

        Only ACCEPT. A REVIEW field is shown to a human and may be corrected into a claim
        by hand; it does not flow through on its own confidence.
        """
        return self.verdict is GateVerdict.ACCEPT

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "text": self.text,
            "verdict": self.verdict,
            "reason": self.reason,
            "mean_confidence": round(self.mean_confidence, 4),
            "min_confidence": round(self.min_confidence, 4),
            "is_numeric": self.is_numeric,
            "token_count": len(self.spans),
        }


def _weighted_mean(spans: Sequence[OcrSpan]) -> float:
    """Confidence averaged over tokens, weighted by character count.

    Unweighted, a one-character fragment at 0.99 would offset a twelve-character amount at
    0.80 and pull a bad field over the line. Weighting by length makes the mean reflect
    how much of the field was actually read well.
    """
    total_chars = sum(len(span.span.text) for span in spans)
    if not total_chars:
        return 0.0
    return sum(span.confidence * len(span.span.text) for span in spans) / total_chars


def gate(
    label: str,
    spans: Sequence[OcrSpan],
    config: OcrConfig | None = None,
) -> FieldReading:
    """Decide whether a recognised field may carry a figure into a claim.

    Checks run cheapest-and-most-disqualifying first, and each one names the threshold it
    failed. Order matters for the reason string, not for correctness: a field failing two
    checks is reported against the more fundamental one, because that is the one to fix.

    A numeric field is gated at the numeric floor throughout — including its aggregate.
    The asymmetry is deliberate: a description an analyst can correct by eye and a duty
    amount that would be filed as read are not the same kind of error.
    """
    config = config or OcrConfig()
    text_value = " ".join(span.span.text for span in spans).strip()
    numeric = any(span.is_numeric for span in spans)

    if not spans or not text_value:
        return FieldReading(
            label=label,
            text="",
            spans=tuple(spans),
            verdict=GateVerdict.REJECT,
            reason="no tokens recognised for this field",
            mean_confidence=0.0,
            min_confidence=0.0,
            is_numeric=False,
        )

    mean = _weighted_mean(spans)
    lowest = min(span.confidence for span in spans)
    aggregate_floor = config.numeric_floor if numeric else config.field_floor

    def reading(verdict: GateVerdict, reason: str) -> FieldReading:
        return FieldReading(
            label=label,
            text=text_value,
            spans=tuple(spans),
            verdict=verdict,
            reason=reason,
            mean_confidence=mean,
            min_confidence=lowest,
            is_numeric=numeric,
        )

    if len(text_value) < config.min_field_chars:
        # High confidence on a one-character fragment is Tesseract being certain about
        # noise, not about a field.
        return reading(
            GateVerdict.REJECT,
            f"{len(text_value)} characters is below the {config.min_field_chars}-character "
            "minimum; a fragment is not evidence whatever its confidence",
        )

    if numeric and _mixed_scripts(text_value):
        # A numeric field carrying Arabic letters means the engine bled a neighbouring
        # label into the value. The number that survives that is not the number on the
        # document.
        return reading(
            GateVerdict.REJECT,
            "numeric field contains letters from a second script; the engine has merged "
            "an adjacent label into the value",
        )

    failing = [span for span in spans if span.below_configured_floor(config)]
    if failing:
        worst = min(failing, key=lambda span: span.confidence)
        verdict = GateVerdict.REJECT if numeric else GateVerdict.REVIEW
        return reading(
            verdict,
            f"token {worst.span.text!r} at {worst.confidence:.2f} is below the "
            f"{worst.required_floor:.2f} floor for "
            f"{'numeric' if worst.is_numeric else 'text'} tokens",
        )

    if mean < aggregate_floor:
        return reading(
            GateVerdict.REJECT if numeric else GateVerdict.REVIEW,
            f"weighted mean {mean:.3f} is below the {aggregate_floor:.2f} field floor; "
            "no single token failed, so the whole field is uniformly uncertain",
        )

    return reading(
        GateVerdict.ACCEPT,
        f"all {len(spans)} tokens cleared their floors; weighted mean {mean:.3f}",
    )


def gate_all(
    fields: Mapping[str, Sequence[OcrSpan]],
    config: OcrConfig | None = None,
) -> dict[str, FieldReading]:
    """Gate a whole document's fields at once."""
    return {label: gate(label, spans, config) for label, spans in fields.items()}


def blocking(readings: Mapping[str, FieldReading]) -> list[FieldReading]:
    """Readings that must stop a claim before the matcher.

    REJECT only. REVIEW is a queue item, not a blocker — an analyst clears it and the
    claim continues, which is the whole point of having two failure grades rather than one.
    """
    return [reading for reading in readings.values() if reading.verdict is GateVerdict.REJECT]


def _mixed_scripts(value: str) -> bool:
    """Whether a value mixes Arabic letters with digits.

    Arabic-Indic digits are excluded from the letter test by `normalise` having already
    folded them to ASCII, so this fires on genuine letter contamination rather than on a
    legitimately Arabic-numbered field.
    """
    has_digit = any(char.isdigit() for char in value)
    has_arabic_letter = any("\u0620" <= char <= "\u064a" for char in value)
    return has_digit and has_arabic_letter
