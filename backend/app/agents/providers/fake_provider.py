"""Deterministic scripted decision provider.

Used by tests and by keyless environments (``llm_provider=auto`` without an
API key). Each agent follows a fixed script of ``(tool_name, arguments)``
entries; every entry is served once in order, then the script cycles with a
default wait. When the observation reports that the agent's last tool call
was rejected by the world, the provider moves to the next entry (the recovery
action), demonstrating T3-9: adjust after failure.

M4 adds a deterministic conversation demo on top of the scripts, in priority
order:

1. Respond — when 【收到的消息】 lists unread messages, reply to the first
   sender: ``chat`` with a canned in-character line; once the agent has sent
   >= 2 messages in the current conversation it instead sends ``leave``
   ("不聊了，我得去忙了"), ending the conversation. No script consumption.
   A partner's ``leave`` is not answered (the conversation is over).
2. Initiate — idle at a spot with another idle agent within 3 cells, greet
   the nearest one (``greet`` + canned line), unless the pair is in cooldown.
3. Script — the original per-agent script (unchanged).

The failure probe reads the 上次工具结果 section of the observation, which the
observation service renders as ``结果: 成功/失败（…）``.
"""

from __future__ import annotations

import re
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

# M4 conversation demo: canned in-character lines, cycled per agent.
TALK_POOL = [
    "你好呀，今天天气不错",
    "今天工作忙吗？",
    "听说商店进了新货",
    "广场上真热闹",
    "你吃过饭了吗？",
]
LEAVE_MESSAGE = "不聊了，我得去忙了"
TALK_DISTANCE = 3
# An agent that has already sent this many messages in the current
# conversation stops chatting and says goodbye.
MAX_SENT_BEFORE_LEAVE = 2

_SECTION_HEADER = "【上次工具结果】"
_FAILED_MARKER = "结果: 失败"
_MESSAGES_HEADER = "【收到的消息】"
_MESSAGE_LINE = re.compile(r"^-\s*(.+?)（([^（）]+), (\w+)）：(.+)$")


class FakeDecisionProvider:
    """Scripted provider: deterministic, instant, no network."""

    def __init__(
        self,
        scripts: dict[str, list[tuple[str, dict[str, Any]]]] | None = None,
    ) -> None:
        self._scripts = {
            agent_id: list(entries) for agent_id, entries in (scripts or DEFAULT_SCRIPTS).items()
        }
        # All state is keyed by (world_id, agent_id): one provider instance is
        # shared across worlds (and restarts), so world A's consumed scripts
        # must never leak into world B.
        self._script_positions: dict[tuple[str, str], int] = {}
        self._conversation_sent: dict[tuple[str, str], int] = {}
        self._pool_cursor: dict[tuple[str, str], int] = {}

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
        started = time.perf_counter()

        # 1. Respond to unread messages (no script consumption).
        messages = self._unread_messages(observation)
        if messages:
            first = messages[0]
            if first[1] != "leave":  # a leave is not answered; conversation over
                if first[1] == "greet":
                    # A greeting opens a fresh conversation: reset the counter
                    # so a previous conversation's count cannot force an
                    # instant goodbye in the new one.
                    self._conversation_sent[(context.world_id, agent_id)] = 0
                if self._conversation_sent.get((context.world_id, agent_id), 0) >= MAX_SENT_BEFORE_LEAVE:
                    tool_name, tool_arguments = "talk", {
                        "target_agent_id": first[0],
                        "message": LEAVE_MESSAGE,
                        "intent": "leave",
                    }
                else:
                    self._conversation_sent[(context.world_id, agent_id)] = (
                        self._conversation_sent.get((context.world_id, agent_id), 0) + 1
                    )
                    tool_name, tool_arguments = "talk", {
                        "target_agent_id": first[0],
                        "message": self._next_pool_message(context.world_id, agent_id),
                        "intent": "chat",
                    }
                return self._result(agent_id, tool_name, tool_arguments, started)

        # 2. Initiate: greet the nearest idle agent within earshot, unless a
        # conversation is already active with them or the pair is in cooldown.
        engine = getattr(context, "engine", None)
        conversation_service = getattr(engine, "conversation_service", None)
        if conversation_service is not None:
            targets = engine.idle_agents_near(
                context.world_id, agent_id, TALK_DISTANCE
            )
            if targets:
                target = targets[0]
                if (
                    conversation_service.active_between(
                        context.world_id, agent_id, target
                    )
                    is None
                    and not conversation_service.in_cooldown(
                        context.world_id, agent_id, target
                    )
                ):
                    self._conversation_sent[(context.world_id, agent_id)] = 1
                    return self._result(
                        agent_id,
                        "talk",
                        {
                            "target_agent_id": target,
                            "message": self._next_pool_message(context.world_id, agent_id),
                            "intent": "greet",
                        },
                        started,
                    )

            # 2b. No idle neighbour, but someone is waiting within earshot:
            # wait until the earliest one frees up, so both become idle
            # together and a conversation can start (deterministic demo
            # convergence — plain scripted waits would stay phase-offset).
            waiting = engine.waiting_agents_near(
                context.world_id, agent_id, TALK_DISTANCE
            )
            if waiting:
                minutes = max(int(waiting[0][1]), 1)
                return self._result(
                    agent_id,
                    "wait",
                    {"minutes": minutes, "reason": "等朋友忙完再聊聊"},
                    started,
                )

        # 3. Script entry as before.
        script = self._scripts.get(agent_id) or [CYCLE_WAIT]
        position = self._script_positions.get((context.world_id, agent_id), 0)
        last_failed = self._last_tool_failed(observation)
        if last_failed and position == 0:
            position = 1  # defensive: never re-serve a known-bad first entry
        if position >= len(script):
            tool_name, tool_arguments = RECOVERY_MOVE if last_failed else CYCLE_WAIT
        else:
            tool_name, tool_arguments = script[position]
        self._script_positions[(context.world_id, agent_id)] = position + 1

        return self._result(agent_id, tool_name, tool_arguments, started)

    # ------------------------------------------------------------------ #
    # Result + observation parsing
    # ------------------------------------------------------------------ #

    def _result(
        self,
        agent_id: str,
        tool_name: str,
        tool_arguments: dict[str, Any],
        started: float,
    ) -> DecisionResult:
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

    def _next_pool_message(self, world_id: str, agent_id: str) -> str:
        cursor = self._pool_cursor.get((world_id, agent_id), 0)
        self._pool_cursor[(world_id, agent_id)] = cursor + 1
        return TALK_POOL[cursor % len(TALK_POOL)]

    @staticmethod
    def _last_tool_failed(observation: str) -> bool:
        """True when the observation's 上次工具结果 section reports a failure."""
        idx = observation.find(_SECTION_HEADER)
        if idx < 0:
            return False
        section = observation[idx : idx + 500]
        return _FAILED_MARKER in section

    @staticmethod
    def _unread_messages(
        observation: str,
    ) -> list[tuple[str, str, str, str]]:
        """Unread messages from 【收到的消息】: (from_agent_id, intent, name, text)."""
        start = observation.find(_MESSAGES_HEADER)
        if start < 0:
            return []
        body = observation[start + len(_MESSAGES_HEADER) :]
        end = body.find("【")
        if end >= 0:
            body = body[:end]
        messages: list[tuple[str, str, str, str]] = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            match = _MESSAGE_LINE.match(line)
            if match is None:
                continue
            name, agent_id, intent, text = match.groups()
            messages.append((agent_id, intent, name, text))
        return messages
