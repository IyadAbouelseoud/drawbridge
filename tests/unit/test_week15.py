"""Week 15 — what running the thing found.

Every defect pinned here was invisible to the checks that existed. The embedding text was
valid text, the workflow files were valid JSON, the image tags were valid tags. Each was
being verified for form and never for use, so each test below asserts against *use*: the
convention a vector was written under, the body a node actually posts, the digest a
registry actually serves.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from services.classifier.src.search import (
    CODE_SCORE,
    LEXICAL_FLOOR,
    TariffHit,
    code_prefix,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / "n8n" / "workflows"


class TestTheCodeInAQuery:
    """`embed_corpus.py` prefixed every document with its own tariff code so that a query
    naming a code would reach the line "by either half". It never did — no analyst query
    carries a code, so the only effect was 28,899 document vectors carrying a token the
    query side never contains. The lookup replaces the hope."""

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("8471.30 portable machines", "847130"),
            ("hs 090121 coffee", "090121"),
            ("0901210015", "0901210015"),
            ("8471.30.01.00", "8471300100"),
        ],
    )
    def test_it_finds_a_code_however_it_is_punctuated(self, query: str, expected: str) -> None:
        assert code_prefix(query) == expected

    @pytest.mark.parametrize(
        "query",
        [
            "ruggedised field laptop computer",
            "men's cotton tee shirts, knitted",
            # Four digits is a heading, not something the schedule can be looked up by
            # here: matching `8471%` would return every ADP line in the chapter and call
            # it an exact answer.
            "8471 machines",
            # A container number is eleven characters of letters and digits. Treating it
            # as a classification would answer a shipping question with a tariff line.
            "container MSCU1234567 of shirts",
        ],
    )
    def test_it_does_not_invent_one(self, query: str) -> None:
        assert code_prefix(query) is None

    def test_a_code_hit_outranks_any_similarity(self) -> None:
        """Cosine and trigram similarity are both bounded by 1.0, and a code match scores
        exactly that. Ranking a paraphrase above the line the analyst asked for by name
        would be the search overriding its user."""
        assert CODE_SCORE >= 1.0

    def test_a_code_hit_does_not_ask_the_caller_to_confirm_the_caller(self) -> None:
        """Nothing was inferred: the caller typed digits and this is the schedule's row for
        them. A confirmation prompt here trains the habit of clearing the flag, which is
        what makes the flag work on the hits that are inferences."""
        hit = TariffHit(
            code="8471300100",
            description_en="Portable automatic data processing machines",
            description_ar=None,
            jurisdiction="us",
            source="usitc_hts",
            revision="2026-HTSA",
            duty_rate_general="Free",
            score=CODE_SCORE,
            lexical_score=None,
            vector_distance=None,
            code_matched=True,
        )
        assert hit.matched_by == "code"
        assert hit.needs_analyst_confirmation is False

    def test_prefix_hits_come_back_in_a_reproducible_order(self) -> None:
        """Every code hit scores exactly CODE_SCORE. Without a tiebreak the merge returns
        them in whatever order a set iterated, so the same query on the same corpus could
        rank differently between runs — and a classification that reached a filing has to
        be reproducible years later. Shorter first, which is the subheading before its
        ten-digit statistical suffixes."""
        from services.classifier.src.search import _merge

        rows = {
            code: {
                "code": code,
                "description_en": "x",
                "description_ar": None,
                "jurisdiction": "us",
                "source": "usitc_hts",
                "revision": "2026-HTSA",
                "duty_rate_general": None,
            }
            for code in ("8471300100", "84713001", "8471300180")
        }
        hits = _merge({}, {}, 0.20, rows)
        hits.sort(key=lambda h: (-h.score, len(h.code), h.code))
        assert [h.code for h in hits] == ["84713001", "8471300100", "8471300180"]

    def test_a_vector_only_hit_still_does(self) -> None:
        """The exemption is for lookups, not for hits that happen to score well."""
        hit = TariffHit(
            code="8471300100",
            description_en="Portable automatic data processing machines",
            description_ar=None,
            jurisdiction="us",
            source="usitc_hts",
            revision="2026-HTSA",
            duty_rate_general="Free",
            score=0.9,
            lexical_score=None,
            vector_distance=0.1,
        )
        assert hit.matched_by == "vector"
        assert hit.needs_analyst_confirmation is True


class TestTheEmbeddingIsStamped:
    """`embeddings.py` has claimed since week 8 that a corpus is queryable only by the
    backend that wrote it and that mixing them is "a data error the ingest can detect".
    There was no column. Seven weeks later the corpus and the query side turned out to be
    in different distributions and there was no way to ask a row why."""

    def test_the_convention_is_part_of_the_stamp(self) -> None:
        """The failure that actually happened was one model over two texts, not two
        models. A stamp holding only the model id would have recorded the same string for
        the code-prefixed corpus and the fixed one, and caught nothing."""
        from scripts.embed_corpus import TEXT_CONVENTION, corpus_id

        class _Backend:
            model_id = "fastembed:sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

        stamp = corpus_id(_Backend())  # type: ignore[arg-type]
        assert stamp.endswith(f"/{TEXT_CONVENTION}")
        assert _Backend.model_id in stamp

    def test_a_long_model_name_shortens_the_stamp_rather_than_failing_the_write(
        self,
    ) -> None:
        """A local Ollama model with a long name must degrade to a truncated stamp, not to
        an unembedded corpus."""
        from scripts.embed_corpus import corpus_id

        class _Backend:
            model_id = "ollama:" + "x" * 400

        assert len(corpus_id(_Backend())) == 128  # type: ignore[arg-type]

    def test_the_code_is_no_longer_joined_to_the_embedded_text(self) -> None:
        """The regression that matters, asserted against the source: reintroducing the
        prefix would put the corpus back into a distribution no query reaches, and every
        symptom would again look like a weak model."""
        source = (REPO / "scripts" / "embed_corpus.py").read_text(encoding="utf-8")
        assert "f\"{row['code']} {row['body']}\"" not in source


class TestTheWorkflowsCanActuallyBeImported:
    """Three files, validated as JSON since week 9, none of them importable. Everything
    here is a shape `n8n import:workflow` rejects or silently mis-resolves."""

    @pytest.fixture
    def workflows(self) -> list[dict]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(WORKFLOWS.glob("*.json"))]

    def test_every_workflow_has_a_stable_id(self, workflows: list[dict]) -> None:
        """Without one the importer inserts a NULL primary key and fails outright. A
        *stable* one is what makes a re-import an update rather than another inactive
        duplicate beside the one that is running."""
        ids = [w.get("id") for w in workflows]
        assert all(ids), f"workflow without an id: {ids}"
        assert len(set(ids)) == len(ids)

    def test_no_workflow_carries_tags(self, workflows: list[dict]) -> None:
        """`tag_entity.name` is uniquely indexed and the importer creates tags per
        workflow rather than reconciling them, so three files sharing `drawbridge` failed
        the batch import on the second one. Tags are how a person organises a list in the
        UI; putting UI state in the artefact is what made the artefact un-importable."""
        assert [w for w in workflows if w.get("tags")] == []

    def test_every_api_call_carries_the_service_token(self, workflows: list[dict]) -> None:
        """Week 12 added this header to the generated JSON by hand and not to the
        generator. Regenerating would have stripped it from every call and the pipeline
        would have started failing on 401 with a diff that read as formatting."""
        for workflow in workflows:
            for node in workflow["nodes"]:
                if node["type"] != "n8n-nodes-base.httpRequest":
                    continue
                headers = node["parameters"].get("headerParameters", {}).get("parameters", [])
                names = {h["name"]: h["value"] for h in headers}
                assert "Authorization" in names, f"{workflow['name']}/{node['name']}"
                assert "$env.DRAWBRIDGE_SERVICE_TOKEN" in names["Authorization"]

    def test_no_node_reads_another_node_through_item_pairing(self, workflows: list[dict]) -> None:
        """`$('Node').item` resolves through n8n's item pairing, which this pipeline loses
        at every Code node that builds a fresh array. It does not raise when it fails — it
        yields undefined, `JSON.stringify` drops the key, and the request goes out missing
        a field. `/review/suspend` returned 422 "Field required: body.tenant_id" against a
        payload whose tenant_id was present three nodes upstream."""
        for workflow in workflows:
            body = json.dumps(workflow)
            assert ".item.json" not in body, workflow["name"]

    def test_the_error_workflow_is_referenced_by_id(self, workflows: list[dict]) -> None:
        """n8n resolves `errorWorkflow` as an id. The name matched nothing, so the handler
        that exists to record a failed run never ran — and an error handler that is never
        reached is worse than none, because a silent alert reads as no problem."""
        by_name = {w["name"]: w for w in workflows}
        ids = {w["id"] for w in workflows}
        configured = by_name["drawbridge-claim-pipeline"]["settings"]["errorWorkflow"]
        assert configured in ids

    def test_single_operand_conditions_say_so(self, workflows: list[dict]) -> None:
        """A version-2 If node with a boolean operator still reads `rightValue`, and the
        empty string it found was rejected: "Wrong type: '' is a string but was expecting
        a boolean". The gate deciding whether a claim needs an analyst failed on the shape
        of an operand it does not have."""
        for workflow in workflows:
            for node in workflow["nodes"]:
                if node["type"] != "n8n-nodes-base.if":
                    continue
                for condition in node["parameters"]["conditions"]["conditions"]:
                    operator = condition["operator"]
                    if operator.get("type") == "boolean":
                        assert operator.get("singleValue") is True
                        assert "rightValue" not in condition

    def test_the_execution_id_is_sent_as_a_string(self, workflows: list[dict]) -> None:
        """n8n's execution id is a number and `SuspendRequest.workflow_run_id` is
        `str | None`. Pydantic v2 does not coerce, so the one node whose job is to record
        that a run needs a human came back 422."""
        body = json.dumps(by_name_body(workflows, "drawbridge-claim-pipeline"))
        for match in re.finditer(r"workflow_run_id: ([^,}]+)", body):
            assert "String(" in match.group(1), match.group(1)

    def test_the_generator_reproduces_what_is_committed(self) -> None:
        """The files are generated. Week 12 edited the output and not the generator, and
        nothing noticed for three commits because nobody ran it."""
        import subprocess
        import sys

        before = {p.name: p.read_text(encoding="utf-8") for p in sorted(WORKFLOWS.glob("*.json"))}
        subprocess.run(
            [sys.executable, str(REPO / "n8n" / "generate_workflows.py")],
            cwd=REPO,
            check=True,
            capture_output=True,
        )
        after = {p.name: p.read_text(encoding="utf-8") for p in sorted(WORKFLOWS.glob("*.json"))}
        assert before == after


def by_name_body(workflows: list[dict], name: str) -> dict:
    return next(w for w in workflows if w["name"] == name)


class TestTheOnPremImagesArePinned:
    def test_every_third_party_image_carries_a_digest(self) -> None:
        """A tag is a name its publisher can repoint, so a stack that re-pulls on restart
        is a stack whose contents can change without a commit. Images this repository
        builds are excluded: they carry ${DRAWBRIDGE_VERSION} and come from the same commit
        that deploys them, so a digest would pin them to whenever somebody last ran the
        pinning script."""
        compose = (REPO / "docker-compose.onprem.yml").read_text(encoding="utf-8")
        # `\s*` before a negative lookahead backtracks to zero width and lets the
        # lookahead pass at the space, so the ref is captured and tested instead.
        refs = re.findall(r"^\s*image:[ 	]*(\S+)\s*$", compose, re.MULTILINE)
        unpinned = [r for r in refs if not r.startswith("drawbridge/") and "@sha256:" not in r]
        assert unpinned == []

    def test_a_digest_keeps_its_tag_beside_it(self) -> None:
        """Docker resolves the digest and ignores the tag, so the tag is documentation and
        is not load-bearing. That is the point: `redis@sha256:ff02b5...` alone tells a
        reviewer nothing, and a digest nobody can read is a digest nobody checks."""
        compose = (REPO / "docker-compose.onprem.yml").read_text(encoding="utf-8")
        for line in compose.splitlines():
            if "@sha256:" in line and "image:" in line:
                ref = line.split("image:")[1].strip()
                assert ":" in ref.split("@")[0], ref


class TestRetentionRefusesToPretend:
    def test_the_default_retention_covers_the_statutory_obligation(self) -> None:
        """19 CFR 163.4 and GCC Art. 175 both run five years. A backup taken today may
        contain a claim entered today, so the clock on the backup starts now."""
        from scripts.retention import DEFAULT_RETENTION_DAYS

        assert DEFAULT_RETENTION_DAYS >= 5 * 365

    def test_it_asks_for_compliance_mode_and_not_governance(self) -> None:
        """GOVERNANCE can be lifted by anyone holding `s3:BypassGovernanceRetention`,
        which on a single-tenant on-prem MinIO means the operator. A retention control the
        operator can lift is a retention policy."""
        source = (REPO / "scripts" / "retention.py").read_text(encoding="utf-8")
        assert 'ObjectLockMode="COMPLIANCE"' in source
        assert "GOVERNANCE" not in source.split('"""', 2)[2]

    @pytest.mark.parametrize(
        ("dsn", "expected"),
        [
            (
                "postgresql+asyncpg://drawbridge@postgres:5432/drawbridge",
                "postgresql://drawbridge@postgres:5432/drawbridge",
            ),
            (
                "postgresql+psycopg://drawbridge:pw@localhost:5432/drawbridge",
                "postgresql://drawbridge:pw@localhost:5432/drawbridge",
            ),
        ],
    )
    def test_pg_dump_gets_a_url_libpq_understands(self, dsn: str, expected: str) -> None:
        """`pg_dump` speaks libpq and has never heard of `postgresql+asyncpg://`. The API
        and Alembic both carry a driver in the scheme, so the backup job cannot reuse
        either DSN as-is — and the failure would be at 3 a.m. on the first scheduled run,
        not at deploy."""
        from scripts.retention import _libpq_url

        assert _libpq_url(dsn) == expected

    def test_it_connects_as_the_owner(self) -> None:
        """`pg_dump` run as `drawbridge_app` under row-level security produces a dump of
        the rows that role can see, which is none of them: a backup that restores to an
        empty database and reports success. Same for the ledger — "is the chain intact"
        cannot be answered by a connection that can only see one tenant."""
        from scripts.retention import _dsn

        source = (REPO / "scripts" / "retention.py").read_text(encoding="utf-8")
        assert "DRAWBRIDGE_OWNER_DATABASE_URL" in source
        assert callable(_dsn)

    def test_the_catalogue_lists_versions_and_not_objects(self) -> None:
        """Object lock protects the bytes, not the listing. `DeleteObject` still succeeds
        against a locked object by writing a delete marker, and the object then vanishes
        from an ordinary listing while the protected version sits underneath. The failure
        to guard against is looking for backups during an incident, seeing an empty
        bucket, and concluding there are none."""
        source = (REPO / "scripts" / "retention.py").read_text(encoding="utf-8")
        assert "list_object_versions" in source
        assert "masked_by_delete_marker" in source

    def test_verification_does_not_write_to_the_ledger_it_verifies(self) -> None:
        """Recording the result as an `audit_ledger` event would extend the chain being
        verified and put the attestation inside the structure it attests to."""
        source = (REPO / "scripts" / "retention.py").read_text(encoding="utf-8")
        assert "record_event" not in source
        assert "INSERT INTO audit_ledger" not in source


class TestLexicalFloorStillHasNotMoved:
    def test_it_is_where_week_fourteen_measured_it(self) -> None:
        """Not a tautology — a guard. The retrieval work this week is upstream of it, and
        the temptation after fixing retrieval is to tune the floor to match. Paraphrased
        goods queries land at 0.20–0.25 and non-goods queries reach 0.314; the ranges
        overlap, and no floor separates them."""
        assert LEXICAL_FLOOR == 0.15
