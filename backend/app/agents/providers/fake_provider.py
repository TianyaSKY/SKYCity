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

import json
import re
import time
from typing import Any

from app.agents.providers.base import DecisionResult
from app.services.conversation_service import MSG_TARGET_BUSY

# Per-agent scripts. agent_chenyu deliberately opens with a move to a
# nonexistent location ("ghost_town") that the world rules reject, then
# recovers on the next decision (T3-9).
#
# agent_linxia (M5) demonstrates the full economy chain: shop -> buy bread
# x4 (50 - 4*12 = 2 left) -> a fifth buy fails 余额不足 (T3-9 tool-failure
# adjustment) -> farm work (wage + wheat) -> sell the wheat -> buy bread ->
# eat it. The script is deterministic; conversation initiation may only
# interleave when another idle agent is within earshot (accepted).
DEFAULT_SCRIPTS: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "agent_linxia": [
        ("move", {"destination_id": "village_shop", "reason": "去商店买点吃的"}),
        ("buy_item", {"item_id": "bread", "quantity": 1, "reason": "买一个面包"}),
        ("buy_item", {"item_id": "bread", "quantity": 1, "reason": "再买一个面包囤着"}),
        ("buy_item", {"item_id": "bread", "quantity": 1, "reason": "多囤点面包"}),
        ("buy_item", {"item_id": "bread", "quantity": 1, "reason": "再买一个"}),
        ("buy_item", {"item_id": "bread", "quantity": 1, "reason": "最后再来一个"}),  # fails: 余额不足
        ("move", {"destination_id": "village_farm", "reason": "去农场干活赚钱"}),
        ("work", {"job_id": "job_farm_field", "reason": "干农活赚点钱"}),
        ("move", {"destination_id": "village_shop", "reason": "回商店卖小麦、买面包"}),
        ("sell_item", {"item_id": "wheat", "quantity": 1, "reason": "卖掉收获的小麦"}),
        ("buy_item", {"item_id": "bread", "quantity": 1, "reason": "买面包充饥"}),
        ("use_item", {"item_id": "bread", "reason": "饿了，吃面包"}),
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
# At this hunger the agent drops everything and seeks food (branch 1.5).
HUNGER_THRESHOLD = 80
BREAD_PRICE = 12

_SECTION_HEADER = "【上次工具结果】"
_FAILED_MARKER = "结果: 失败"
_MESSAGES_HEADER = "【收到的消息】"
_MESSAGE_LINE = re.compile(r"^-\s*(.+?)（([^（）]+), (\w+)）：(.+)$")
# e.g. 工具: talk | 参数: {...} | 结果: 失败（对方正在忙）
_TOOL_RESULT_LINE = re.compile(r"工具: (\w+) \| 参数: (.+?) \| 结果: (\w+)（(.+?)）")


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

        # 1.5. Hunger response (multi-day robustness): at high hunger the agent
        # eats, buys food, or goes to earn money — before socializing. No
        # script consumption.
        hunger = self._hunger(observation)
        if hunger >= HUNGER_THRESHOLD:
            if self._has_item(observation, "bread"):
                return self._result(
                    agent_id,
                    "use_item",
                    {"item_id": "bread", "reason": "肚子饿了，吃点面包"},
                    started,
                )
            if self._at_shop(observation):
                money = self._money(observation)
                if money >= BREAD_PRICE:
                    return self._result(
                        agent_id,
                        "buy_item",
                        {"item_id": "bread", "quantity": 1, "reason": "买点面包充饥"},
                        started,
                    )
                if self._last_tool_failed(observation):
                    return self._result(
                        agent_id,
                        "wait",
                        {"minutes": 30, "reason": "商店没货，等补货"},
                        started,
                    )
                return self._result(
                    agent_id,
                    "wait",
                    {"minutes": 15, "reason": "钱不够，休息想想办法"},
                    started,
                )
            if self._money(observation) >= BREAD_PRICE:
                return self._result(
                    agent_id,
                    "move",
                    {"destination_id": "village_shop", "reason": "去商店买吃的"},
                    started,
                )
            if self._at_farm(observation):
                return self._result(
                    agent_id,
                    "work",
                    {"job_id": "job_farm_field", "reason": "干活挣钱买吃的"},
                    started,
                )
            return self._result(
                agent_id,
                "move",
                {"destination_id": "village_farm", "reason": "去农场干活挣钱"},
                started,
            )

        # 2. Initiate: greet the nearest idle agent within earshot, unless a
        # conversation is already active with them or the pair is in cooldown.
        engine = getattr(context, "engine", None)
        conversation_service = getattr(engine, "conversation_service", None)
        if conversation_service is not None:
            # 1b. A reply rejected because the target was busy is retried
            # (T3-9-style recovery): the target frees up when its action ends,
            # and the retry delivers the message instead of wedging the
            # conversation open and silent forever.
            last = self._last_tool_result(observation)
            if (
                last is not None
                and last[0] == "talk"
                and last[1] is not None
                and last[2] == MSG_TARGET_BUSY
            ):
                return self._result(agent_id, "talk", last[1], started)

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

    # ------------------------------------------------------------------ #
    # Daily reflection (M6 T6-6): deterministic summary from the digest
    # ------------------------------------------------------------------ #

    async def reflect(
        self,
        *,
        digest: str,
        context: Any,
        trace_id: str,
    ) -> str:
        """Canned reflection derived from the digest's key numbers.

        The digest carries stable markers (``工作 N 次`` and ``（N 位朋友）``)
        so the summary is fully deterministic — the same day always produces
        the same reflection, which keeps tests reproducible.
        """
        work = 0
        friends = 0
        match = re.search(r"工作 (\d+) 次", digest)
        if match is not None:
            work = int(match.group(1))
        match = re.search(r"（(\d+) 位朋友）", digest)
        if match is not None:
            friends = int(match.group(1))
        return f"今天完成了{work}次工作，和{friends}位朋友聊天。明天继续努力。"

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

    # ------------------------------------------------------------------ #
    # Hunger-response parsing (observation section formats)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _hunger(observation: str) -> int:
        match = re.search(r"饥饿:\s*(\d+)/100", observation)
        return int(match.group(1)) if match else 0

    @staticmethod
    def _money(observation: str) -> int:
        match = re.search(r"金钱:\s*(\d+)", observation)
        return int(match.group(1)) if match else 0

    @staticmethod
    def _has_item(observation: str, item_id: str) -> bool:
        return re.search(rf"（{re.escape(item_id)}）×(\d+)", observation) is not None

    @staticmethod
    def _at_shop(observation: str) -> bool:
        return "所在位置: 村庄杂货店" in observation

    @staticmethod
    def _at_farm(observation: str) -> bool:
        return "所在位置: 晨露农场" in observation

    @staticmethod
    def _last_tool_result(
        observation: str,
    ) -> tuple[str, dict[str, Any] | None, str | None] | None:
        """(tool_name, arguments, failure_reason) for the last tool result line,
        or None when the last tool succeeded / there is no recorded run."""
        idx = observation.find(_SECTION_HEADER)
        if idx < 0:
            return None
        match = _TOOL_RESULT_LINE.search(observation[idx : idx + 500])
        if match is None:
            return None
        tool_name, arguments_json, status, detail = match.groups()
        if status != "失败":
            return None
        try:
            arguments = json.loads(arguments_json)
        except (ValueError, TypeError):
            arguments = None
        return tool_name, arguments if isinstance(arguments, dict) else None, detail

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
