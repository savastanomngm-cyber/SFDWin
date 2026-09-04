"""Asset registry. Maps options underlier -> tape proxy -> futures."""
from . import config

DEFAULT_ASSETS = {
    "SPX": {"options_root": "SPX", "tape_proxy": "SPY", "futures": "ES", "multiplier": 100},
    "NDX": {"options_root": "NDX", "tape_proxy": "QQQ", "futures": "NQ", "multiplier": 100},
    "RUT": {"options_root": "RUT", "tape_proxy": "IWM", "futures": "RTY", "multiplier": 100},
    "VIX": {"options_root": "VIX", "tape_proxy": "VIXY", "futures": "VX", "multiplier": 100},
}

# yfinance symbols for LIVE index spot (Sentinel wall-distance measurement)
INDEX_YF = {"SPX": "^SPX", "NDX": "^NDX", "RUT": "^RUT", "VIX": "^VIX"}


def list_assets():
    return config.load().get("assets", DEFAULT_ASSETS)


def get_asset(key):
    assets = list_assets()
    key = key.upper()
    if key not in assets:
        raise ValueError(f"Unknown asset '{key}'. Available: {', '.join(assets.keys())}")
    return assets[key]


def active_asset_key():
    return config.load().get("settings", {}).get("active_asset", "SPX").upper()


def tick_econ(futures):
    return config.load().get("ticks", {}).get(futures, {"tick_size": 0.25, "tick_value": 5.00})


def index_yf_symbol(asset):
    """yfinance symbol for the index leg.
    yfinance indices use a ^ prefix (^NDX, ^SPX). The $ prefix is
    TradingView-only and makes yfinance report 'possibly delisted'."""
    _YF_INDEX = {
        "NDX": "^NDX", "SPX": "^SPX", "RUT": "^RUT", "VIX": "^VIX",
        "DJI": "^DJI", "IXIC": "^IXIC", "MID": "^MID", "OEX": "^OEX",
    }
    if isinstance(asset, dict):
        root = (asset.get("options_root") or asset.get("index")
                or asset.get("index_symbol") or asset.get("key") or "")
    else:
        root = str(asset)
    root = str(root).upper().lstrip("$^")
    return _YF_INDEX.get(root, root)