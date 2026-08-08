"""Stock market response schemas (M10): quotes + holdings for one world."""

from __future__ import annotations

from pydantic import BaseModel


class StockInfo(BaseModel):
    stock_id: str
    name: str
    price: int
    prev_price: int
    day_business: int
    last_div_per_share: int
    source: str
    company_id: str


class StockHoldingInfo(BaseModel):
    agent_id: str
    stock_id: str
    shares: int
    avg_cost: int  # 持仓均价（金币/股），浮盈计算基准


class StocksResponse(BaseModel):
    stocks: list[StockInfo]
    holdings: list[StockHoldingInfo]
