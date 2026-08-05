"""System prompt builder for LLM agents (docs/agent-prompt.md §1).

The prompt is static per agent (identity card never changes), so it is cached
by agent_id. World state is *not* included here — that arrives per decision as
the observation string.
"""

from __future__ import annotations

import json

# Identity card fields, in presentation order.
_IDENTITY_FIELDS = (
    ("name", "姓名"),
    ("age", "年龄"),
    ("occupation", "职业"),
    ("background", "背景"),
    ("values", "价值观"),
    ("long_term_goals", "长期目标"),
    ("speaking_style", "说话风格"),
    ("personality", "性格五因素"),
)

_BEHAVIORAL_RULES = """行为规则：
1. 你是这个世界里生活的一名小镇居民，按照你的身份、价值观与长期目标行事。
2. 一次决策只执行一个行动；优先选择对达成长期目标有帮助的行动。
3. 行动会被世界规则校验（地点存在、可达、开门、容量、是否空闲等）；
   如果上次工具结果显示失败，换一个合理的方案，不要重复同一个失败动作。
4. 保持角色：说符合你性格的话，但行动接口只调用工具，不要编造结果。
5. 饥饿、精力、金钱会影响你的选择；感到饥饿时去商店，精力低时休息。"""

_TOOL_CONVENTIONS = """工具约定：
- move(destination_id, reason)：移动到指定地点。destination_id 必须是可见地点列表中的 id。
- wait(minutes, reason)：原地等待 1~240 分钟。
- 每个工具都必须提供中文 reason，说明你为什么这么做。
- 一次决策至多发起一次行动；其余情况请选择 wait 并给出理由。"""


def build_system_prompt(identity: dict) -> str:
    """Compose the full system prompt from an identity card dict."""
    lines: list[str] = ["你是一个生活在 AI 小镇中的智能体。", ""]
    lines.append("【身份卡】")
    for key, label in _IDENTITY_FIELDS:
        value = identity.get(key)
        if value is None:
            continue
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"- {label}: {value}")
    lines.append("")
    lines.append(_BEHAVIORAL_RULES)
    lines.append("")
    lines.append(_TOOL_CONVENTIONS)
    return "\n".join(lines)
