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
    """One provider decision: the tool to call plus audit metadata.

    ``tool_output`` is the tool's executed result (JSON string) when the
    provider's runtime already executed the tool (the real LLM path runs the
    SDK agent loop, which executes tools). The decision service consumes it
    instead of re-executing the tool; None means the tool still needs to run
    (fake provider / fallback).
    """

    tool_name: str
    tool_arguments: dict
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    raw_summary: str
    tool_output: str | None = None


class DecisionProvider(Protocol):
    """Anything that can turn an observation into a DecisionResult."""

    async def decide(
            self,
            *,
            observation: str,
            context: "AgentToolContext",
            trace_id: str,
    ) -> DecisionResult: ...

    async def reflect(
            self,
            *,
            digest: str,
            context: "AgentToolContext | None",
            trace_id: str,
    ) -> str: ...
