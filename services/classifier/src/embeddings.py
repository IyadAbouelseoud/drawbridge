"""Embedding backends for the tariff corpus.

Vector search is dark until the `embedding` column is populated, and populating it means
choosing a model. That choice is deferred to configuration rather than baked in, because a
corpus embedded with one model and queried with another produces confident nonsense —
cosine distance between two embedding spaces is meaningless rather than merely inaccurate.

Two backends ship:

**`HashingEmbedder`** — deterministic, offline, no model. It projects character trigrams
into a fixed vector space. This is *lexical similarity wearing a vector's clothing*: it
will match "portable data processing machine" to "portable automatic data processing
machines" and will not match "ruggedised field laptop" to either. That limitation is the
whole point of naming it honestly — a corpus embedded this way must not be presented as
semantic search, and `is_semantic` is False so callers can refuse to.

**`OllamaEmbedder`** — a local model endpoint, which is what makes vector search actually
earn its place. Local rather than hosted so a classification stays reproducible without a
third-party API being alive in five years, and so tenant document text never leaves the
deployment.

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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# Must match hts_models.EMBEDDING_DIM. Changing it is a migration, not a config change,
# because the HNSW index is built against the dimension.
EMBEDDING_DIM = 1536

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
    "hashing": HashingEmbedder,
    "ollama": OllamaEmbedder,
}


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
