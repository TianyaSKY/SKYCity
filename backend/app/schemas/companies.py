"""Pydantic contracts for company and formal-employment endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class JobApplicationRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="", max_length=512)


class JobApplicationReviewRequest(BaseModel):
    manager_agent_id: str = Field(min_length=1, max_length=64)
    decision: Literal["accept", "reject"]
    reason: str = Field(default="", max_length=512)


class ShiftStartRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=64)


class ShiftLeaveRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="", max_length=512)


class LeaveReviewRequest(BaseModel):
    manager_agent_id: str = Field(min_length=1, max_length=64)
    decision: Literal["approve", "reject"]
    reason: str = Field(default="", max_length=512)


class ApplicationWithdrawRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=64)


class EmploymentResignRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="", max_length=512)


class EmploymentTerminateRequest(BaseModel):
    manager_agent_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="", max_length=512)


class RecruitmentToggleRequest(BaseModel):
    manager_agent_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="", max_length=512)


class CompanyStatusRequest(BaseModel):
    manager_agent_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="", max_length=512)


class PurchaseCompanyGoodsRequest(BaseModel):
    manager_agent_id: str = Field(min_length=1, max_length=64)
    seller_company_id: str = Field(min_length=1, max_length=64)
    item_id: str = Field(min_length=1, max_length=64)
    quantity: int = Field(default=1, ge=1, le=99)
    reason: str = Field(default="", max_length=512)


class StockStoreRequest(BaseModel):
    manager_agent_id: str = Field(min_length=1, max_length=64)
    store_id: str = Field(min_length=1, max_length=64)
    item_id: str = Field(min_length=1, max_length=64)
    quantity: int = Field(default=1, ge=1, le=99)
    reason: str = Field(default="", max_length=512)
