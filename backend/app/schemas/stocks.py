"""Cooperative-share response schemas for one world."""

from __future__ import annotations

from pydantic import BaseModel


class StockInfo(BaseModel):
    stock_id: str
    name: str
    unit_price: int
    operating_volume: int
    source: str
    company_id: str
    issuer_company_id: str
    outstanding_shares: int
    available_shares: int
    holding_cap: int


class StockHoldingInfo(BaseModel):
    agent_id: str
    stock_id: str
    shares: int


class StocksResponse(BaseModel):
    stocks: list[StockInfo]
    holdings: list[StockHoldingInfo]
