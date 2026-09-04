"""yfinance options chain — free fallback. OI+IV present, greeks computed downstream."""
from datetime import datetime, date
import pandas as pd
from ..schema import ContractQuote, ChainSnapshot
from .base import OptionsFeed

INDEX_SYMBOLS = {"SPX", "NDX", "RUT", "VIX", "XEO", "XSP", "MRUT"}

def get_yf_symbol(symbol):
    """yfinance requires ^ prefix for index options."""
    sym = symbol.upper()
    return f"^{sym}" if sym in INDEX_SYMBOLS else sym

def _safe_int(val):
    """Safely convert yfinance values to int, handling numpy NaNs."""
    if pd.isna(val):
        return 0
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0

class YFinanceFeed(OptionsFeed):
    name = "yfinance"
    provenance = "DELAYED"

    def available(self):
        try:
            import yfinance  # noqa
            return True
        except ImportError:
            return False

    def fetch_chain(self, symbol):
        import yfinance as yf
        sym = symbol.upper()
        t = yf.Ticker(get_yf_symbol(sym))
        expiries = t.options
        if not expiries:
            raise ValueError(f"No option expiries for {sym}")

        spot = None
        try:
            spot = float(t.history(period="1d")["Close"].iloc[-1])
        except Exception:
            pass

        today = date.today()
        contracts = []
        for exp in expiries:
            try:
                chain = t.option_chain(exp)
            except Exception:
                continue
            
            try:
                exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
                dte = (exp_date - today).days
            except Exception:
                dte = None

            for frame, cp in ((chain.calls, "call"), (chain.puts, "put")):
                if frame is None or frame.empty:
                    continue
                for _, row in frame.iterrows():
                    iv = row.get("impliedVolatility")
                    # yfinance IV is a decimal (0.25), we want percentage (25.0) to match CBOE
                    if pd.notna(iv) and iv < 1.0:
                        iv = iv * 100.0
                    
                    contracts.append(ContractQuote(
                        symbol=sym, expiry=exp, strike=float(row.get("strike", 0)),
                        contract_type=cp,
                        bid=row.get("bid"), ask=row.get("ask"),
                        last=row.get("lastPrice"),
                        volume=_safe_int(row.get("volume")),
                        open_interest=_safe_int(row.get("openInterest")),
                        iv=float(iv) if pd.notna(iv) else None,
                        delta=None, gamma=None, theta=None, vega=None,
                        source="yfinance", provenance="DELAYED"))

        if not contracts:
            raise ValueError(f"yfinance returned 0 contracts for {sym}")
            
        return ChainSnapshot(symbol=sym, spot=spot,
                             as_of=datetime.utcnow().isoformat(),
                             source="yfinance", provenance="DELAYED",
                             contracts=contracts)