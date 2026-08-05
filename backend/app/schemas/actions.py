"""Action request/response schemas (manual test API, no LLM in M2)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.event import WorldEventEnvelope


class ActionRequest(BaseModel):
    """POST /api/worlds/{id}/agents/{agent_id}/actions body."""

    model_config = ConfigDict(extra="forbid")

    action_type: Literal["move", "wait", "talk", "work", "buy_item", "sell_item", "use_item", "sleep", "buy_stock", "sell_stock", "transfer_money", "give_item"]
    destination_id: str | None = None
    minutes: int | None = Field(default=None, ge=1)
    reason: str | None = None
    target_agent_id: str | None = None
    message: str | None = Field(default=None, max_length=200)
    intent: Literal["greet", "chat", "ask", "offer", "leave"] | None = None
    # M5 economy actions.
    job_id: str | None = None
    item_id: str | None = None
    quantity: int | None = Field(default=None, ge=1, le=99)
    # M10 stock actions.
    stock_id: str | None = None
    shares: int | None = Field(default=None, ge=1, le=9999)
    # M11 agent-to-agent transfer.
    amount: int | None = Field(default=None, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def _validate_shape(self) -> "ActionRequest":
        if self.action_type == "move" and not self.destination_id:
            raise ValueError("move requires destination_id")
        if self.action_type == "talk" and not self.target_agent_id:
            raise ValueError("talk requires target_agent_id")
        if self.action_type == "talk" and not self.message:
            raise ValueError("talk requires message")
        if self.action_type == "work" and not self.job_id:
            raise ValueError("work requires job_id")
        if self.action_type in ("buy_item", "sell_item", "use_item") and not self.item_id:
            raise ValueError(f"{self.action_type} requires item_id")
        if self.action_type in ("buy_stock", "sell_stock") and not self.stock_id:
            raise ValueError(f"{self.action_type} requires stock_id")
        if self.action_type == "transfer_money" and not self.target_agent_id:
            raise ValueError("transfer_money requires target_agent_id")
        if self.action_type == "transfer_money" and self.amount is None:
            raise ValueError("transfer_money requires amount")
        if self.action_type == "give_item" and not self.target_agent_id:
            raise ValueError("give_item requires target_agent_id")
        if self.action_type == "give_item" and not self.item_id:
            raise ValueError("give_item requires item_id")
        return self


class ActionSuccess(BaseModel):
    """200 response: the action was accepted, envelope describes it."""

    success: Literal[True] = True
    event: WorldEventEnvelope


class ActionRejected(BaseModel):
    """409 response: a world rule rejected the action (R1/R6/R8/pause)."""

    success: Literal[False] = False
    reason: str
