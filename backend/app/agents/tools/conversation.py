"""talk tool for LLM agents (M4).

Funnels through ConversationService — the conversation rule gate (R1/R2/R9,
pair cooldown, max turns, duplicate detection). It never touches SQL/ORM/WS
directly and returns the same structured JSON the decision service records in
``llm_runs.tool_result``: ``{"success", "reason", "event"}`` where ``event``
is the conversation_message envelope when the message was delivered.
"""

from __future__ import annotations

import json
from typing import Literal

from agents import RunContextWrapper, function_tool

from app.agents.context import AgentToolContext

MAX_TALK_CHARS = 200


@function_tool
async def talk(
        ctx: RunContextWrapper[AgentToolContext],
        target_agent_id: str,
        message: str,
        intent: Literal["greet", "chat", "ask", "offer", "leave"],
) -> str:
    """与附近（距离 ≤ 3 格）且空闲的智能体对话。intent 表示意图：greet 打招呼、
    chat 闲聊、ask 询问、offer 提议、leave 告别（发出后对话结束）。"""
    service = ctx.context.engine.conversation_service
    if service is None:
        return json.dumps(
            {"success": False, "reason": "对话服务未初始化", "event": None},
            ensure_ascii=False,
        )
    message = (message or "").strip()[:MAX_TALK_CHARS]
    ok, reason, envelope = service.send_message(
        world_id=ctx.context.world_id,
        from_agent_id=ctx.context.agent_id,
        to_agent_id=target_agent_id,
        message=message,
        intent=intent,
        trace_id=ctx.context.trace_id,
    )
    return json.dumps(
        {
            "success": ok,
            "reason": reason,
            "event": envelope.model_dump() if envelope is not None else None,
        },
        ensure_ascii=False,
    )
