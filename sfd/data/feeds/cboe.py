"""CBOE delayed quotes — best free source for GEX (greeks pre-computed)."""
import re
import requests
from datetime import datetime
from ..schema import ContractQuote, ChainSnapshot
from .base import OptionsFeed

URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"

# CBOE requires an underscore prefix for index options
INDEX_SYMBOLS = {"SPX", "NDX", "RUT", "VIX", "XEO", "XSP", "MRUT"}

def get_cboe_symbol(symbol):
    sym = symbol.upper()
    return f"_{sym}" if sym in INDEX_SYMBOLS else sym

def parse_osi(sym):
    """Robust OSI parser using regex. Immune to variable root padding."""
    # Matches: Root (letters/underscores) + optional spaces + YYMMDD + C/P + 8-digit strike
    m = re.search(r'([A-Z_]+)\s*(\d{6})([CP])(\d{8})$', sym.strip())
    if not m:
        raise ValueError(f"Invalid OSI format: {sym}")
    
    root, date_str, cp, strike_str = m.groups()
    expiry = datetime.strptime(date_str, "%y%m%d").date().isoformat()
    contract_type = "call" if cp == "C" else "put"
    strike = int(strike_str) / 1000.0
    return expiry, contract_type, strike

class CBOEFeed(OptionsFeed):
    name = "cboe"
    provenance = "DELAYED"

    def available(self):
        try:
            import requests  # noqa
            return True
        except ImportError:
            return False

    def fetch_chain(self, symbol):
        api_sym = get_cboe_symbol(symbol)
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*"
        }
        r = requests.get(URL.format(sym=api_sym), timeout=12, headers=headers)
        r.raise_for_status()
        
        payload = r.json()
        data = payload.get("data", {})

        spot = None
        for k in ("current_price", "last", "close"):
            if data.get(k):
                spot = float(data[k]); break

        contracts = []
        for o in data.get("options", []):
            try:
                expiry, cp, strike = parse_osi(o["option"])
                contracts.append(ContractQuote(
                    symbol=symbol.upper(), expiry=expiry, strike=strike, contract_type=cp,
                    bid=o.get("bid"), ask=o.get("ask"),
                    last=o.get("last_trade_price"),
                    volume=int(o.get("volume") or 0),
                    open_interest=int(o.get("open_interest") or 0),
                    iv=o.get("iv"),
                    delta=o.get("delta"), gamma=o.get("gamma"),
                    theta=o.get("theta"), vega=o.get("vega"),
                    source="cboe", provenance="DELAYED"))
            except Exception:
                continue  # skip malformed rows, keep the chain

        if not contracts:
            raise ValueError(f"CBOE returned 0 parseable contracts for {symbol}")

        return ChainSnapshot(symbol=symbol.upper(), spot=spot,
                             as_of=datetime.utcnow().isoformat(),
                             source="cboe", provenance="DELAYED",
                             contracts=contracts)