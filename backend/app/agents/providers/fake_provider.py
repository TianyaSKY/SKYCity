"""Deterministic scripted decision provider.

Used by tests and by keyless environments (``llm_provider=auto`` without an
API key). Each agent follows a fixed script of ``(tool_name, arguments)``
entries; every entry is served once in order, then the script cycles with a
default wait. When the observation reports that the agent's last tool call
was rejected by the world, the provider moves to the next entry (the recovery
action), demonstrating T3-9: adjust after failure.

The failure probe reads the 上次工具结果 section of the observation, which the
observation service renders as ``结果: 成功/失败（…）``.
"""

from __future__ import annotations

import time
from typing import Any

from app.agents.providers.base import DecisionResult

# Per-agent scripts. agent_chenyu deliberately opens with a move to a
# nonexistent location ("ghost_town") that the world rules reject, then
# recovers on the next decision (T3-9).
DEFAULT_SCRIPTS: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "agent_linxia": [
        ("move", {"destination_id": "village_shop", "reason": "去商店看看有什么新货"}),
        ("wait", {"minutes": 30, "reason": "在商店附近歇歇脚"}),
    ],
    "agent_zhangming": [
        ("move", {"destination_id": "village_plaza", "reason": "去广场散散步"}),
        ("wait", {"minutes": 60, "reason": "在广场长椅上坐一会儿"}),
    ],
    "agent_chenyu": [
        ("move", {"destination_id": "ghost_town", "reason": "听说镇外有个鬼镇，去探险"}),
        ("move", {"destination_id": "village_plaza", "reason": "鬼镇去不了，还是去广场逛逛"}),
        ("wait", {"minutes": 30, "reason": "在广场和朋友聊聊天"}),
    ],
    "agent_wangfang": [
        ("wait", {"minutes": 45, "reason": "农场活干完了，休息一会儿"}),
    ],
    "agent_laozhang": [
        ("move", {"destination_id": "town_hall", "reason": "去镇公所看看公告"}),
        ("wait", {"minutes": 30, "reason": "在镇公所门口抽袋烟"}),
    ],
}

# Served when a script is exhausted (cycle) or a recovery move is needed.
CYCLE_WAIT = ("wait", {"minutes": 30, "reason": "例行休息"})
RECOVERY_MOVE = ("move", {"destination_id": "village_plaza", "reason": "换个地方走走"})

_SECTION_HEADER = "【上次工具结果】"
_FAILED_MARKER = "结果: 失败"


class FakeDecisionProvider:
    """Scripted provider: deterministic, instant, no network."""

    def __init__(
        self,
        scripts: dict[str, list[tuple[str, dict[str, Any]]]] | None = None,
    ) -> None:
        self._scripts = {
            agent_id: list(entries) for agent_id, entries in (scripts or DEFAULT_SCRIPTS).items()
        }
        self._positions: dict[str, int] = {}

    # ------------------------------------------------------------------ #
    # Provider interface
    # ------------------------------------------------------------------ #

    async def decide(
        self,
        *,
        observation: str,
        context: Any,
        trace_id: str,
    ) -> DecisionResult:
        agent_id = context.agent_id
        script = self._scripts.get(agent_id) or [CYCLE_WAIT]
        started = time.perf_counter()

        position = self._positions.get(agent_id, 0)
        last_failed = self._last_tool_failed(observation)
        if last_failed and position == 0:
            position = 1  # defensive: never re-serve a known-bad first entry
        if position >= len(script):
            tool_name, tool_arguments = RECOVERY_MOVE if last_failed else CYCLE_WAIT
        else:
            tool_name, tool_arguments = script[position]
        self._positions[agent_id] = position + 1

        latency_ms = max(int((time.perf_counter() - started) * 1000), 1)
        return DecisionResult(
            tool_name=tool_name,
            tool_arguments=dict(tool_arguments),
            model="fake",
            input_tokens=0,
            output_tokens=0,
            latency_ms=latency_ms,
            raw_summary=f"[fake] {tool_name} {tool_arguments}",
        )

    # ------------------------------------------------------------------ #
    # Observation parsing
    # ------------------------------------------------------------------ #

    @staticmethod
    def _last_tool_failed(observation: str) -> bool:
        """True when the observation's 上次工具结果 section reports a failure."""
        idx = observation.find(_SECTION_HEADER)
        if idx < 0:
            return False
        section = observation[idx : idx + 500]
        return _FAILED_MARKER in section
