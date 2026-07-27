"""ARI-OS Cortex — embedding backends (ollama | api | off).

Single public entry point :func:`embed` dispatches to one of three backends:

- ``ollama`` — local :class:`EmbedClient` against ``$OLLAMA_URL`` (default
  ``http://127.0.0.1:11434``); the heavy default per the design spec.
- ``api`` — remote provider (google) using :func:`ari_os.tools.ask.get_key`
  for key resolution (env → macOS Keychain ``com.ari-os.keys``), ``httpx`` for
  the request.
- ``off`` — returns ``None`` for every text. Retrieval still works via the
  lexical (FTS5) path; the brain is functional with no embedding server.

Pack/unpack helpers (:func:`pack_embedding`, :func:`unpack_embedding`) live
here too — they are the wire format for ``chunk_vec`` (sqlite-vec) and the
sidecar.
"""
from __future__ import annotations

import struct
import time
from typing import Callable

import httpx

from .config import EMBED_DIM, EMBED_MODEL, OLLAMA_URL

MAX_RETRIES = 3
BACKOFF_BASE = 0.5

# Embed-time size backstop. nomic-embed-text rejects inputs over its 2048-token
# context with HTTP 500 "the input length exceeds the context length". That is
# NOT transient — retrying the same text fails identically — so the response is
# truncated and retried instead. The chunker char-cap (chunker.MAX_CHUNK_CHARS)
# normally prevents this; this backstops the raw .jsonl segment path and
# adversarially dense content (~1 char/token) that slips past a char cap.
SHRINK_FACTOR = 0.6
MAX_SIZE_SHRINKS = 8
MIN_EMBED_CHARS = 200
# On the first size error, jump straight down to this ceiling rather than
# shrinking 0.6x at a time. A single .jsonl transcript turn can be ~1MB; pure
# geometric shrink would need many failed round-trips (and a fixed step count
# never converges). After this jump, geometric shrink fine-tunes if still over.
ABS_TRUNCATE_CHARS = 8000
_SIZE_ERROR_MARKERS = ("context length", "input length exceeds", "exceeds the context")

# Public surface
__all__ = [
    "EmbedError",
    "EmbedClient",
    "embed",
    "pack_embedding",
    "unpack_embedding",
]


def _is_size_error(resp: httpx.Response | None) -> bool:
    """True when a 500 means 'input too long' (truncate, don't retry as-is)."""
    if resp is None or resp.status_code != 500:
        return False
    try:
        body = resp.text.lower()
    except Exception:
        return False
    return any(m in body for m in _SIZE_ERROR_MARKERS)


class EmbedError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# ollama backend
# ---------------------------------------------------------------------------

class EmbedClient:
    """Ollama /api/embeddings client. One HTTP call per text; transient 5xx
    and dim-0 responses retry with backoff; oversized-input 500 truncates
    the text and re-issues (retrying unchanged would fail identically)."""

    def __init__(
        self,
        model: str = EMBED_MODEL,
        dim: int = EMBED_DIM,
        url: str = OLLAMA_URL,
        *,
        max_retries: int = MAX_RETRIES,
        backoff_base: float = BACKOFF_BASE,
        inter_request_delay: float = 0.05,
    ) -> None:
        self.model = model
        self.dim = dim
        self.url = url
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.inter_request_delay = inter_request_delay

    def _embed_one(self, text: str) -> list[float]:
        cur = text
        transient_attempts = 0
        size_shrinks = 0
        while True:
            try:
                r = httpx.post(
                    f"{self.url}/api/embeddings",
                    json={"model": self.model, "prompt": cur},
                    timeout=30.0,
                )
                r.raise_for_status()
            except httpx.HTTPStatusError as e:
                if _is_size_error(e.response):
                    if len(cur) > MIN_EMBED_CHARS and size_shrinks < MAX_SIZE_SHRINKS:
                        target = min(int(len(cur) * SHRINK_FACTOR), ABS_TRUNCATE_CHARS)
                        cur = cur[: max(MIN_EMBED_CHARS, target)]
                        size_shrinks += 1
                        continue
                    raise EmbedError(
                        f"Ollama embed failed: input too long after "
                        f"{size_shrinks} truncations: {e}"
                    ) from e
                if e.response.status_code >= 500 and transient_attempts < self.max_retries - 1:
                    transient_attempts += 1
                    time.sleep(self.backoff_base * (2 ** (transient_attempts - 1)))
                    continue
                raise EmbedError(f"Ollama embed call failed: {e}") from e
            except httpx.HTTPError as e:
                raise EmbedError(f"Ollama embed call failed: {e}") from e
            v = r.json().get("embedding", [])
            if len(v) != self.dim:
                if len(v) == 0 and transient_attempts < self.max_retries - 1:
                    transient_attempts += 1
                    time.sleep(self.backoff_base * (2 ** (transient_attempts - 1)))
                    continue
                raise EmbedError(
                    f"Ollama returned dim {len(v)}, expected {self.dim}. "
                    f"Wrong model selected?"
                )
            # Coerce to float: Ollama returns whole-number components as JSON
            # ints, yielding mixed int/float lists. SQLite (struct.pack) tolerates
            # that, but sqlite-vec's packed-row binding is strict. Normalizing
            # here keeps the write path consistent.
            return [float(x) for x in v]

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts via Ollama /api/embeddings (one call per text)."""
        out: list[list[float]] = []
        for i, t in enumerate(texts):
            out.append(self._embed_one(t))
            if self.inter_request_delay > 0 and i < len(texts) - 1:
                time.sleep(self.inter_request_delay)
        return out


# ---------------------------------------------------------------------------
# api backend (google embeddings via ask.get_key)
# ---------------------------------------------------------------------------

# Default model used by the `api` backend when the caller doesn't override.
# gemini-embedding-001 returns 768-d when the request sets outputDimensionality
# (see _api_embed_one), matching the schema's vec0 column. Override via
# ARI_OS_EMBED_MODEL env (already wired through .config.EMBED_MODEL).
# Note: text-embedding-004's embedContent endpoint was retired by Google
# (404 as of 2026-07) — do not revert to it.
API_DEFAULT_MODEL = "gemini-embedding-001"
_API_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"


def _api_embed_one(text: str, *, model: str, dim: int, timeout: float = 30.0) -> list[float]:
    """Embed one text via google's ``embedContent`` endpoint.

    Key resolution goes through :func:`ari_os.tools.ask.get_key`, which honors
    ``$GEMINI_API_KEY`` then the macOS Keychain item ``com.ari-os.keys`` —
    same plumbing as the rest of ARI-OS's provider surface.
    """
    from ari_os.tools.ask import get_key  # late import: ask is a sibling tool
    key = get_key("google")
    url = _API_ENDPOINT.format(model=model)
    body = {
        "model": f"models/{model}",
        "content": {"parts": [{"text": text}]},
        "outputDimensionality": dim,
    }
    try:
        r = httpx.post(url, json=body, timeout=timeout,
                       headers={"x-goog-api-key": key})
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise EmbedError(f"api embed call failed: {e}") from e
    payload = r.json()
    try:
        values = payload["embedding"]["values"]
    except (KeyError, TypeError) as e:
        raise EmbedError(f"api embed returned unexpected payload: {payload!r}") from e
    if len(values) != dim:
        raise EmbedError(
            f"api embed returned dim {len(values)}, expected {dim}"
        )
    return [float(x) for x in values]


def _api_embed_batch(
    texts: list[str], *, model: str, dim: int, timeout: float = 30.0
) -> list[list[float]]:
    """One request per text — google's ``embedContent`` is a single-string RPC.

    (The ``batchEmbedContents`` variant is deliberately not used: keeping one
    text per request means a size-cap failure is recoverable the same way the
    ollama path is, and the spec keeps the backends symmetric.)"""
    return [
        _api_embed_one(t, model=model, dim=dim, timeout=timeout)
        for t in texts
    ]


# ---------------------------------------------------------------------------
# public dispatch
# ---------------------------------------------------------------------------

def embed(
    texts: list[str],
    *,
    backend: str = "off",
    model: str | None = None,
    dim: int = EMBED_DIM,
    url: str = OLLAMA_URL,
    embed_client: "EmbedClient | None" = None,
    api_call: "Callable[[str, str, int, float], list[float]] | None" = None,
) -> list[list[float] | None]:
    """Public entry point. Returns one entry per input text.

    - ``backend='off'`` (default) — returns ``[None, None, ...]``. Retrieval
      still works via the lexical (FTS5) path; the brain is fully functional
      with no embedding server installed.
    - ``backend='ollama'`` — instantiates (or reuses, if ``embed_client`` is
      passed) an :class:`EmbedClient` and embeds via Ollama.
    - ``backend='api'`` — calls the google embeddings endpoint using the
      same key plumbing as :mod:`ari_os.tools.ask`.

    ``api_call`` is an injection seam for tests: when provided, it overrides
    the network call. Signature: ``(text, model, dim, timeout) -> list[float]``.
    """
    backend = backend.lower()
    if backend == "off":
        return [None] * len(texts)
    if backend == "ollama":
        client = embed_client or EmbedClient(
            model=model or EMBED_MODEL, dim=dim, url=url,
        )
        return client.embed(texts)
    if backend == "api":
        fn = api_call or _api_embed_one
        out: list[list[float] | None] = []
        for t in texts:
            v = fn(t, model=model or API_DEFAULT_MODEL, dim=dim, timeout=30.0)
            if v is not None and len(v) != dim:
                raise EmbedError(
                    f"api embed returned dim {len(v)}, expected {dim}"
                )
            out.append(v)
        return out
    raise EmbedError(f"unknown embed backend: {backend!r}")


# ---------------------------------------------------------------------------
# pack / unpack (wire format for chunk_vec)
# ---------------------------------------------------------------------------

def pack_embedding(v: list[float]) -> bytes:
    if len(v) != EMBED_DIM:
        raise ValueError(f"expected {EMBED_DIM}-d vector, got {len(v)}")
    return struct.pack(f"{EMBED_DIM}f", *v)


def unpack_embedding(raw: bytes) -> list[float]:
    return list(struct.unpack(f"{EMBED_DIM}f", raw))


# ---------------------------------------------------------------------------
# backend selection (local patch: wire `cortex.embeddings` config into the
# default client; upstream hardcodes the Ollama EmbedClient at call sites)
# ---------------------------------------------------------------------------

class NullEmbedClient:
    """No-op embedder: retrieval degrades to the lexical (FTS5) path."""

    def embed(self, texts: list[str]) -> list[None]:
        return [None] * len(texts)


class ApiEmbedClient:
    """Google embedContent client with the EmbedClient interface."""

    def __init__(self, model: str = API_DEFAULT_MODEL, dim: int = EMBED_DIM) -> None:
        self.model = model
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [
            _api_embed_one(t, model=self.model, dim=self.dim)
            for t in texts
        ]


def _google_key_available() -> bool:
    """Gentle key probe: env then keychain. Never exits, never prints."""
    import os as _os
    import subprocess as _sp
    if _os.environ.get("GEMINI_API_KEY"):
        return True
    try:
        r = _sp.run(
            ["security", "find-generic-password",
             "-s", "com.ari-os.keys", "-a", "GEMINI_API_KEY", "-w"],
            capture_output=True, text=True, timeout=5)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def _ollama_reachable(url: str = OLLAMA_URL) -> bool:
    try:
        httpx.get(f"{url.rstrip('/')}/api/tags", timeout=0.3)
        return True
    except Exception:
        return False


def default_embed_client():
    """Resolve the embed client from ``cortex.embeddings`` config.

    google -> ApiEmbedClient (Null if no key yet: lexical-only, no noise)
    ollama -> EmbedClient · off -> NullEmbedClient
    auto/unset -> ollama if reachable, else google if key, else Null
    """
    from .config import _config_value
    pref = (_config_value("cortex.embeddings") or "auto").strip().lower()
    if pref == "off":
        return NullEmbedClient()
    if pref == "ollama":
        return EmbedClient()
    if pref in ("google", "api"):
        return ApiEmbedClient() if _google_key_available() else NullEmbedClient()
    # auto
    if _ollama_reachable():
        return EmbedClient()
    if _google_key_available():
        return ApiEmbedClient()
    return NullEmbedClient()
