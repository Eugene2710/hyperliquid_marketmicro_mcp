"""Runtime configuration for the hlmcp venue layer.

A single immutable :class:`HLConfig` carries every operational knob the
Hyperliquid REST adapter needs: the endpoint URL, the concurrency cap, the
token-bucket rate-limit parameters, the per-request timeout, and the HIP-3
``perpDexs`` cache TTL. :func:`load_config` builds one from environment
variables (with ``.env`` support via ``python-dotenv``), falling back to the
spike-derived defaults below.

Provenance of the defaults (see ``docs/architecture.md`` "Operating envelope"):

- ``max_concurrency = 20`` — **[measured]** Q5: 20 concurrent ``clearinghouseState``
  calls finished in ~150ms with zero throttling.
- ``sustained_rate_per_sec = 7`` — **[derived]** 70% of HL's documented 10 req/sec
  ceiling; Q4 confirmed 7/sec for 30s is error-free.
- ``burst_capacity = 15`` — **[estimate, LOW confidence]** an engineering guess at a
  typical fan-out size; flagged in architecture.md "Open questions". Kept a tunable
  parameter precisely because it is not measured — could reasonably be 10 or 20.
- ``request_timeout_s = 4.0`` — **[derived]** ~3-5x the measured p99 (Q3) so slow-but-
  successful requests are not killed while dead ones still fail fast.
- ``dex_cache_ttl_s = 300`` — the ``perpDexs`` list changes only when a new HIP-3
  deployer comes online (rare), so a 5-minute cache is safe (Q2c).

This module is PURE configuration: no I/O beyond reading the environment in
:func:`load_config`.
"""

import os
from dataclasses import dataclass, field, fields

from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Spike-derived defaults (each tagged with provenance in the module docstring) #
# --------------------------------------------------------------------------- #

DEFAULT_BASE_URL: str = "https://api.hyperliquid.xyz/info"
"""HL public info endpoint. Read-only; no auth."""

DEFAULT_MAX_CONCURRENCY: int = 20
"""Max in-flight requests (asyncio.Semaphore). [measured] Q5."""

DEFAULT_SUSTAINED_RATE_PER_SEC: float = 7.0
"""Token-bucket refill rate, req/sec. [derived] 70% of the 10/sec ceiling, Q4."""

DEFAULT_BURST_CAPACITY: float = 15.0
"""Token-bucket max size: instant requests before throttling. [estimate, LOW conf]."""

DEFAULT_REQUEST_TIMEOUT_S: float = 4.0
"""Per-request HTTP timeout, seconds. [derived] ~3-5x measured p99, Q3."""

DEFAULT_DEX_CACHE_TTL_S: float = 300.0
"""perpDexs (HIP-3 discovery) cache TTL, seconds. The list changes rarely (Q2c)."""


def _parse_sample_wallets(raw: str | None) -> tuple[str, ...]:
    """Parse the ``HL_SAMPLE_WALLETS`` env var into a tuple of addresses.

    Mechanism: split a comma-separated string, strip surrounding whitespace from
    each entry, and drop empties. Addresses are NOT normalized here — that is the
    venue's job (it normalizes every wallet before sending); this only tokenizes
    the env var.

    Args:
        raw: The raw env-var value, or ``None`` if unset.

    Returns:
        A tuple of non-empty address strings (possibly empty if ``raw`` is unset
        or blank).
    """
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class HLConfig:
    """Immutable runtime configuration for :class:`~hlmcp.venues.hyperliquid.HyperliquidPublic`.

    Frozen so it can be shared freely across coroutines without risk of mutation.
    Construct directly for tests (override only what you need) or via
    :func:`load_config` to pull from the environment.

    Attributes:
        base_url: HL info endpoint URL (read-only, no auth).
        max_concurrency: Max simultaneous in-flight requests; enforced by an
            ``asyncio.Semaphore`` in the adapter's ``_post``.
        sustained_rate_per_sec: Token-bucket refill rate (requests/second) — the
            sustained throughput the limiter throttles to after a burst.
        burst_capacity: Token-bucket capacity — how many requests fire instantly
            before throttling to ``sustained_rate_per_sec``. LOW-confidence
            estimate; tune against observed usage.
        request_timeout_s: Per-request HTTP timeout in seconds.
        dex_cache_ttl_s: How long the ``perpDexs`` (HIP-3 discovery) result is
            cached before a refetch, in seconds.
        sample_wallets: Sample wallet addresses from ``HL_SAMPLE_WALLETS``, used
            by integration tests and (later) as default tool inputs. NOT
            normalized — the venue normalizes before sending.
    """

    base_url: str = DEFAULT_BASE_URL
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    sustained_rate_per_sec: float = DEFAULT_SUSTAINED_RATE_PER_SEC
    burst_capacity: float = DEFAULT_BURST_CAPACITY
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S
    dex_cache_ttl_s: float = DEFAULT_DEX_CACHE_TTL_S
    sample_wallets: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Validate the envelope so a misconfigured env var fails loudly at load.

        Raises:
            ValueError: If any numeric knob is non-positive (a zero rate or
                capacity would deadlock the limiter; a zero concurrency cap would
                deadlock the semaphore).
        """
        if self.max_concurrency < 1:
            raise ValueError(f"max_concurrency must be >= 1, got {self.max_concurrency}")
        if self.sustained_rate_per_sec <= 0:
            raise ValueError(
                f"sustained_rate_per_sec must be > 0, got {self.sustained_rate_per_sec}"
            )
        if self.burst_capacity < 1:
            raise ValueError(f"burst_capacity must be >= 1, got {self.burst_capacity}")
        if self.request_timeout_s <= 0:
            raise ValueError(f"request_timeout_s must be > 0, got {self.request_timeout_s}")
        if self.dex_cache_ttl_s < 0:
            raise ValueError(f"dex_cache_ttl_s must be >= 0, got {self.dex_cache_ttl_s}")


def load_config() -> HLConfig:
    """Build an :class:`HLConfig` from environment variables, with ``.env`` support.

    Mechanism: load ``.env`` (if present) into ``os.environ`` via
    ``python-dotenv``, then read each ``HL_*`` override, falling back to the
    module-level ``DEFAULT_*`` constants when a variable is unset. Numeric
    overrides are parsed with the relevant constructor (``int``/``float``);
    ``__post_init__`` then validates the assembled envelope.

    Recognized environment variables (all optional):

    - ``HL_BASE_URL`` — endpoint URL
    - ``HL_MAX_CONCURRENCY`` — int
    - ``HL_SUSTAINED_RATE_PER_SEC`` — float
    - ``HL_BURST_CAPACITY`` — float
    - ``HL_REQUEST_TIMEOUT_S`` — float
    - ``HL_DEX_CACHE_TTL_S`` — float
    - ``HL_SAMPLE_WALLETS`` — comma-separated addresses

    Returns:
        A validated :class:`HLConfig`.

    Raises:
        ValueError: If a numeric env var is present but unparseable, or if the
            assembled config fails :meth:`HLConfig.__post_init__` validation.
    """
    load_dotenv()

    def _float(name: str, default: float) -> float:
        raw: str | None = os.environ.get(name)
        if raw is None or not raw.strip():
            return default
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be a number, got {raw!r}") from exc

    def _int(name: str, default: int) -> int:
        raw: str | None = os.environ.get(name)
        if raw is None or not raw.strip():
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer, got {raw!r}") from exc

    return HLConfig(
        base_url=os.environ.get("HL_BASE_URL") or DEFAULT_BASE_URL,
        max_concurrency=_int("HL_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY),
        sustained_rate_per_sec=_float("HL_SUSTAINED_RATE_PER_SEC", DEFAULT_SUSTAINED_RATE_PER_SEC),
        burst_capacity=_float("HL_BURST_CAPACITY", DEFAULT_BURST_CAPACITY),
        request_timeout_s=_float("HL_REQUEST_TIMEOUT_S", DEFAULT_REQUEST_TIMEOUT_S),
        dex_cache_ttl_s=_float("HL_DEX_CACHE_TTL_S", DEFAULT_DEX_CACHE_TTL_S),
        sample_wallets=_parse_sample_wallets(os.environ.get("HL_SAMPLE_WALLETS")),
    )


# Exported so callers can introspect the config surface (e.g. for docs/CLI help)
# without depending on dataclasses internals.
CONFIG_FIELD_NAMES: tuple[str, ...] = tuple(f.name for f in fields(HLConfig))
"""Names of every :class:`HLConfig` field, in declaration order."""
