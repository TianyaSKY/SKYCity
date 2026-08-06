"""build tool for LLM agents (M14 construction, R22).

Funnels through BuildService — the construction rule gate. Never touches
SQL/ORM/WS directly and returns the same structured JSON the decision service
records in ``llm_runs.tool_result``: ``{"success", "reason", "event"}``.
"""

from __future__ import annotations

import json

from agents import RunContextWrapper, function_tool

from app.agents.context import AgentToolContext


def _result_json(ok: bool, envelope, reason: str | None) -> str:
    return json.dumps(
        {
            "success": ok,
            "reason": reason,
            "event": envelope.model_dump() if envelope is not None else None,
        },
        ensure_ascii=False,
    )


@function_tool
async def build(
    ctx: RunContextWrapper[AgentToolContext],
    col: int,
    row: int,
    blueprint_id: str,
    reason: str,
) -> str:
    """在 (col,row) 格建造蓝图建筑（蓝图名见可做的事列表）。要求：你离目标格 ≤ 3 格、
    目标格可行走且未被占用、背包里有足够材料；建造期间不能做其他事，
    完成后建筑会真实出现在地图上并影响通行。"""
    service = ctx.context.engine.build_service
    if service is None:
        return json.dumps({"success": False, "reason": "建造服务未初始化", "event": None}, ensure_ascii=False)
    ok, envelope, err = service.build_start(
        world_id=ctx.context.world_id,
        agent_id=ctx.context.agent_id,
        col=col,
        row=row,
        blueprint_id=blueprint_id,
        reason=reason,
    )
    return _result_json(ok, envelope, err)
