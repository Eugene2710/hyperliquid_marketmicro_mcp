"""Raw Hyperliquid API response shapes.

These ``HL*`` models mirror the Hyperliquid info-endpoint responses **exactly**.
Two rules govern everything in this module:

1. **Numerics stay strings.** The HL API serializes all monetary/size/price
   values as decimal *strings* to preserve precision. We keep them as ``str``
   here and parse to ``float``/``Decimal`` only in the user-facing response layer
   (``schemas/responses.py``). Nothing is computed or coerced here.
2. **Mirror, don't interpret.** A parse failure against these models means the
   API shape changed — that is the signal we want, not something to paper over.

Provenance: validated against recorded fixtures in ``tests/fixtures/`` captured
from the public ``https://api.hyperliquid.xyz/info`` endpoint (see
``docs/api_spike_findings.md`` Q1/Q2/Q2c).

Forward-compatibility: models use Pydantic's default ``extra="ignore"`` so that
new fields HL adds (it does — see ``HLPerpDex``) do not break parsing. Fields we
have *observed* are declared explicitly; genuinely unobserved shapes are flagged
in comments where the schema is most likely to need extension.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

# --------------------------------------------------------------------------- #
# clearinghouseState                                                          #
# --------------------------------------------------------------------------- #


class Leverage(BaseModel):
    """Leverage configuration for a single position.

    Hyperliquid runs each position in one of two margin modes:

    - ``cross``: the position shares one collateral pool with all other
      cross-margin positions; their PnL defends against each other's losses.
    - ``isolated``: the position has its own walled-off margin and cannot draw
      on the rest of the account if it is losing.

    Attributes:
        type: The margin mode for this position (``"cross"`` or ``"isolated"``).
        value: The position's current leverage multiplier (e.g. 5, 10, 20).
            This is the *position's* leverage, distinct from the symbol ceiling
            ``HLPosition.maxLeverage``.

    Note (unobserved shape): every recorded fixture is ``cross``. Isolated-mode
    leverage is reported to also carry a ``rawUsd`` string (the isolated margin
    allocation). We have not captured it, so it is not declared; ``extra=ignore``
    means an isolated position still parses (the extra field is dropped). Revisit
    when an isolated fixture is available.
    """

    type: Literal["cross", "isolated"] = Field(
        description="Margin mode: 'cross' (shared pool) or 'isolated' (walled-off)."
    )
    value: int = Field(description="Position's current leverage multiplier, e.g. 5, 10, 20.")


class CumFunding(BaseModel):
    """Cumulative funding on a position, in USD (positive = paid, negative = received).

    The sign is a COST: it is the negation of the cash flow in HL's
    ``userFunding`` ledger, whose ``usdc`` delta is negative when funding is paid.
    Verified live 2026-10-06 (api_spike_findings.md, Funding → Sign conventions):
    8/8 longs paying at a positive rate showed positive ``sinceChange``, and a
    short that received +26.230339 USD in its one settlement since the last size
    change showed ``sinceChange`` = -26.230339.

    All values are USD amounts serialized as strings. The three timeframes have
    distinct reset semantics:

    - ``allTime``: total funding accrued on this coin across the wallet's entire
      history; persists across close/reopen cycles.
    - ``sinceOpen``: funding accrued since the *current* position was opened
      (size went 0 → non-zero).
    - ``sinceChange``: funding accrued since the *last size change* (any add or
      trim resets it).

    A wallet that closed and reopened a position will have ``allTime`` differ
    from ``sinceOpen``.

    Attributes:
        allTime: Lifetime funding for this coin (USD string).
        sinceOpen: Funding since the current position was opened (USD string).
        sinceChange: Funding since the last size change (USD string).
    """

    allTime: str = Field(description="Lifetime funding for this coin, USD string.")
    sinceOpen: str = Field(description="Funding since the current position opened, USD string.")
    sinceChange: str = Field(description="Funding since the last size change, USD string.")


class HLPosition(BaseModel):
    """A single open perpetuals position.

    All numeric fields are decimal *strings* for precision; callers that need
    floats must parse explicitly (done in the response layer, not here).
    Direction is encoded entirely in the sign of ``szi`` (negative = short).

    Attributes:
        coin: Trading symbol, e.g. ``"BTC"``, ``"ETH"``, ``"xyz:MSTR"``.
        szi: Signed position size; negative = short.
        leverage: The position's margin mode and leverage multiplier.
        entryPx: Volume-weighted average entry price (string).
        positionValue: Current notional value in USD (string).
        unrealizedPnl: Current unrealized PnL in USD (string).
        returnOnEquity: PnL as a fraction of margin used (string).
        liquidationPx: Price at which this position would liquidate given the
            rest of the account, or ``None`` when over-collateralized to
            impossibility. For cross-margin positions this per-position value is
            often practically meaningless (a snapshot of the whole account); the
            real risk threshold is account-level
            (``accountValue - crossMaintenanceMarginUsed``). See
            ``HLClearinghouseState`` and api_spike_findings.md Q2.
        marginUsed: Margin allocated to this position, USD string.
        maxLeverage: The *symbol's* leverage ceiling set by HL — NOT this
            position's leverage (that is ``leverage.value``).
        cumFunding: Cumulative funding across three timeframes.
    """

    coin: str = Field(description="Trading symbol, e.g. 'BTC', 'ETH', 'xyz:MSTR'.")
    szi: str = Field(description="Signed position size; negative = short.")
    leverage: Leverage = Field(description="Position margin mode and leverage multiplier.")
    entryPx: str = Field(description="Volume-weighted average entry price.")
    positionValue: str = Field(description="Current notional value in USD.")
    unrealizedPnl: str = Field(description="Current unrealized PnL in USD.")
    returnOnEquity: str = Field(description="PnL as a fraction of margin used.")
    liquidationPx: str | None = Field(
        description=(
            "Price at which this position liquidates given the rest of the "
            "account; null when over-collateralized. For cross positions this is "
            "supplementary diagnostic only — the real trigger is account-level."
        ),
    )
    marginUsed: str = Field(description="Margin allocated to this position, USD.")
    maxLeverage: int = Field(
        description=(
            "The symbol's leverage ceiling (set by HL), NOT this position's "
            "current leverage. See `leverage.value` for the latter."
        ),
    )
    cumFunding: CumFunding = Field(description="Cumulative funding across three timeframes.")

    # Unobserved shapes that may add fields here (Pydantic ignores extras, so
    # they parse but are dropped — declare them when fixtures exist):
    #   - isolated-margin positions (may carry isolated-margin detail),
    #   - HIP-3-market positions (e.g. coin "xyz:MSTR"; same shape so far).


class HLAssetPosition(BaseModel):
    """One entry in a wallet's ``assetPositions`` list.

    Wraps an :class:`HLPosition` with the account's position-direction model:

    - ``oneWay``: a single net position per symbol (most common). Selling beyond
      size flips the sign to a short.
    - ``hedged``: long and short on the same symbol can coexist as separate
      entries, so a symbol may appear *twice* in ``assetPositions``.

    Attributes:
        type: Position-direction mode (``"oneWay"`` or ``"hedged"``).
        position: The wrapped position detail.

    Note (unobserved shape): all recorded fixtures are ``oneWay``. The ``hedged``
    response shape (duplicate-coin entries) has not been captured; the type is
    declared from the documented contract.
    """

    type: Literal["oneWay", "hedged"] = Field(description="Position-direction mode.")
    position: HLPosition = Field(description="The wrapped position detail.")


class HLMarginSummary(BaseModel):
    """Account-level margin and exposure snapshot.

    All values are USD strings. For a pure cross-margin account, the
    ``marginSummary`` and ``crossMarginSummary`` on the parent state are
    typically identical; they diverge when some positions are isolated (the
    ``cross*`` summary then reflects only the cross-margin subset).

    Attributes:
        accountValue: Total account value (collateral + unrealized PnL).
        totalNtlPos: Sum of |position notional| across all positions.
        totalRawUsd: Raw USDC balance, ignoring unrealized PnL.
        totalMarginUsed: Total margin locked behind positions.
    """

    accountValue: str = Field(description="Total account value (collateral + unrealized PnL).")
    totalNtlPos: str = Field(description="Sum of |position notional| across all positions.")
    totalRawUsd: str = Field(description="Raw USDC balance, ignoring unrealized PnL.")
    totalMarginUsed: str = Field(description="Total margin locked behind positions.")


class HLClearinghouseState(BaseModel):
    """Full response from the ``clearinghouseState`` info endpoint for one wallet.

    The top-level shape for a single wallet on a single dex (native HL by
    default, or a HIP-3 deployment via the request's ``dex`` field).

    Risk-monitoring note: for cross-margin accounts the meaningful liquidation
    threshold is ``accountValue - crossMaintenanceMarginUsed`` — NOT the
    per-position ``liquidationPx`` fields, which are derived diagnostics. The
    account-level gap is the real trigger (api_spike_findings.md Q2).

    An empty ``assetPositions`` list is a normal result (a wallet with no open
    positions), not an error.

    Attributes:
        marginSummary: Whole-account margin/exposure snapshot.
        crossMarginSummary: Same shape, restricted to the cross-margin subset.
        crossMaintenanceMarginUsed: Minimum margin the cross pool must maintain;
            when ``accountValue`` falls toward this, cross positions force-close.
        withdrawable: USDC immediately available for withdrawal.
        assetPositions: Open positions (possibly empty).
        time: Server timestamp, milliseconds since epoch.
    """

    marginSummary: HLMarginSummary = Field(description="Whole-account margin snapshot.")
    crossMarginSummary: HLMarginSummary = Field(
        description="Margin snapshot restricted to the cross-margin subset."
    )
    crossMaintenanceMarginUsed: str = Field(
        description=(
            "Minimum margin the cross pool must maintain to avoid liquidation; "
            "when accountValue falls toward this number, cross positions "
            "start force-closing."
        ),
    )
    withdrawable: str = Field(
        description=(
            "USDC immediately available for withdrawal — collateral not locked "
            "behind positions or open orders."
        ),
    )
    assetPositions: list[HLAssetPosition] = Field(
        description="Open positions; empty list is a normal no-positions result."
    )
    time: int = Field(description="Server timestamp, milliseconds since epoch.")


# --------------------------------------------------------------------------- #
# l2Book                                                                      #
# --------------------------------------------------------------------------- #


class HLL2Level(BaseModel):
    """A single aggregated price level in an l2Book side.

    Attributes:
        px: Price of the level (decimal string).
        sz: Total size resting at this level (decimal string).
        n: Number of distinct orders aggregated into this level.
    """

    px: str = Field(description="Level price (decimal string).")
    sz: str = Field(description="Total resting size at this level (decimal string).")
    n: int = Field(description="Number of distinct orders aggregated into this level.")


class HLL2Book(BaseModel):
    """Response from the ``l2Book`` info endpoint.

    The book is returned aggregated per the request's ``nSigFigs``/``mantissa``
    and then truncated to at most 20 levels per side (api_spike_findings.md Q1).

    ``levels`` is a two-element list ``[bids, asks]``: index 0 is the bid side
    (descending price), index 1 the ask side (ascending price). It is kept in
    the exact API order rather than split into named fields, to mirror the wire
    shape; convenience accessors live in the analytics/response layers.

    Attributes:
        coin: The symbol this book is for (echoes the request).
        time: Server timestamp, milliseconds since epoch.
        levels: ``[bid_levels, ask_levels]``, each up to 20 entries.
        spread: Top-of-book spread as a decimal string. (Observed live but not
            documented in the original spike notes — flagged when these schemas
            were first validated against recorded fixtures; ``Optional`` defensively in case older responses omit it.)
    """

    coin: str = Field(description="Symbol this book is for.")
    time: int = Field(description="Server timestamp, milliseconds since epoch.")
    levels: list[list[HLL2Level]] = Field(
        description="[bid_levels, ask_levels]; each side up to 20 entries."
    )
    spread: str | None = Field(
        default=None, description="Top-of-book spread, decimal string (may be absent)."
    )


# --------------------------------------------------------------------------- #
# perpDexs (HIP-3 discovery)                                                  #
# --------------------------------------------------------------------------- #


class HLPerpDex(BaseModel):
    """One HIP-3 deployment entry from the ``perpDexs`` discovery response.

    ``extra="allow"`` is deliberate: this shape is the *least stable* in the API.
    The original spike documented 6 fields; live responses now carry 11 (HL added
    ``assetToFundingInterestRate``, ``assetToFundingMultiplier``,
    ``deployerFeeScale``, ``lastDeployerFeeScaleChangeTime``, ``subDeployers``).
    Rather than break on the next addition, we declare what we have observed and
    retain anything new. Flagged when these schemas were first validated against
    recorded fixtures.

    Only ``name`` is load-bearing for v0 (the dex routing key for
    ``clearinghouseState``'s ``dex`` field); the rest is metadata surfaced by the
    ``list_hip3_dexes`` tool.

    Attributes:
        name: Routing key for the dex (e.g. ``"xyz"``); pass as the ``dex`` field.
        fullName: Human-readable name.
        deployer: Deployer wallet address.
        oracleUpdater: Address authorized to push oracle updates.
        feeRecipient: Address receiving the deployment's fees.
        assetToStreamingOiCap: Per-asset open-interest caps as ``[coin, cap]``
            string pairs.
        assetToFundingInterestRate: Per-asset funding interest-rate ``[coin, rate]``
            string pairs (added since the spike).
        assetToFundingMultiplier: Per-asset funding multiplier ``[coin, mult]``
            string pairs (added since the spike).
        deployerFeeScale: Deployer fee scale, decimal string (added since the spike).
        lastDeployerFeeScaleChangeTime: ISO-8601 timestamp of the last fee-scale
            change (added since the spike).
        subDeployers: Delegated sub-deployer permissions; heterogeneous
            ``[action, [addresses...]]`` entries (added since the spike).
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(description="Dex routing key, e.g. 'xyz'; pass as the `dex` field.")
    fullName: str | None = Field(default=None, description="Human-readable dex name.")
    deployer: str | None = Field(default=None, description="Deployer wallet address.")
    oracleUpdater: str | None = Field(
        default=None, description="Address authorized to push oracle updates."
    )
    feeRecipient: str | None = Field(
        default=None, description="Address receiving the deployment's fees."
    )
    assetToStreamingOiCap: list[list[str]] | None = Field(
        default=None, description="Per-asset OI caps as [coin, cap] string pairs."
    )
    assetToFundingInterestRate: list[list[str]] | None = Field(
        default=None, description="Per-asset funding interest-rate [coin, rate] pairs."
    )
    assetToFundingMultiplier: list[list[str]] | None = Field(
        default=None, description="Per-asset funding multiplier [coin, mult] pairs."
    )
    deployerFeeScale: str | None = Field(
        default=None, description="Deployer fee scale, decimal string."
    )
    lastDeployerFeeScaleChangeTime: str | None = Field(
        default=None, description="ISO-8601 timestamp of last fee-scale change."
    )
    subDeployers: list[Any] | None = Field(
        default=None,
        description="Delegated sub-deployer permissions; [action, [addresses]] entries.",
    )


class HLPerpDexs(RootModel[list[HLPerpDex | None]]):
    """The ``perpDexs`` response: a list whose FIRST element is ``null``.

    The null first element represents native HL perps (the empty-string dex key);
    every remaining element is an :class:`HLPerpDex`. We model this null-first
    shape explicitly as ``list[HLPerpDex | None]`` rather than silently dropping
    the leading ``None``, so the wire contract is visible in the type.

    Use :attr:`dexes` to get just the named HIP-3 deployments (native HL
    excluded).
    """

    @property
    def dexes(self) -> list[HLPerpDex]:
        """Return the named HIP-3 deployments, excluding the null native-HL slot.

        Returns:
            All non-null entries in the response, in API order.
        """
        return [d for d in self.root if d is not None]


# --------------------------------------------------------------------------- #
# metaAndAssetCtxs (per-asset live context: funding, premium, prices, OI)     #
# --------------------------------------------------------------------------- #


class HLUniverseAsset(BaseModel):
    """One market in a dex's ``meta.universe`` (index-aligned with its asset ctx).

    ``extra="allow"`` for the same reason as :class:`HLPerpDex`: HIP-3 universes
    carry extra per-asset fields (``growthMode``, ``deployerFeeScale``,
    ``lastFeeScaleChangeTime``, ...) that native HL does not. Only ``name`` is
    load-bearing (the lookup key for the matching ctx).

    Attributes:
        name: Market symbol; HIP-3 names come back dex-prefixed (``"xyz:XYZ100"``).
        szDecimals: Size decimals for the market.
        maxLeverage: Symbol leverage ceiling.
        isDelisted: ``True`` for delisted markets; absent (``None``) otherwise.
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(description="Market symbol; HIP-3 names are dex-prefixed.")
    szDecimals: int = Field(description="Size decimals for the market.")
    maxLeverage: int = Field(description="Symbol leverage ceiling.")
    isDelisted: bool | None = Field(
        default=None, description="True for delisted markets; absent otherwise."
    )


class HLMeta(BaseModel):
    """The ``meta`` half of ``metaAndAssetCtxs``.

    ``marginTables``/``collateralToken`` are present on the wire but unused, so
    they are ignored (default ``extra="ignore"``).

    Attributes:
        universe: The dex's markets, index-aligned with the ctx list.
    """

    universe: list[HLUniverseAsset] = Field(description="Markets, index-aligned with ctxs.")


class HLAssetCtx(BaseModel):
    """Live per-asset context from ``metaAndAssetCtxs`` (decimal strings).

    ``funding`` is the PREDICTED rate for the current hourly interval, expressed
    per hour (HL settles funding hourly). It carries no server timestamp.

    ``premium``, ``midPx`` and ``impactPxs`` are ``null`` for delisted markets
    (observed 56/234 native, 21/131 on ``xyz``, 2026-10-05).

    Attributes:
        funding: Predicted hourly funding rate for the current interval.
        openInterest: Open interest in coin units.
        prevDayPx: Price 24h ago.
        dayNtlVlm: 24h notional volume, USD.
        premium: Current premium (mark vs oracle input to funding), or ``None``.
        oraclePx: Oracle price.
        markPx: Mark price.
        midPx: Book mid, or ``None`` when there is no book.
        impactPxs: ``[impact_bid, impact_ask]`` prices, or ``None``.
        dayBaseVlm: 24h volume in coin units.
    """

    funding: str = Field(description="Predicted hourly funding rate, current interval.")
    openInterest: str = Field(description="Open interest, coin units.")
    prevDayPx: str = Field(description="Price 24h ago.")
    dayNtlVlm: str = Field(description="24h notional volume, USD.")
    premium: str | None = Field(default=None, description="Current premium; null if delisted.")
    oraclePx: str = Field(description="Oracle price.")
    markPx: str = Field(description="Mark price.")
    midPx: str | None = Field(default=None, description="Book mid; null if no book.")
    impactPxs: list[str] | None = Field(
        default=None, description="[impact_bid, impact_ask]; null if delisted."
    )
    dayBaseVlm: str = Field(description="24h volume, coin units.")


class HLMetaAndAssetCtxs(RootModel[tuple[HLMeta, list[HLAssetCtx]]]):
    """The ``metaAndAssetCtxs`` response: a 2-element ``[meta, ctxs]`` array.

    ``meta.universe[i]`` and ``ctxs[i]`` describe the same market; the pairing is
    purely positional. Use :meth:`ctx_for` to look a market up by name.
    """

    @property
    def meta(self) -> HLMeta:
        """Return the ``meta`` half (universe)."""
        return self.root[0]

    @property
    def ctxs(self) -> list[HLAssetCtx]:
        """Return the ctx half, index-aligned with ``meta.universe``."""
        return self.root[1]

    def ctx_for(self, coin: str) -> tuple[HLUniverseAsset, HLAssetCtx] | None:
        """Look up a market's universe entry and ctx by exact symbol.

        Mechanism: linear scan of ``meta.universe`` for ``name == coin``, returning
        the entry and the ctx at the same index.

        Args:
            coin: Exact symbol, e.g. ``"BTC"`` or ``"xyz:XYZ100"`` (case-sensitive).

        Returns:
            ``(asset, ctx)`` for the market, or ``None`` if it is not listed.
        """
        for i, asset in enumerate(self.meta.universe):
            if asset.name == coin:
                return asset, self.ctxs[i]
        return None


# --------------------------------------------------------------------------- #
# fundingHistory                                                              #
# --------------------------------------------------------------------------- #


class HLFundingHistoryEntry(BaseModel):
    """One hourly funding settlement from ``fundingHistory``.

    Rows come oldest-first, at most 500 per response (api_spike_findings.md,
    Funding section) — longer windows must be paged.

    Attributes:
        coin: Symbol (echoes the request; HIP-3 names dex-prefixed).
        fundingRate: Settled hourly funding rate (decimal string). Positive means
            longs paid shorts.
        premium: Premium sampled for this interval (decimal string).
        time: Settlement timestamp, milliseconds since epoch.
    """

    coin: str = Field(description="Symbol for this settlement.")
    fundingRate: str = Field(description="Settled hourly rate; positive = longs paid shorts.")
    premium: str = Field(description="Premium for this interval.")
    time: int = Field(description="Settlement time, ms since epoch.")


# --------------------------------------------------------------------------- #
# predictedFundings (cross-venue)                                             #
# --------------------------------------------------------------------------- #


class HLVenueFunding(BaseModel):
    """One venue's predicted funding for a coin, from ``predictedFundings``.

    ``fundingRate`` is per THAT venue's own interval (e.g. 8h on Binance/Bybit,
    1h on HL), so compare only after dividing by ``fundingIntervalHours``.
    ``fundingIntervalHours`` is sometimes absent (observed on ``BinPerp`` for
    some coins, 2026-10-05), in which case the rate cannot be normalized.

    HL's own ``nextFundingTime`` equals the most recent settlement (it lags one
    interval; 4/4 snapshots, 2026-10-05/06), so it is in the past; do not use it
    as the next settlement.

    Attributes:
        fundingRate: Predicted rate per ``fundingIntervalHours``.
        nextFundingTime: Venue-reported next settlement, ms since epoch.
        fundingIntervalHours: The venue's funding interval, or ``None`` if absent.
    """

    fundingRate: str = Field(description="Predicted rate per the venue's own interval.")
    nextFundingTime: int = Field(description="Venue-reported next settlement, ms epoch.")
    fundingIntervalHours: int | None = Field(
        default=None, description="Venue funding interval in hours; may be absent."
    )


class HLPredictedFundings(RootModel[list[tuple[str, list[tuple[str, HLVenueFunding | None]]]]]):
    """The ``predictedFundings`` response: ``[[coin, [[venue, funding|null], ...]], ...]``.

    Covers native HL coins only (no HIP-3 symbols). Venue keys observed:
    ``BinPerp``, ``HlPerp``, ``BybitPerp``. A venue entry is ``null`` when that
    venue does not list the coin.
    """

    def for_coin(self, coin: str) -> list[tuple[str, HLVenueFunding | None]] | None:
        """Return the per-venue entries for ``coin``, or ``None`` if absent.

        Mechanism: linear scan of the root list for the matching coin name.

        Args:
            coin: Exact native symbol, e.g. ``"BTC"``.

        Returns:
            The ``(venue, funding | None)`` list for the coin, or ``None``.
        """
        for name, venues in self.root:
            if name == coin:
                return venues
        return None
