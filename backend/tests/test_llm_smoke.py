"""M9 real-LLM smoke tests against OpenAIProvider.

These exercise the REAL model end to end (decision -> tool call -> world
rules) and are skipped entirely without OPENAI_API_KEY (or when the
openai-agents SDK cannot be imported). Each test drives ``provider.decide``
directly on a tiny world created with autonomous=False — no background loop.

Coverage (smoke): (1) the provider returns a legal move/wait tool call;
(2) a move's destination_id is one of the world's 8 locations; (3) a talk
message is clean dialogue (no inner-monologue markers); (4) after a failed
buy in 上次工具结果 the provider does not retry the same failed call.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from app.agents.context import AgentToolContext
from app.agents.observation_service import build_observation
from app.config.settings import get_settings
from app.database.session import SessionLocal
from app.services.action_execution_service import ActionExecutionService
from app.services.conversation_service import ConversationService
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
from app.world_engine.engine import WorldEngine

try:  # openai-agents must be importable at collection time
    from app.agents.providers.openai_provider import OpenAIProvider

    PROVIDER_AVAILABLE = True
except Exception:  # pragma: no cover - keyless/offline environments
    OpenAIProvider = None  # type: ignore[assignment,misc]
    PROVIDER_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY") or not PROVIDER_AVAILABLE,
    reason="需要真实 LLM API 密钥与 openai-agents SDK",
)

# One smoke call should not hang CI: bound the provider round-trip.
SMOKE_TIMEOUT_SECONDS = 90


@pytest.fixture(scope="module")
def world_config() -> ParsedWorldConfig:
    return load_world_config(get_settings())


def make_engine(world_config: ParsedWorldConfig) -> WorldEngine:
    eng = WorldEngine(
        session_factory=SessionLocal,
        world_config=world_config,
        world_data_dir=Path(get_settings().world_data_dir).resolve(),
    )
    eng.action_service = ActionExecutionService(eng, SessionLocal)
    eng.conversation_service = ConversationService(eng, SessionLocal)
    return eng


def decide_once(
    engine: WorldEngine,
    world_id: str,
    agent_id: str,
    observation: str,
    trace_id: str,
):
    """One real provider call; returns the extracted first tool call."""
    provider = OpenAIProvider(get_settings())
    context = AgentToolContext(
        world_id=world_id,
        agent_id=agent_id,
        action_service=engine.action_service,
        engine=engine,
    )
    return asyncio.run(
        asyncio.wait_for(
            provider.decide(
                observation=observation, context=context, trace_id=trace_id
            ),
            timeout=SMOKE_TIMEOUT_SECONDS,
        )
    )


def visible_locations_text(world_config: ParsedWorldConfig) -> str:
    lines = []
    for loc in world_config.locations:
        lines.append(f"- {loc.name}({loc.location_id}): 开门")
    return "\n".join(lines)


def location_ids(world_config: ParsedWorldConfig) -> set[str]:
    return {loc.location_id for loc in world_config.locations}


def test_llm_returns_legal_tool_call(world_config: ParsedWorldConfig) -> None:
    """(1) The real provider must return a valid move/wait tool call."""
    eng = make_engine(world_config)
    runtime = eng.create_world("冒烟世界", autonomous=False)
    world_id = runtime.world_id
    observation = build_observation(
        world_id, "agent_linxia", SessionLocal, memory_service=eng.memory_service
    )
    result = decide_once(
        eng, world_id, "agent_linxia", observation, "trc_smoke_1"
    )
    assert result.tool_name in {"move", "wait"}, (
        f"unexpected tool {result.tool_name}: {result.tool_arguments}"
    )
    assert isinstance(result.tool_arguments, dict)
    eng._runtimes.clear()


def test_llm_move_destination_is_visible_location(
    world_config: ParsedWorldConfig,
) -> None:
    """(2) A move must target one of the world's 8 locations."""
    eng = make_engine(world_config)
    runtime = eng.create_world("冒烟世界", autonomous=False)
    world_id = runtime.world_id
    ids = location_ids(world_config)
    assert len(ids) == 8

    observation = (
        "【世界现状】第1天 上午 10:00 天气: 晴朗\n"
        "【自身状态】饥饿: 85/100 精力: 90/100 金钱: 50 所在位置: 家 当前行动: 空闲\n"
        "【可见地点】\n"
        f"{visible_locations_text(world_config)}\n"
        "【可做的事】\n"
        "- move(destination_id, reason): 移动到可见地点中的某个 id\n"
        "- wait(minutes, reason): 原地等待 1~240 分钟\n"
        "【上次工具结果】\n"
        "（无）"
    )
    result = decide_once(
        eng, world_id, "agent_linxia", observation, "trc_smoke_2"
    )
    if result.tool_name == "move":
        destination = result.tool_arguments.get("destination_id")
        assert destination in ids, (
            f"move destination {destination!r} not in {sorted(ids)}"
        )
    else:
        assert result.tool_name == "wait"
    eng._runtimes.clear()


def test_llm_talk_message_is_clean_dialogue(
    world_config: ParsedWorldConfig,
) -> None:
    """(3) A talk reply must be real dialogue, not inner monologue."""
    eng = make_engine(world_config)
    runtime = eng.create_world("冒烟世界", autonomous=False)
    world_id = runtime.world_id

    observation = (
        "【世界现状】第1天 早上 08:00 天气: 晴朗\n"
        "【自身状态】饥饿: 20/100 精力: 90/100 金钱: 50 所在位置: 家 当前行动: 空闲\n"
        "【收到的消息】\n"
        "- 张明（agent_zhangming, ask）：你今天有空吗？要不要一起去广场走走？\n"
        "【可见地点】\n"
        f"{visible_locations_text(world_config)}\n"
        "【可做的事】\n"
        "- move(destination_id, reason): 移动到可见地点中的某个 id\n"
        "- wait(minutes, reason): 原地等待 1~240 分钟\n"
        "- talk(target_agent_id, message, intent): 与附近且空闲的智能体对话；"
        "收到消息时应当回复（intent 用 chat/ask/offer）\n"
        "【上次工具结果】\n"
        "（无）"
    )
    result = decide_once(
        eng, world_id, "agent_linxia", observation, "trc_smoke_3"
    )
    if result.tool_name == "talk":
        message = str(result.tool_arguments.get("message") or "")
        assert len(message) > 0
        assert not message.startswith("我想"), f"inner monologue leaked: {message}"
        assert "心理活动" not in message
        assert "内心" not in message
    eng._runtimes.clear()


def test_llm_adjusts_after_failed_tool(world_config: ParsedWorldConfig) -> None:
    """(4) A failed buy must not be retried as the same tool call."""
    eng = make_engine(world_config)
    runtime = eng.create_world("冒烟世界", autonomous=False)
    world_id = runtime.world_id

    observation = (
        "【世界现状】第1天 上午 10:30 天气: 晴朗\n"
        "【自身状态】饥饿: 75/100 精力: 80/100 金钱: 2 所在位置: 村口商店 当前行动: 空闲\n"
        "【可见地点】\n"
        f"{visible_locations_text(world_config)}\n"
        "【可做的事】\n"
        "- move(destination_id, reason): 移动到可见地点中的某个 id\n"
        "- wait(minutes, reason): 原地等待 1~240 分钟\n"
        "- buy_item(item_id, reason, quantity=1): 在商店购买商品；钱不够会被拒绝\n"
        "- work(job_id, reason): 在当前地点开始【可做的事】里列出的工作\n"
        "- sell_item(item_id, reason, quantity=1): 把背包里的物品卖给商店换钱\n"
        "【上次工具结果】\n"
        "工具: buy_item | 参数: {\"item_id\": \"bread\", \"quantity\": 1} | "
        "结果: 失败（余额不足）"
    )
    result = decide_once(
        eng, world_id, "agent_linxia", observation, "trc_smoke_4"
    )
    same_failed_call = (
        result.tool_name == "buy_item"
        and result.tool_arguments.get("item_id") == "bread"
    )
    assert not same_failed_call, (
        f"provider retried the failed buy: {result.tool_name} {result.tool_arguments}"
    )
    eng._runtimes.clear()
