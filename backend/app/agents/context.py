"""Server-side context injected into every agent tool call.

``agent_id`` is filled in by the decision service — never by the model — so an
agent cannot impersonate another (docs/agent-prompt.md §3). Tools reach the
world engine's rule gate through ``action_service`` and never touch SQL/ORM/WS.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.action_execution_service import ActionExecutionService
from app.world_engine.engine import WorldEngine


@dataclass(slots=True)
class AgentToolContext:
    """Everything a tool needs to perform one validated world action."""

    world_id: str
    agent_id: str
    action_service: ActionExecutionService
    engine: WorldEngine
    trace_id: str | None = None
