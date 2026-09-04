"""Fetch → normalize → cache → gate. One entry point: get_chain()."""
from datetime import datetime, timedelta
from .feeds.polygon import PolygonFeed
from .feeds.cboe import CBOEFeed
from .feeds.yfinance_feed import YFinanceFeed
from . import store, quality

# Priority order: real-time first if paid key present, else free
FEEDS = [PolygonFeed(), CBOEFeed(), YFinanceFeed()]

def get_chain(symbol, refresh=True, max_age_s=60):
    """Return a ChainSnapshot. Tries feeds in priority order, caches result."""
    sym = symbol.upper()
    store.init()

    # Serve from cache if fresh and not forcing refresh
    if not refresh:
        last = store.latest_chain_ts(sym)
        if last:
            age = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds()
            if age < max_age_s:
                return _rehydrate(sym, last)

    errors = []
    for feed in FEEDS:
        if not feed.available():
            continue
        try:
            snap = feed.fetch_chain(sym)
            snap.quality = quality.quality_report(snap)
            store.save_chain(snap)          # cache with provenance
            return snap
        except Exception as e:
            errors.append(f"{feed.name}: {str(e)[:80]}")
            continue

    raise RuntimeError(f"All feeds failed for {sym}: {' | '.join(errors)}")

def _rehydrate(symbol, ts):
    from .schema import ContractQuote, ChainSnapshot
    rows = store.load_chain_asof(symbol, ts)
    contracts = [ContractQuote(
        symbol=r["symbol"], expiry=r["expiry"], strike=r["strike"],
        contract_type=r["type"], bid=r["bid"], ask=r["ask"], last=r["last"],
        volume=r["volume"], open_interest=r["open_interest"], iv=r["iv"],
        delta=r["delta"], gamma=r["gamma"], theta=r["theta"], vega=r["vega"],
        source=r["source"], provenance=r["provenance"]) for r in rows]
    meta = store.con().execute(
        "SELECT * FROM chain_meta WHERE symbol=? AND ts=?", (symbol, ts)).fetchone()
    snap = ChainSnapshot(symbol=symbol, spot=meta["spot"], as_of=ts,
                         source=meta["source"], provenance=meta["provenance"],
                         contracts=contracts)
    snap.quality = quality.quality_report(snap)
    snap.provenance = "CACHED"   # be honest: this came from cache
    return snap

def feed_status():
    return {f.name: f.available() for f in FEEDS}