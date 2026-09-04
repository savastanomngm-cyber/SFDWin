"""Polygon.io — real-time options snapshot. Requires paid Options plan."""
import os, requests
from datetime import datetime
from ..schema import ContractQuote, ChainSnapshot
from .base import OptionsFeed

URL = "https://api.polygon.io/v3/snapshot/options/{sym}?apiKey={key}"

class PolygonFeed(OptionsFeed):
    name = "polygon"
    provenance = "LIVE"

    def available(self):
        return bool(os.getenv("POLYGON_API_KEY"))

    def fetch_chain(self, symbol):
        sym = symbol.upper()
        key = os.getenv("POLYGON_API_KEY")
        r = requests.get(URL.format(sym=sym, key=key), timeout=15)
        r.raise_for_status()
        results = r.json().get("results", [])
        spot, contracts = None, []
        for o in results:
            d = o.get("details", {})
            g = o.get("greeks", {})
            if o.get("underlying_asset", {}).get("price"):
                spot = float(o["underlying_asset"]["price"])
            try:
                contracts.append(ContractQuote(
                    symbol=sym, expiry=d.get("expiration_date"),
                    strike=float(d.get("strike_price")),
                    contract_type=d.get("contract_type"),
                    bid=(o.get("last_quote") or {}).get("bid"),
                    ask=(o.get("last_quote") or {}).get("ask"),
                    last=o.get("last_trade", {}).get("price"),
                    volume=int(o.get("volume") or 0),
                    open_interest=int(o.get("open_interest") or 0),
                    iv=o.get("implied_volatility"),
                    delta=g.get("delta"), gamma=g.get("gamma"),
                    theta=g.get("theta"), vega=g.get("vega"),
                    source="polygon", provenance="LIVE"))
            except Exception:
                continue
        if not contracts:
            raise ValueError(f"Polygon returned 0 contracts for {sym} (check plan tier)")
        return ChainSnapshot(symbol=sym, spot=spot,
                             as_of=datetime.utcnow().isoformat(),
                             source="polygon", provenance="LIVE",
                             contracts=contracts)