"""Week 7: embeddings, OCR gating, ERP explosion.

Each of these guards a place where a wrong answer looks exactly like a right one — a
lexical backend presented as semantic search, a 43%-confidence digit filed as a duty
amount, an ERP scrap percentage read as a yield.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from drawbridge_schemas.bom import (
    consumption_per_finished_unit,
    path_key,
    required_for_path,
)
from drawbridge_schemas.trade import HTSCode
from services.classifier.src.embeddings import (
    EMBEDDING_DIM,
    EmbeddingError,
    HashingEmbedder,
    OllamaEmbedder,
    build,
    to_pgvector,
)
from services.extraction.src.native import TextSpan
from services.extraction.src.ocr import (
    OCR_CONFIDENCE_FLOOR,
    OCR_FIELD_FLOOR,
    OCR_NUMERIC_FLOOR,
    GateVerdict,
    OcrConfig,
    OcrEngine,
    OcrSpan,
    blocking,
    gate,
    gate_all,
)
from services.ingest.src.erp_mock import (
    BomCycleError,
    ErpBomRow,
    ErpError,
    MockErpSource,
    explode,
    fetch,
    sample_source,
)

# --------------------------------------------------------------------------- helpers


def span(text: str, confidence: float) -> OcrSpan:
    return OcrSpan(
        span=TextSpan(page=1, bbox=(0.0, 0.0, 10.0, 10.0), text=text, language="en"),
        confidence=confidence,
        engine=OcrEngine.TESSERACT,
    )


def row(
    component: str,
    *,
    parent: str | None = None,
    qty: str = "1",
    scrap: str = "0",
    hts: str = "7318159000",
    phantom: bool = False,
    purchased: bool = True,
    finished: str = "FG-1",
) -> ErpBomRow:
    return ErpBomRow(
        finished_part=finished,
        component_part=component,
        parent_part=parent,
        quantity_per_parent=Decimal(qty),
        unit_of_measure="PCE",
        component_hts=hts,
        description=f"part {component}",
        scrap_percent=Decimal(scrap),
        is_phantom=phantom,
        is_purchased=purchased,
    )


def build_bom(*rows: ErpBomRow):
    return explode(
        rows,
        tenant_id=uuid4(),
        finished_good_hts="8413709000",
        finished_good_description="finished good",
        finished_unit_of_measure="PCE",
    )


# ------------------------------------------------------------------------ embeddings


class TestHashingEmbedder:
    def test_dimension_and_determinism(self) -> None:
        embedder = HashingEmbedder()
        first = embedder.embed(["portable data processing machines"])[0]
        second = embedder.embed(["portable data processing machines"])[0]
        assert len(first) == EMBEDDING_DIM
        assert first == second

    def test_vectors_are_unit_length(self) -> None:
        """pgvector's <=> is cosine distance; normalising keeps scale out of the answer."""
        vector = HashingEmbedder().embed(["8471300100 portable machines"])[0]
        assert sum(c * c for c in vector) == pytest.approx(1.0, abs=1e-9)

    def test_near_duplicate_text_is_nearer_than_unrelated_text(self) -> None:
        embedder = HashingEmbedder()
        base, near, far = embedder.embed(
            [
                "portable automatic data processing machines",
                "portable automatic data processing machine",
                "frozen concentrated orange juice",
            ]
        )
        assert _cosine(base, near) > _cosine(base, far)

    def test_it_declares_itself_lexical(self) -> None:
        """A paraphrase it cannot match must not be sold as semantic search."""
        assert HashingEmbedder().is_semantic is False

    def test_empty_text_raises_rather_than_returning_zeros(self) -> None:
        """A zero vector sits at maximum distance from everything — 'no match', not 'no answer'."""
        with pytest.raises(EmbeddingError, match="empty text"):
            HashingEmbedder().embed(["   "])

    def test_batch_order_is_preserved(self) -> None:
        embedder = HashingEmbedder()
        texts = ["alpha widget", "beta widget", "gamma widget"]
        batch = embedder.embed(texts)
        assert batch == [embedder.embed([t])[0] for t in texts]


class TestBackendSelection:
    def test_known_backends_build(self) -> None:
        assert isinstance(build("hashing"), HashingEmbedder)
        assert isinstance(build("ollama"), OllamaEmbedder)

    def test_an_unknown_backend_raises_rather_than_defaulting(self) -> None:
        """A typo must not silently embed a corpus with the lexical backend."""
        with pytest.raises(KeyError, match="unknown embedding backend"):
            build("openai")

    def test_ollama_records_its_model_in_the_id(self) -> None:
        assert build("ollama", model="mxbai-embed-large").model_id == "ollama:mxbai-embed-large"

    def test_pgvector_literal_form(self) -> None:
        assert to_pgvector([1.0, -0.5, 0.25]) == "[1,-0.5,0.25]"


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


# ----------------------------------------------------------------------- OCR gating


class TestFloors:
    def test_numeric_is_stricter_than_text(self) -> None:
        """A misread digit is a wrong filed figure; a misread letter is a typo."""
        assert OCR_NUMERIC_FLOOR > OCR_CONFIDENCE_FLOOR > OCR_FIELD_FLOOR

    def test_the_token_floor_is_the_week_7_value(self) -> None:
        assert OCR_CONFIDENCE_FLOOR == 0.90

    def test_a_numeric_token_is_held_to_the_numeric_floor(self) -> None:
        assert span("62500.00", 0.92).below_floor is True
        assert span("Merchandise", 0.92).below_floor is False


class TestGate:
    def test_a_clean_text_field_is_accepted(self) -> None:
        reading = gate("description", [span("Portable", 0.97), span("machines", 0.96)])
        assert reading.verdict is GateVerdict.ACCEPT
        assert reading.usable is True

    def test_a_clean_numeric_field_is_accepted(self) -> None:
        reading = gate("duty_amount", [span("62500.00", 0.99)])
        assert reading.verdict is GateVerdict.ACCEPT
        assert reading.is_numeric is True

    def test_a_low_confidence_digit_is_rejected_not_reviewed(self) -> None:
        """This is the week 7 point: it must never reach the matcher in any form."""
        reading = gate("duty_amount", [span("62500.00", 0.43)])
        assert reading.verdict is GateVerdict.REJECT
        assert reading.usable is False
        assert "0.95 floor" in reading.reason

    def test_a_low_confidence_word_is_reviewed_not_rejected(self) -> None:
        reading = gate("description", [span("Portable", 0.71)])
        assert reading.verdict is GateVerdict.REVIEW
        assert reading.usable is False

    def test_a_uniformly_mediocre_field_fails_on_aggregate(self) -> None:
        """No token fails; the field still is not readable. Only a mean catches this."""
        spans = [span("Portable", 0.91), span("automatic", 0.91), span("machines", 0.91)]
        assert all(not s.below_floor for s in spans)
        reading = gate("description", spans, OcrConfig(field_floor=0.95))
        assert reading.verdict is GateVerdict.REVIEW
        assert "uniformly uncertain" in reading.reason

    def test_the_mean_is_weighted_by_length(self) -> None:
        """A 1-char fragment at 0.99 must not carry a 12-char amount at 0.80 over the line."""
        reading = gate("description", [span("x", 0.99), span("Merchandise!", 0.91)])
        assert reading.mean_confidence < 0.93

    def test_a_one_character_field_is_rejected_whatever_its_confidence(self) -> None:
        reading = gate("port_code", [span("7", 1.0)])
        assert reading.verdict is GateVerdict.REJECT
        assert "not evidence" in reading.reason

    def test_a_numeric_field_contaminated_with_arabic_is_rejected(self) -> None:
        """Letters in a numeric field mean the engine merged an adjacent label in."""
        reading = gate("duty_amount", [span("الرسوم 18750.00", 0.99)])
        assert reading.verdict is GateVerdict.REJECT
        assert "second script" in reading.reason

    def test_an_empty_field_is_rejected_with_its_reason(self) -> None:
        reading = gate("duty_amount", [])
        assert reading.verdict is GateVerdict.REJECT
        assert reading.text == ""
        assert "no tokens" in reading.reason

    def test_a_rejected_reading_still_carries_its_evidence(self) -> None:
        """An analyst asked why a figure is missing needs the numbers, not a blank."""
        reading = gate("duty_amount", [span("62500.00", 0.43)])
        payload = reading.as_dict()
        assert payload["min_confidence"] == 0.43
        assert payload["token_count"] == 1
        assert payload["text"] == "62500.00"

    def test_a_tenant_can_loosen_its_own_floors(self) -> None:
        loose = OcrConfig(numeric_floor=0.40, field_floor=0.40, confidence_floor=0.40)
        assert gate("duty_amount", [span("62500.00", 0.43)], loose).verdict is GateVerdict.ACCEPT

    def test_arabic_indic_digits_are_treated_as_numeric(self) -> None:
        """`normalise` folds them to ASCII upstream, so the numeric floor must apply."""
        assert span("18750.00", 0.92).is_numeric is True


class TestGateAll:
    def test_only_rejections_block(self) -> None:
        readings = gate_all(
            {
                "description": [span("Portable", 0.71)],
                "duty_amount": [span("62500.00", 0.43)],
                "entry_number": [span("A12-3456789-0", 0.99)],
            }
        )
        assert readings["entry_number"].verdict is GateVerdict.ACCEPT
        assert readings["description"].verdict is GateVerdict.REVIEW
        blockers = blocking(readings)
        assert [r.label for r in blockers] == ["duty_amount"]


# --------------------------------------------------------------------- ERP explosion


class TestErpExplosion:
    def test_scrap_becomes_yield_not_the_other_way_round(self) -> None:
        """5% scrap is 0.95 yield. Backwards, every level understates consumption."""
        bom = build_bom(row("PART", scrap="5"))
        assert bom.components[0].yield_rate == Decimal("0.95")

    def test_the_sample_bill_explodes_to_the_hand_computed_multiplier(self) -> None:
        tenant = uuid4()
        bom = fetch(
            sample_source(tenant),
            tenant_id=tenant,
            finished_part="PUMP-8000",
            finished_good_hts="8413709000",
            finished_good_description="Centrifugal pump",
        )
        routes = {path_key(p): p for p in bom.designatable_paths()}
        billet = routes["8483409000>8483900000>7207200000"]
        # (1 / 0.90) x (2 / 0.75) x (3 / 0.50) = 17.7777...
        assert required_for_path(billet, Decimal("1")) == Decimal("17.7777")
        assert required_for_path(billet, Decimal("90")) == Decimal("1600.0000")

    def test_a_phantom_collapses_into_its_parent(self) -> None:
        """A phantom is never stocked, so it cannot be a designation level."""
        tenant = uuid4()
        bom = fetch(
            sample_source(tenant),
            tenant_id=tenant,
            finished_part="PUMP-8000",
            finished_good_hts="8413709000",
            finished_good_description="Centrifugal pump",
        )
        keys = {path_key(p) for p in bom.designatable_paths()}
        # The bolt attaches directly to the finished good, not beneath a FASTENER-KIT node.
        assert "7318159000" in keys
        assert not any(key.count(">") and key.endswith("7318159000") for key in keys)

    def test_a_phantom_multiplies_its_children_through(self) -> None:
        bom = build_bom(
            row("KIT", parent=None, qty="3", phantom=True, hts="7318159000"),
            row("BOLT", parent="KIT", qty="8", hts="7318159000"),
        )
        path = bom.path_for(HTSCode(code="7318159000"))
        assert path is not None
        assert consumption_per_finished_unit(path) == Decimal("24")

    def test_an_in_house_stage_is_not_designatable(self) -> None:
        bom = build_bom(
            row("SUB", parent=None, hts="8483409000", purchased=False),
            row("LEAF", parent="SUB", hts="7207200000"),
        )
        assert bom.path_for(HTSCode(code="8483409000")) is None
        assert bom.path_for(HTSCode(code="7207200000")) is not None

    def test_row_order_from_the_extract_does_not_change_the_tree(self) -> None:
        """ERP extracts arrive unordered; the model must not depend on that."""
        rows = [
            row("SUB", parent=None, hts="8483409000", purchased=False),
            row("LEAF", parent="SUB", hts="7207200000", qty="2"),
        ]
        forward = build_bom(*rows)
        reverse = build_bom(*reversed(rows))
        assert [path_key(p) for p in forward.walk()] == [path_key(p) for p in reverse.walk()]

    def test_the_bom_id_is_stable_across_pulls(self) -> None:
        """A re-pulled bill must keep its id, or stored claim references stop resolving."""
        tenant = uuid4()
        source = sample_source(tenant)
        args = {
            "tenant_id": tenant,
            "finished_part": "PUMP-8000",
            "finished_good_hts": "8413709000",
            "finished_good_description": "Centrifugal pump",
        }
        assert fetch(source, **args).bom_id == fetch(source, **args).bom_id


class TestErpRefusals:
    def test_a_cycle_is_fatal(self) -> None:
        """Truncating the loop would give a plausible multiplier from an incoherent bill."""
        with pytest.raises(BomCycleError, match="its own ancestor"):
            build_bom(
                row("A", parent=None, hts="8483409000", purchased=False),
                row("B", parent="A", hts="8483900000", purchased=False),
                row("A", parent="B", hts="8483409000", purchased=False),
            )

    def test_full_scrap_is_refused(self) -> None:
        with pytest.raises(ErpError, match="scrap must be in"):
            build_bom(row("PART", scrap="100"))

    def test_negative_scrap_is_refused(self) -> None:
        with pytest.raises(ErpError, match="scrap must be in"):
            build_bom(row("PART", scrap="-5"))

    def test_a_non_positive_quantity_is_refused(self) -> None:
        with pytest.raises(ErpError, match="not a component"):
            build_bom(row("PART", qty="0"))

    def test_a_bill_with_no_top_level_row_is_refused(self) -> None:
        with pytest.raises(ErpError, match="no attachment point"):
            build_bom(row("B", parent="A"))

    def test_rows_spanning_two_finished_goods_are_refused(self) -> None:
        with pytest.raises(ErpError, match="more than one finished good"):
            build_bom(row("A", finished="FG-1"), row("B", finished="FG-2"))

    def test_a_childless_phantom_is_refused(self) -> None:
        with pytest.raises(ErpError, match="no children"):
            build_bom(row("KIT", parent=None, phantom=True))

    def test_an_empty_extract_is_refused(self) -> None:
        with pytest.raises(ErpError, match="no rows"):
            build_bom()

    def test_an_unknown_part_names_what_the_source_holds(self) -> None:
        source = MockErpSource()
        with pytest.raises(ErpError, match="no bill for part"):
            source.rows_for(uuid4(), "NOPE")

    def test_one_tenants_bill_is_not_visible_to_another(self) -> None:
        """Two customers can hold the same part number for different articles."""
        mine, theirs = uuid4(), uuid4()
        source = sample_source(mine)
        assert source.finished_parts(mine) == ["PUMP-8000"]
        assert source.finished_parts(theirs) == []
        with pytest.raises(ErpError, match="under tenant"):
            source.rows_for(theirs, "PUMP-8000")
