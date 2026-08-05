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
- talk(target_agent_id, message, intent)：与附近（距离 ≤ 3 格）且空闲的智能体对话；
  intent 取 greet/chat/ask/offer/leave 之一；对方忙碌或太远时会被拒绝。
  收到【收到的消息】里的消息时应当回复（intent 用 chat/ask/offer）；如果不想继续聊，
  用 intent=leave 礼貌告别并结束对话。
- work(job_id, reason)：在当前地点开始【可做的事】里列出的工作；完成后结算工资与产物。
- buy_item(item_id, reason, quantity=1)：在商店购买商品；钱不够会被拒绝。
- sell_item(item_id, reason, quantity=1)：把背包里的物品卖给商店换钱。
- use_item(item_id, reason)：食用背包里的食物恢复饥饿（每次消耗 1 件）。
- 每个工具都必须提供中文 reason（talk 的消息用中文），说明你为什么这么做。
- 一次决策至多发起一次行动；其余情况请选择 wait 并给出理由。"""

_ECONOMY_GUIDE = """经济规则：
- 工作是赚钱的主要方式：去工作地点用 work 工具干活，完成后拿到工资和产物。
- 感到饥饿时去商店用 buy_item 买食物（如面包）；食物要用 use_item 食用才会恢复饥饿。
- 干活收获的材料（如小麦）可以到商店 sell_item 卖钱。
- 你无法赊账或借钱：钱不够就先去工作，赚到钱再回来买东西。"""


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
    lines.append("")
    lines.append(_ECONOMY_GUIDE)
    return "\n".join(lines)
