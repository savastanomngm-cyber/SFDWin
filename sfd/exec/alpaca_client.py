"""Alpaca Execution — Fills SFD tickets programmatically."""
import os
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
trading_client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True) if ALPACA_KEY else None

def execute_ticket(ticket, symbol="QQQ"):
    if not trading_client: return {"status": "error", "reason": "Alpaca client not initialized"}
    try:
        side = OrderSide.BUY if ticket.get("direction") == "LONG" else OrderSide.SELL
        order = trading_client.submit_order(order_data=LimitOrderRequest(
            symbol=symbol, qty=ticket.get("qty", 1), side=side,
            time_in_force=TimeInForce.DAY, limit_price=ticket.get("entry")
        ))
        return {"status": "success", "order_id": order.id, "symbol": symbol, 
                "side": side.value, "qty": ticket.get("qty", 1), "limit": ticket.get("entry")}
    except Exception as e:
        return {"status": "error", "reason": str(e)}