"""Observation builder: one Chinese text block per agent decision.

Sections follow docs/agent-prompt.md §1 (world state, self, visible locations,
visible people, available actions, last tool result, memory stub) and are
bounded to ~2000 characters. The 上次工具结果 section uses the stable markers
``结果: 成功/失败（…）`` that the fake provider parses.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.database.models.agents import Agent
from app.database.models.llm_runs import LLMRun
from app.database.models.locations import WorldLocation
from app.database.models.worlds import World
from app.world_engine.engine import is_location_open

MAX_OBSERVATION_CHARS = 2000

_WEATHER_NAMES = {
    "clear": "晴朗",
    "cloudy": "多云",
    "rain": "下雨",
    "snow": "下雪",
}

_HOUR_WORD = ("凌晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "深夜")


def _time_word(world_time: int) -> str:
    hour = (world_time % 1440) // 60
    if hour < 5:
        return "凌晨"
    if hour < 8:
        return "早上"
    if hour < 12:
        return "上午"
    if hour == 12:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 20:
        return "傍晚"
    if hour < 23:
        return "晚上"
    return "深夜"


def _format_clock(world_time: int) -> str:
    minutes = world_time % 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _action_text(agent: Agent, world_time: int) -> str:
    """Human-readable description of an agent's current action (or 空闲)."""
    if agent.action_type is None:
        return "空闲"
    if agent.action_type == "move":
        data = agent.action_data or {}
        return f"正在前往 {data.get('to') or '某地'}（{data.get('reason') or '无理由'}）"
    if agent.action_type == "wait":
        data = agent.action_data or {}
        remaining = (agent.action_ends_at or world_time) - world_time
        return f"等待中（{data.get('reason') or '休息'}，剩余 {max(remaining, 0)} 分钟）"
    return agent.action_type


def build_observation(
    world_id: str,
    agent_id: str,
    session_factory: sessionmaker[Session],
) -> str:
    """Compose the observation text for one agent decision."""
    session = session_factory()
    try:
        world = session.get(World, world_id)
        if world is None:
            return "（世界不存在）"
        agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
        if agent is None:
            return "（智能体不存在）"
        locations = list(
            session.scalars(
                select(WorldLocation)
                .where(WorldLocation.world_id == world_id)
                .order_by(WorldLocation.location_id)
            )
        )
        others = list(
            session.scalars(
                select(Agent)
                .where(Agent.world_id == world_id, Agent.agent_id != agent_id)
                .order_by(Agent.agent_id)
            )
        )
        last_run = session.scalars(
            select(LLMRun)
            .where(LLMRun.world_id == world_id, LLMRun.agent_id == agent_id)
            .order_by(LLMRun.created_at.desc(), LLMRun.run_id.desc())
            .limit(1)
        ).first()
        world_time = world.world_time
        weather = _WEATHER_NAMES.get(world.weather, world.weather or "晴朗")

        location_by_id = {loc.location_id: loc for loc in locations}
        current_loc = location_by_id.get(agent.location_id)
        if current_loc is not None:
            open_state = (
                "开门"
                if is_location_open(
                    current_loc.location_type,
                    current_loc.open_hour,
                    current_loc.close_hour,
                    world_time,
                )
                else "关门"
            )
            here = f"{current_loc.name}（{open_state}）"
        else:
            here = f"空地({agent.col},{agent.row})"

        lines: list[str] = []
        lines.append(
            f"【世界现状】第{world_time // 1440 + 1}天 {_time_word(world_time)} "
            f"{_format_clock(world_time)} 天气: {weather}"
        )
        lines.append(
            f"【自身状态】饥饿: {agent.hunger}/100 精力: {agent.energy}/100 "
            f"金钱: {agent.money} 所在位置: {here} 当前行动: {_action_text(agent, world_time)}"
        )

        lines.append("【可见地点】")
        for loc in locations:
            open_state = (
                "开门"
                if is_location_open(loc.location_type, loc.open_hour, loc.close_hour, world_time)
                else "关门"
            )
            mark = "（当前位置）" if loc.location_id == agent.location_id else ""
            lines.append(f"- {loc.name}({loc.location_id}): {open_state}{mark}")

        same_location = [a for a in others if a.location_id == agent.location_id]
        lines.append("【可见人物】")
        if same_location:
            for other in same_location:
                lines.append(f"- {other.name}: {_action_text(other, world_time)}")
        else:
            lines.append("（当前地点没有其他人）")

        lines.append("【可做的事】")
        lines.append("- move(destination_id, reason): 移动到可见地点中的某个 id")
        lines.append("- wait(minutes, reason): 原地等待 1~240 分钟")

        lines.append("【上次工具结果】")
        if last_run is None:
            lines.append("（无）")
        else:
            detail = ""
            tool_result = last_run.tool_result or {}
            if isinstance(tool_result, dict):
                detail = tool_result.get("reason") or ""
            status = "成功" if last_run.success else "失败"
            arguments = json.dumps(last_run.tool_arguments or {}, ensure_ascii=False)
            lines.append(
                f"工具: {last_run.tool_name or '（无）'} | 参数: {arguments} | "
                f"结果: {status}（{detail or '已执行'}）"
            )

        lines.append("【相关记忆】")
        lines.append("[记忆系统将在后续里程碑启用]")

        observation = "\n".join(lines)
        if len(observation) > MAX_OBSERVATION_CHARS:
            observation = observation[: MAX_OBSERVATION_CHARS - 20] + "\n…[观察已截断]"
        return observation
    finally:
        session.close()
