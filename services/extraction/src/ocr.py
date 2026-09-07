"""Bilingual OCR fallback.

Reached only when `native.assess` rejects the text layer. Everything here is slower and
less certain than the native path, and the confidence scores reflect that honestly rather
than flattering the result.

Engine choice: Tesseract with `ara+eng` is the default because the Arabic traineddata
ships in Debian and the language pair can be requested in one pass. PaddleOCR's Arabic
model is more accurate on low-quality scans but pulls a much larger image; it is selected
per-tenant where scan quality justifies the size.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from services.extraction.src.arabic import detect_language, normalise
from services.extraction.src.native import TextSpan

# Below this, a recognised token is not trustworthy enough to carry a figure into a
# claim. Tokens under the floor still surface — flagged — so an analyst sees them
# rather than the field silently going missing.
OCR_CONFIDENCE_FLOOR = 0.70

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
    def below_floor(self) -> bool:
        return self.confidence < OCR_CONFIDENCE_FLOOR


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
