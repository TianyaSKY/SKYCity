"""Decision provider abstraction (M3).

A provider turns an observation into one tool decision. Two implementations
ship: the real LLM path (openai-agents) and a deterministic fake used by tests
and keyless environments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.agents.context import AgentToolContext


class DecisionError(Exception):
    """Raised when a provider cannot produce a tool decision."""


@dataclass(slots=True)
class DecisionResult:
    """One provider decision: the tool to call plus audit metadata."""

    tool_name: str
    tool_arguments: dict
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    raw_summary: str


class DecisionProvider(Protocol):
    """Anything that can turn an observation into a DecisionResult."""

    async def decide(
        self,
        *,
        observation: str,
        context: "AgentToolContext",
        trace_id: str,
    ) -> DecisionResult: ...
