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

    # Measured against the week 8 corpus, and provisional. This model's distances are
    # compressed into a narrow band: a good match sits around 0.52-0.63 and an unrelated
    # one around 0.86, so the separation is real but the margin is thin and the ceiling
    # sits closer to the noise than is comfortable. It was set from nine tariff lines,
    # which is enough to show the old 0.55 was wrong and not enough to call this right —
    # see ROADMAP.md, week 9. Erring high: a vector-only hit is flagged for analyst
    # confirmation rather than returned as an answer, so a loose ceiling costs a review
    # and a tight one loses the match entirely.
    vector_ceiling = 0.75

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
