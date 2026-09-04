"""Alpaca Markets feed — Real-time OPRA options + IEX equities.
FIX: Pre-import all Alpaca modules at module level to avoid threading deadlocks."""
import os, re
from datetime import datetime, timedelta

ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")

# 🟢 PRE-IMPORT ALL ALPACA MODULES HERE (thread-safe)
# This prevents deadlocks when FastAPI workers + background threads import simultaneously
ALPACA_AVAILABLE = False
try:
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest, OptionChainRequest
    from alpaca.data.timeframe import TimeFrame
    ALPACA_AVAILABLE = True
except ImportError:
    print("⚠️  alpaca-py not installed. Run: pip install alpaca-py")
except Exception as e:
    print(f"⚠️  Alpaca import error: {e}")

def _parse_osi_symbol(symbol_key):
    """Parse OSI format: ROOT + YYMMDD + C/P + Strike*1000 (8 digits)"""
    m = re.match(r'^([A-Z]+)(\d{6})([CP])(\d{8})$', symbol_key)
    if not m:
        return None
    root, date_str, cp, strike_str = m.groups()
    try:
        exp_date = datetime.strptime(date_str, "%y%m%d").date()
    except Exception:
        return None
    return {
        "root": root,
        "expiry": exp_date.strftime("%Y-%m-%d"),
        "type": "call" if cp == "C" else "put",
        "strike": int(strike_str) / 1000.0
    }

def get_realtime_equity_bars(symbol="QQQ", interval="5m", limit=500):
    """Fetch real-time IEX equity bars for the chart proxy."""
    if not ALPACA_KEY or not ALPACA_AVAILABLE:
        return None
    try:
        client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
        
        # Map interval string to Alpaca TimeFrame
        if interval == "1m":
            tf = TimeFrame.Minute
        elif interval == "5m":
            tf = TimeFrame(5, "Min")
        elif interval == "15m":
            tf = TimeFrame(15, "Min")
        elif interval == "1h":
            tf = TimeFrame.Hour
        else:
            tf = TimeFrame(5, "Min")
        
        end = datetime.utcnow()
        start = end - timedelta(days=5)
        
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=start,
            end=end,
            limit=limit,
            feed="iex"  # IEX is free real-time
        )
        
        bars = client.get_stock_bars(req)
        out = []
        for bar in bars[symbol]:
            out.append({
                "time": int(bar.timestamp.timestamp()),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": int(bar.volume)
            })
        return out
    except Exception as e:
        print(f"Alpaca equity fetch error: {e}")
        return None

def get_realtime_option_chain(symbol="QQQ"):
    """Fetch real-time option chain from Alpaca (Paper = free OPRA)."""
    if not ALPACA_KEY or not ALPACA_AVAILABLE:
        return None
    try:
        client = OptionHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
        req = OptionChainRequest(underlying_symbol=symbol)
        chain = client.get_option_chain(req)
        
        contracts = []
        for symbol_key, snapshot in chain.items():
            # Parse the OSI symbol to get strike/type/expiry
            parsed = _parse_osi_symbol(symbol_key)
            if not parsed:
                continue
            
            # Extract quote/greeks from snapshot
            quote = getattr(snapshot, 'latest_quote', None)
            greeks = getattr(snapshot, 'greeks', None)
            iv = getattr(snapshot, 'implied_volatility', None)
            oi = getattr(snapshot, 'open_interest', None)
            vol = getattr(snapshot, 'volume', None)
            
            bid = float(quote.bid_price) if quote and quote.bid_price else None
            ask = float(quote.ask_price) if quote and quote.ask_price else None
            
            contracts.append({
                "strike": parsed["strike"],
                "type": parsed["type"],
                "expiry": parsed["expiry"],
                "bid": bid,
                "ask": ask,
                "iv": float(iv) if iv else None,
                "delta": float(greeks.delta) if greeks else None,
                "gamma": float(greeks.gamma) if greeks else None,
                "theta": float(greeks.theta) if greeks else None,
                "vega": float(greeks.vega) if greeks else None,
                "open_interest": int(oi) if oi else 0,
                "volume": int(vol) if vol else 0,
            })
        
        return contracts
    except Exception as e:
        print(f"Alpaca options fetch error: {e}")
        return None