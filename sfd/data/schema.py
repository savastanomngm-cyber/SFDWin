"""SFD options data schema. Provenance or it didn't happen."""
from dataclasses import dataclass, field
from datetime import date

PROVENANCE = ("LIVE", "DELAYED", "CACHED", "EST")

@dataclass
class ContractQuote:
    symbol: str
    expiry: str            # YYYY-MM-DD
    strike: float
    contract_type: str     # 'call' | 'put'
    bid: float = None
    ask: float = None
    last: float = None
    volume: int = 0
    open_interest: int = 0
    iv: float = None
    delta: float = None
    gamma: float = None
    theta: float = None
    vega: float = None
    source: str = "unknown"
    provenance: str = "EST"   # least-trusted default; feeds MUST upgrade

    @property
    def dte(self):
        try:
            return (date.fromisoformat(self.expiry) - date.today()).days
        except Exception:
            return None

    @property
    def mid(self):
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2.0
        return self.last

    def to_row(self, ts):
        return (ts, self.symbol, self.expiry, self.strike, self.contract_type,
                self.bid, self.ask, self.last, self.volume, self.open_interest,
                self.iv, self.delta, self.gamma, self.theta, self.vega,
                self.source, self.provenance)

@dataclass
class ChainSnapshot:
    symbol: str
    spot: float
    as_of: str             # ISO timestamp of capture
    source: str
    provenance: str
    contracts: list = field(default_factory=list)
    quality: dict = field(default_factory=dict)

    def calls(self): return [c for c in self.contracts if c.contract_type == "call"]
    def puts(self):  return [c for c in self.contracts if c.contract_type == "put"]
    def near_dte(self, max_dte=45):
        return [c for c in self.contracts if c.dte is not None and 0 <= c.dte <= max_dte]