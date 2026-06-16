# src/hlmcp/venues/hyperliquid.py
class HLAPIError(Exception):
    """Raised when the Hyperliquid REST API returns a non-2xx status."""
    def __init__(self, status: int, body: str, payload: dict[str, Any]) -> None:
        self.status = status
        self.body = body
        self.payload = payload
        super().__init__(f"HL API {status}: {body[:200]} (payload={payload})")


class HyperliquidPublic:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=10.0)
        self._dex_cache: set[str] | None = None
        self._dex_cache_ts: float = 0.0
        self._dex_metadata_cache: dict[str, dict] | None = None

    async def _post(self, payload: dict[str, Any]) -> Any:
        r = await self._http.post(INFO_URL, json=payload, timeout=10.0)
        if r.status_code >= 400:
            raise HLAPIError(r.status_code, r.text, payload)
        return r.json()

    async def list_dexes(self) -> dict[str, dict | None]:
        """Return {dex_name: metadata_or_None_for_native}. Cached 5 min."""
        if self._dex_metadata_cache and (time.time() - self._dex_cache_ts) < 300:
            return self._dex_metadata_cache
        raw = await self._post({"type": "perpDexs"})
        result: dict[str, dict | None] = {"": None}  # native HL
        for entry in raw:
            if isinstance(entry, dict) and "name" in entry:
                result[entry["name"]] = entry
        self._dex_metadata_cache = result
        self._dex_cache_ts = time.time()
        return result

    async def fetch_clearinghouse_state(
        self,
        user: str,
        dex: str = "",
    ) -> ClearinghouseState:
        # Client-side validation: address format
        normalized = normalize_wallet(user)
        # Client-side validation: dex name
        known = set((await self.list_dexes()).keys())
        if dex not in known:
            raise ValueError(
                f"unknown dex {dex!r}. Known: {sorted(known)}. "
                f"Use empty string for native HL."
            )
        raw = await self._post({
            "type": "clearinghouseState",
            "user": normalized,
            "dex": dex,
        })
        return ClearinghouseState.from_hl_response(raw, user=normalized, dex=dex)

    async def fetch_all_dexes_for_user(
        self,
        user: str,
    ) -> dict[str, ClearinghouseState]:
        """Fan out across every known dex. Returns {dex_name: state}."""
        normalized = normalize_wallet(user)
        dexes = await self.list_dexes()

        async def one(dex_name: str) -> tuple[str, ClearinghouseState]:
            state = await self.fetch_clearinghouse_state(normalized, dex=dex_name)
            return dex_name, state

        results = await asyncio.gather(*(one(d) for d in dexes.keys()))
        return dict(results)