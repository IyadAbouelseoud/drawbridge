"""Embedding backends for the tariff corpus.

Vector search is dark until the `embedding` column is populated, and populating it means
choosing a model. That choice is deferred to configuration rather than baked in, because a
corpus embedded with one model and queried with another produces confident nonsense —
cosine distance between two embedding spaces is meaningless rather than merely inaccurate.

Three backends ship:

**`HashingEmbedder`** — deterministic, offline, no model. It projects character trigrams
into a fixed vector space. This is *lexical similarity wearing a vector's clothing*: it
will match "portable data processing machine" to "portable automatic data processing
machines" and will not match "ruggedised field laptop" to either. That limitation is the
whole point of naming it honestly — a corpus embedded this way must not be presented as
semantic search, and `is_semantic` is False so callers can refuse to.

**`FastEmbedEmbedder`** — the default from week 8, and the one that makes vector search
earn its place. A quantised ONNX sentence-transformer running on CPU in-process: no
server to keep alive, no API key, no per-token cost. The default model is multilingual
because half this corpus is Arabic — an English-only encoder would leave the ZATCA
schedule embedded in a space where its own descriptions do not retrieve it. It also
retrieves *across* the language boundary, which is the property the dual-jurisdiction
design actually needs: an Arabic Bayan description reaches the English USITC line.

**`OllamaEmbedder`** — a local model endpoint, for a deployment that already runs one or
that wants a larger encoder than fits comfortably in-process. Local rather than hosted so
a classification stays reproducible without a third-party API being alive in five years,
and so tenant document text never leaves the deployment.

Every backend records `model_id` on the rows it writes. A corpus is queryable only by the
backend that wrote it, and mixing them is a data error the ingest can detect.

**Rerankers are here too, and they are a different kind of object.** An `Embedder` maps
one text to a point, so a corpus can be embedded once and searched forever. A `Reranker`
scores a *pair* — this query against this document — which cannot be precomputed and
cannot be indexed: the cost is one model call per candidate, at query time. That is why
it is a second stage over a shortlist rather than a replacement for the first, and why
`Reranker.score` takes the query text that `Embedder.embed` never sees.
"""

from __future__ import annotations

import hashlib
import json
import math
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

# Must match hts_models.EMBEDDING_DIM. Changing it is a migration, not a config change,
# because the HNSW index is built against the dimension. 384 is the width of the default
# fastembed model; migration a7c31f9d4e60 moved the column down from 1536.
EMBEDDING_DIM = 384

# Multilingual by necessity rather than preference. The corpus is half Arabic, and an
# English-only encoder does not merely score Arabic badly — it has no useful geometry for
# it at all, so every ZATCA line would sit at an arbitrary point.
DEFAULT_FASTEMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Character n-gram width for the hashing backend. Three is the standard trigram width and
# matches what the pg_trgm index already uses on the same text, so the two paths agree
# about what "similar" means rather than disagreeing subtly.
_NGRAM = 3


class EmbeddingError(RuntimeError):
    """The backend could not produce a vector.

    Raised rather than returning a zero vector: a zero vector is a valid input to cosine
    distance and would silently sit at maximum distance from everything, which reads as
    "no match" instead of "no answer".
    """


class Embedder(ABC):
    """One embedding backend."""

    model_id: str
    dimension: int = EMBEDDING_DIM

    vector_ceiling: float = 0.55
    """Cosine distance above which a vector hit is noise.

    A property of the backend, not of the search, because the number means nothing
    outside one embedding space. Two models can both retrieve correctly and disagree
    completely about what distance a good match sits at — so a ceiling tuned against one
    and applied to another does not degrade gracefully, it rejects everything or accepts
    everything. Week 8 found exactly that: a ceiling calibrated for the trigram backend
    silently suppressed every semantic hit after the model changed.
    """

    @property
    def is_semantic(self) -> bool:
        """Whether this backend can match a paraphrase.

        False for lexical backends. A caller building an analyst-facing "semantic search"
        surface should refuse a backend that answers False rather than shipping a feature
        that silently does not work.
        """
        return True

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Vectors for a batch of texts, in the order given.

        Batched rather than one-at-a-time because a real backend amortises a round trip
        across the batch, and a 19,000-line schedule embedded one call at a time is an
        overnight job instead of a coffee break.
        """


def _normalise(vector: list[float]) -> list[float]:
    """Scale to unit length.

    pgvector's `<=>` is cosine distance, which is invariant to magnitude — but normalising
    at write time makes the stored vectors directly comparable by dot product too, and
    keeps a badly-scaled backend from being the reason a search looks broken.
    """
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0:
        msg = "backend produced a zero vector, which has no direction to compare"
        raise EmbeddingError(msg)
    return [component / norm for component in vector]


class HashingEmbedder(Embedder):
    """Deterministic character-trigram hashing. No model, no network, no training.

    Useful for three things and dishonest about the fourth: it makes the whole ingest and
    search path exercisable end-to-end, it is reproducible forever, it costs nothing — and
    it cannot match a paraphrase. `is_semantic` is False.
    """

    model_id = "hashing-trigram-v1"

    # Trigram overlap collapses fast: a shared phrase lands well under 0.4, and unrelated
    # text sits near 1.0. The gap is wide, so the ceiling can sit in the middle.
    vector_ceiling = 0.55

    def __init__(self, dimension: int = EMBEDDING_DIM) -> None:
        self.dimension = dimension

    @property
    def is_semantic(self) -> bool:
        return False

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        cleaned = " ".join(text.lower().split())
        if not cleaned:
            msg = "cannot embed empty text"
            raise EmbeddingError(msg)

        vector = [0.0] * self.dimension
        padded = f" {cleaned} "
        for index in range(len(padded) - _NGRAM + 1):
            gram = padded[index : index + _NGRAM]
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimension
            # Sign from an independent byte range, so a common trigram does not push every
            # document in the same direction and collapse the space onto one axis.
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign
        return _normalise(vector)


class FastEmbedEmbedder(Embedder):
    """A quantised ONNX sentence-transformer, in-process on CPU.

    This is the backend that makes vector search mean something. `is_semantic` is True
    and earns it: "ruggedised field laptop" reaches *portable automatic data processing
    machines* with no lexical overlap at all, which is precisely the query the hashing
    backend cannot answer and precisely the query an analyst actually types.

    **Cost.** Nothing per call — no API, no server, no key. The model weights are fetched
    once from HuggingFace and cached on disk; after that the process is offline. That one
    download is the only network dependency, and `cache_dir` exists so a deployment can
    pre-seed it and run air-gapped.

    **Model choice.** The default is multilingual, not because multilingual is generally
    better but because this corpus is half Arabic. An English-only encoder such as
    `BAAI/bge-small-en-v1.5` is a legitimate choice for a US-only deployment and is
    smaller and slightly sharper on English; it is the wrong choice here, because the
    ZATCA schedule would be embedded into a space that does not represent it.

    **Construction is lazy.** Building the model means possibly downloading a few hundred
    megabytes, which should happen when someone asks for a vector, not as a side effect of
    importing a config module or listing the available backends.
    """

    # Calibrated in week 9 against tests/fixtures/tariff_benchmark.json — twenty labelled
    # queries, ten the corpus answers and ten it does not.
    #
    # The measurement retired the assumption the week 8 value rested on. There is no
    # ceiling that admits every correct answer and rejects every wrong one: the worst true
    # positive sits at 0.624 and the nearest hard negative at 0.508, so the two ranges
    # overlap and the separation the old comment described does not exist at this
    # granularity. A ceiling cannot deliver precision, and 0.75 was quietly admitting
    # eight of the ten negatives while appearing to.
    #
    # So the ceiling is calibrated for *recall* and nothing else: 0.68 is the smallest
    # value that still retrieves the correct code for all ten positives (worst case
    # 0.624), with headroom before the rubbish band — "live breeding cattle" and "marine
    # cargo insurance" sit at 0.74 and 0.80 and stay excluded. Everything admitted below
    # it is a candidate, not an answer. Precision is `search.CONFIRMATION_LEXICAL_FLOOR`'s
    # job, and it is measured separately.
    #
    # **Week 16 did not move it, and changed what it is for.** With a reranker attached
    # this is no longer the gate on what an analyst is shown — it is the gate on what the
    # cross-encoder is allowed to consider, applied before the second stage runs. A
    # candidate cut here cannot be rescued, and rescuing distant candidates is exactly
    # what the second stage turned out to do: the correct subheading for "ruggedised field
    # laptop computer" sits at distance 0.601 and position 28 in the shortlist, and the
    # reranker lifts it to 6. Across the ten benchmark positives the worst correct
    # candidate anywhere in the depth-50 pool is that 0.601, so 0.68 clears it by 0.079 and
    # any value below about 0.61 is now demonstrably wrong rather than merely tight.
    # Raising it further has no measured benefit and admits more noise into a stage that
    # costs two seconds a query, so it stays where week 9 put it — for a reason week 9
    # could not have had.
    vector_ceiling = 0.68

    def __init__(
        self,
        model: str = DEFAULT_FASTEMBED_MODEL,
        dimension: int = EMBEDDING_DIM,
        cache_dir: str | None = None,
        threads: int | None = None,
    ) -> None:
        self.model = model
        self.model_id = f"fastembed:{model}"
        self.dimension = dimension
        self.cache_dir = cache_dir
        self.threads = threads
        self._encoder: Any | None = None

    def _load(self) -> Any:
        if self._encoder is not None:
            return self._encoder
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - depends on the install extra
            msg = (
                "fastembed is not installed; it ships in the 'embed' extra (uv sync --extra embed)"
            )
            raise EmbeddingError(msg) from exc

        try:
            self._encoder = TextEmbedding(
                model_name=self.model,
                cache_dir=self.cache_dir,
                threads=self.threads,
            )
        except Exception as exc:
            # Almost always a first-run download that could not reach HuggingFace. Worth
            # naming, because the failure otherwise looks like a broken model rather than
            # a missing one.
            msg = (
                f"could not load fastembed model {self.model!r}: {exc}. If this is a "
                "first run, it needs one-time network access to fetch the weights; "
                "afterwards set cache_dir to run offline."
            )
            raise EmbeddingError(msg) from exc
        return self._encoder

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        cleaned = [" ".join(text.split()) for text in texts]
        if any(not text for text in cleaned):
            msg = "cannot embed empty text"
            raise EmbeddingError(msg)

        encoder = self._load()
        vectors = [[float(component) for component in row] for row in encoder.embed(cleaned)]

        if len(vectors) != len(cleaned):
            msg = (
                f"fastembed returned {len(vectors)} vectors for {len(cleaned)} texts; "
                "the batch cannot be aligned back to its rows"
            )
            raise EmbeddingError(msg)
        for vector in vectors:
            if len(vector) != self.dimension:
                msg = (
                    f"model {self.model} returns {len(vector)} dimensions but the corpus "
                    f"column is {self.dimension}; embedding it would write into a space "
                    "the index cannot search"
                )
                raise EmbeddingError(msg)
        return [_normalise(vector) for vector in vectors]


class OllamaEmbedder(Embedder):
    """A local Ollama embedding endpoint.

    Local by choice. A classification that reached a filing must be reproducible years
    later, and a hosted embedding API is a dependency on someone else's uptime and
    versioning; it also means tenant document text leaving the deployment, which the
    recordkeeping posture does not want.

    The dimension is *asserted* against the first response rather than trusted from
    config: a model whose output width differs from the column is a silent write failure
    at best, and at worst a corpus half in one space and half in another.
    """

    def __init__(
        self,
        model: str = "nomic-embed-text",
        base_url: str = "http://localhost:11434",
        dimension: int = EMBEDDING_DIM,
        timeout: float = 120.0,
    ) -> None:
        self.model = model
        self.model_id = f"ollama:{model}"
        self.base_url = base_url.rstrip("/")
        self.dimension = dimension
        self.timeout = timeout

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vectors.append(self._embed_one(text))
        return vectors

    def _embed_one(self, text: str) -> list[float]:
        payload = json.dumps({"model": self.model, "prompt": text}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/embeddings",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError) as exc:
            msg = f"Ollama at {self.base_url} did not answer: {exc}"
            raise EmbeddingError(msg) from exc

        vector = body.get("embedding")
        if not vector:
            msg = f"Ollama returned no embedding for model {self.model}"
            raise EmbeddingError(msg)
        if len(vector) != self.dimension:
            msg = (
                f"model {self.model} returns {len(vector)} dimensions but the corpus "
                f"column is {self.dimension}; embedding it would write into a space the "
                "index cannot search"
            )
            raise EmbeddingError(msg)
        return _normalise([float(component) for component in vector])


#: The default cross-encoder. Multilingual, and that is a requirement rather than a
#: preference — the same reason `DEFAULT_FASTEMBED_MODEL` is multilingual. Week 15 measured
#: `Xenova/ms-marco-MiniLM-L-6-v2`, an English cross-encoder, over this bilingual corpus and
#: it moved the Arabic smartphone query from rank 1 to rank 12. An English reranker does not
#: score Arabic badly; it has no basis for scoring it at all, so the order it returns is
#: arbitrary rather than merely wrong.
#:
#: Week 16 also measured `BAAI/bge-reranker-base`, which is multilingual and a third of the
#: latency. It did not help: top-10 stayed at 6/10 and rank-1 fell from 3 to 2. Being
#: multilingual is necessary and not sufficient.
DEFAULT_RERANK_MODEL = "jinaai/jina-reranker-v2-base-multilingual"


class Reranker(ABC):
    """One cross-encoder, scoring a query against a candidate document.

    Deliberately not an `Embedder`. An embedder answers "where does this text sit", once
    per document, and the answer survives in a column. A reranker answers "does this
    document answer this query", which is a property of the pair and therefore has no
    column to live in — it is recomputed on every search, for every candidate. The whole
    design consequence follows from that: the shortlist has to be short.
    """

    model_id: str

    @abstractmethod
    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        """Relevance logits for each document against `query`, in the order given.

        Logits, not probabilities, because that is what a cross-encoder emits and
        squashing inside the backend would throw away the thing the raw scale is good
        for — comparing two candidates. `confidence()` does the squashing where a bounded
        number is actually wanted.
        """


def confidence(logit: float) -> float:
    """A reranker logit as a 0–1 number that can be blended with a similarity.

    The logistic, which is the function the cross-encoder was trained under, so this
    recovers the probability the model was fitted to emit rather than imposing a new scale
    on it.

    Blending needs this because the two signals being combined are not commensurable:
    cosine similarity is bounded in [0, 1] by construction and a logit is unbounded in both
    directions. Weighting an unbounded score against a bounded one does not produce a
    weighted average, it produces whichever number happened to be larger.
    """
    if logit >= 0:
        return 1.0 / (1.0 + math.exp(-logit))
    # The same value, arranged so the exponent is never large and positive. exp(800)
    # overflows, and a large negative logit is exactly what a confidently-rejected
    # candidate produces — which is the common case in a shortlist of fifty.
    exponent = math.exp(logit)
    return exponent / (1.0 + exponent)


class FastEmbedReranker(Reranker):
    """A quantised ONNX cross-encoder, in-process on CPU.

    The same shape as `FastEmbedEmbedder` and for the same reasons: no server, no key, no
    per-token cost, weights cached on disk after one download, and `cache_dir` so a
    deployment can pre-seed it and run air-gapped.

    **The cost is real and it is per candidate.** Roughly two seconds to score fifty
    candidates on CPU, against about twenty milliseconds for the pgvector query that
    produced them. A reranker is affordable over a shortlist and ruinous over a corpus,
    which is the entire reason the pipeline retrieves first.

    **Construction is lazy** — a gigabyte of weights should be fetched when someone asks
    for a score, not as a side effect of importing a config module.
    """

    def __init__(
        self,
        model: str = DEFAULT_RERANK_MODEL,
        cache_dir: str | None = None,
        threads: int | None = None,
    ) -> None:
        self.model = model
        self.model_id = f"fastembed-ce:{model}"
        self.cache_dir = cache_dir
        self.threads = threads
        self._encoder: Any | None = None

    def _load(self) -> Any:
        if self._encoder is not None:
            return self._encoder
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError as exc:  # pragma: no cover - depends on the install extra
            msg = (
                "fastembed is not installed; it ships in the 'embed' extra (uv sync --extra embed)"
            )
            raise EmbeddingError(msg) from exc
        try:
            self._encoder = TextCrossEncoder(
                model_name=self.model,
                cache_dir=self.cache_dir,
                threads=self.threads,
            )
        except Exception as exc:
            msg = (
                f"could not load cross-encoder {self.model!r}: {exc}. If this is a "
                "first run, it needs one-time network access to fetch the weights; "
                "afterwards set cache_dir to run offline."
            )
            raise EmbeddingError(msg) from exc
        return self._encoder

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        if not documents:
            return []
        cleaned = [" ".join(document.split()) for document in documents]
        if any(not document for document in cleaned):
            msg = "cannot rerank against empty document text"
            raise EmbeddingError(msg)

        scores = [float(value) for value in self._load().rerank(query, cleaned)]
        if len(scores) != len(cleaned):
            msg = (
                f"cross-encoder returned {len(scores)} scores for {len(cleaned)} "
                "documents; the batch cannot be aligned back to its candidates"
            )
            raise EmbeddingError(msg)
        return scores


RERANKERS: dict[str, type[Reranker]] = {"fastembed": FastEmbedReranker}


def build_reranker(name: str = "fastembed", **kwargs: object) -> Reranker:
    """Construct a reranker by name. Raises on an unknown one, exactly as `build` does."""
    try:
        factory = RERANKERS[name]
    except KeyError:
        msg = f"unknown reranker backend {name!r}; known backends are {sorted(RERANKERS)}"
        raise KeyError(msg) from None
    return factory(**kwargs)


BACKENDS: dict[str, type[Embedder]] = {
    "fastembed": FastEmbedEmbedder,
    "hashing": HashingEmbedder,
    "ollama": OllamaEmbedder,
}

# What a deployment gets when it does not say. `fastembed` rather than `hashing` because
# a corpus embedded lexically and presented as semantic search is the failure this module
# exists to prevent, and a default is the most likely thing to go unexamined.
DEFAULT_BACKEND = "fastembed"


def build(name: str, **kwargs: object) -> Embedder:
    """Construct a backend by name.

    Raises on an unknown name rather than defaulting. Defaulting would let a typo in a
    deployment config silently embed a whole corpus with the lexical backend, which
    answers — and answers badly — without saying so.
    """
    try:
        factory = BACKENDS[name]
    except KeyError:
        msg = f"unknown embedding backend {name!r}; known backends are {sorted(BACKENDS)}"
        raise KeyError(msg) from None
    return factory(**kwargs)


def to_pgvector(vector: Sequence[float]) -> str:
    """pgvector's literal text form.

    Passed as text and cast in SQL so the driver does not have to know about the type,
    matching how `services/classifier/src/search.py` already sends query vectors.
    """
    return "[" + ",".join(f"{float(component):.7g}" for component in vector) + "]"
